"""화면에서 파이프라인을 돌린다 — 적재 → 검출·AI 초안 → (승인) → 정답지 대조.

경모님 지적(2026-08-27): "전체 flow 를 실행하면서 실제로 동작하는지 확인할 수 있어야 한다.
시간대 자동 생성은 검증에 의미가 없으니 버튼으로 계속 테스트할 수 있게." 그 화면의 뒷단이다.

- 한 번에 하나만 돈다(Lock). SQLite 파일 하나를 쓰므로 동시 실행은 잠금 충돌이다.
- 진행은 pipeline.run 의 say() 를 그대로 받아 줄 단위로 쌓는다. 화면이 폴링한다.
- 실패는 삼키지 않는다. 예외 문자열이 그대로 status 에 남는다.
- 정답지 대조는 tools/score.py 의 score() 를 그대로 쓴다 — 제출용 채점기와 같은 기준.
"""
import json
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import collect
import db
import pipeline
import score as score_mod

# 목록의 정본은 DB 의 shift 표다 — CSV 를 올리면 바로 적재해 그 안의 근무가 전부 목록에 뜬다.
# 정답지 JSON 은 대조용 선택 사항(KEYS). 경모님 2026-08-27: "날짜별로 다 올리고 만들 수 있어야 한다" —
# 전엔 정답지가 있어야 근무가 등록돼 CSV 만 올리면 "등록된 근무 없음" 이 나왔다. 임도영님 새 생성기는
# 근무 하나짜리 JSON(shift_id·from·to·injected 최상위)을 내보내므로 두 형식을 다 받는다.
UPLOAD_DIR = ROOT / "app" / "uploads"
KEYS = {}          # shift_id -> 정답지(근무 하나) dict (+ key_file, uploaded_at)
SEEN_FILE = UPLOAD_DIR / ".keys_first_seen.json"   # shift_id -> 그 근무 정답지를 처음 받은 시각(epoch). 다시 올려도 바뀌지 않는다
_pending = []      # 실행 중에 올라온 CSV — 그 작업이 끝나면 적재 (접근은 _qlock 아래)
_qlock = threading.Lock()
_lock = threading.Lock()
_last_run = {}     # 마지막으로 끝난 실행(검출·초안)의 shift_id·result — 뒤이어 적재가 돌아도 초안 링크가 남게 (Codex)
upload_lock = threading.Lock()   # 업로드는 한 번에 하나 — 본문을 메모리에 다 올리므로 동시 2건이면 MemoryMax 를 넘는다
_job = {"running": False, "step": None, "lines": [], "error": None, "shift_id": None, "result": None, "started": None}


def state():
    d = dict(_job)
    d["elapsed"] = (time.time() - d["started"]) if (d["running"] and d["started"]) else None   # "멈춘 건 아닐까" — 경과를 보인다
    with _qlock:
        d["pending"] = [Path(p).name for p, _ in _pending]
    d["can_skip"] = list(_job.get("can_skip") or [])
    d["advice"] = _job.get("advice")
    d["last_run"] = dict(_last_run)
    return d


def hold():
    """리셋처럼 DB 파일을 갈아끼우는 동안 실행이 시작되지 못하게 작업 락을 비차단으로 잡는다. 못 잡으면 None."""
    return _lock if _lock.acquire(blocking=False) else None


def _start(shift_id, step):
    if not _lock.acquire(blocking=False):
        raise RuntimeError("다른 작업이 돌고 있습니다. 끝난 뒤 다시 누르세요.")
    _begin(shift_id, step)


def _begin(shift_id, step):
    """락을 잡은 뒤 작업 상태를 연다."""
    _job.update({"running": True, "step": step, "lines": [], "error": None, "shift_id": shift_id, "result": None, "started": time.time()})


def _finish(err=None, result=None, drain_after=True):
    _job.update({"running": False, "error": err, "result": result})
    if _job["shift_id"] and not err:
        _last_run.update({"shift_id": _job["shift_id"], "result": result})
    _lock.release()
    if drain_after:
        drain()


def drain():
    """줄 선 CSV 가 있으면 적재를 시작한다 → 시작했으면 True. 작업 락 획득과 큐 이전을 _qlock 아래에서 한 번에 한다 —
    큐를 비운 뒤 락을 못 잡아 되돌리는 사이에 다른 작업의 _finish→drain 이 빈 큐를 보고 지나가면 CSV 가 다음
    작업까지 멈춰 있었다 (Codex). 락을 못 잡으면 큐는 그대로, 그 작업의 _finish 가 다시 부른다."""
    with _qlock:
        if not _pending:
            return False
        if not _lock.acquire(blocking=False):
            return False
        paths = _pending[:]
        _pending.clear()
        _begin(None, "ingest")
    return _run_ingest(paths)


def release_hold(lock):
    """hold() 로 잡은 락을 놓고 줄 선 적재를 이어간다 — 리셋이 큐를 멈추게 두지 않는다 (Codex)."""
    lock.release()
    drain()


def _spawn(work, requeue=None):
    """작업 스레드를 띄운다. Thread.start() 자체가 실패하면(자원 고갈) 락이 영구 점유되고 큐가 사라진다 (Codex) —
    락을 놓고 오류를 남기고, 옮겨 둔 CSV 는 큐에 되돌린다."""
    try:
        threading.Thread(target=work, daemon=True).start()
        return True
    except Exception as exc:
        if requeue:
            with _qlock:
                _pending[:0] = [p for p in requeue if p not in _pending]
        _say(f"✗ 작업 스레드를 시작하지 못함 — {type(exc).__name__}: {exc}")
        # 여기서 drain 하면 같은 실패를 무한 반복한다(실측). 큐는 다음 업로드/작업 종료 때 다시 시도된다.
        _finish(err=f"작업 스레드를 시작하지 못함: {type(exc).__name__}: {exc}", drain_after=False)
        return False


def _say(msg):
    msg = time.strftime("%H:%M:%S ") + msg      # 같은 파일을 다시 올려 똑같이 실패하면 새 시도인지 안 보였다 (경모님 2026-08-27)
    _job["lines"].append(msg)
    print(f"  [job] {msg}", flush=True)


def ingest_path(path, skip_bad=False):
    """CSV 하나를 적재한다. 여러 근무가 섞여 있어도 collect 가 나눈다.
    skip_bad=False: 깨진 행 1% 까지는 건너뛰고 넘으면 거부(잘못된 파일). True: 사용자가 명시적으로 건너뛰기를 택함 — 상한 없음.
    깨진 행이 있으면 근무별 품질 기록을 남긴다 → 초안에 '데이터 품질' 항목으로 들어가고 AI 도 그 사실을 알고 판단한다."""
    bad = collect.BadRows(max_ratio=None if skip_bad else collect.MAX_BAD_RATIO)
    src = collect.source_for(path, bad)
    try:
        counts = collect.ingest(src)
    except ValueError as exc:
        if "깨졌습니다" in str(exc):
            # 거부하되 모양을 말한다 — 연속(계측 끊김: 건너뛰면 결측 구간으로 초안에 들어감) / 산발(전송 문제: 다시 내려받기 권고)
            kind, runs, ntags = bad.pattern()
            if kind == "연속":
                advice = ("특정 태그가 오래 비어 있습니다: " + ", ".join(f"{r['tag']} {r['start'][11:16]}~{r['end'][11:16]} ({r['minutes']}분)" for r in runs[:3])
                          + ". 「깨진 행을 건너뛰고 적재」하면 그 구간이 초안에 '계측 결측' 항목으로 들어갑니다.")
            else:
                advice = (f"{ntags}개 태그에 흩어져 비어 있습니다 — 데이터 전송 문제로 보입니다. RTDB 에서 다시 내려받아 올리기를 권합니다. "
                          "그래도 진행하려면 「깨진 행을 건너뛰고 적재」(결측이 초안에 '산발 결측' 항목으로 남습니다).")
            _job["advice"] = advice
            _say("   → " + advice)
        raise
    if bad.note:
        _say("⚠ " + bad.note)
    if bad.count:
        _say(f"⚠ 깨진 행 {bad.count:,}개 건너뜀 ({bad.count/bad.total:.2%}) — 태그별: " + ", ".join(f"{t} {n}" for sid in bad.by_shift for t, n in list(bad.by_shift[sid].items())[:4]))
    with db.connect() as conn:
        for sid in counts:
            q = bad.quality(sid, skip_bad)   # 깨진 행이 없으면 None — 정상 파일로 다시 적재하면 옛 기록이 지워진다 (Codex)
            gaps = db.find_gaps(conn, sid)   # 행이 빠졌든 값이 비었든, 태그별로 10분 넘게 값이 없는 구간
            if gaps:
                q = q or {"bad_rows": 0, "unattributed_rows": 0, "by_tag": {}, "samples": [], "skipped_by_user": bool(skip_bad), "pattern": "연속", "affected_tags": 0, "runs": []}
                q["gaps"] = gaps[:20]
                _say(f"⚠ 계측 결측 구간 {len(gaps)}개 — " + ", ".join(f"{g['tag']} {g['start'][11:16]}~{g['end'][11:16]} ({g['minutes']}분)" for g in gaps[:3]) + (" …" if len(gaps) > 3 else ""))
            db.set_quality(conn, sid, q)
    return counts


def _summarize(sids):
    """적재된 근무를 바로 요약해 둔다 — 정상(결함 없음) 데이터만 올려도 다음 근무의 기준선 재료가 된다
    (임도영님 7일 정상 데이터, 경모님 ①). 검출·초안은 만들지 않는다."""
    with db.connect() as conn:
        for sid in sids:
            series = db.load_series(conn, sid)
            if series:
                db.save_summaries(conn, sid, pipeline.ports.summarize(series))


def ingest_async(paths, skip_bad=False):
    """올라온 CSV 들을 적재한다(별도 스레드). 다른 작업이 돌고 있으면 줄을 세우고 False — 그 작업이 끝나면 이어진다."""
    mark_qa_active()
    with _qlock:
        for p in paths:
            key = (str(p), bool(skip_bad))
            if key not in _pending:
                _pending.append(key)
    return drain()


def _run_ingest(paths):
    """작업 락을 잡은 상태에서 부른다(drain). 파일별로 적재하고 요약한다. paths = [(경로, skip_bad)]."""
    def work():
        got, failed, can_skip = {}, [], []
        _job["can_skip"] = []; _job["advice"] = None
        try:
            for p, skip in paths:
                _say(f"적재 시작 — {Path(p).name}" + (" (깨진 행 건너뛰기)" if skip else ""))
                try:
                    for sid, n in sorted(ingest_path(p, skip_bad=skip).items()):
                        _say(f"{sid}: {n:,}점 적재")
                        got[sid] = n
                except Exception as exc:   # 파일 하나가 깨져도 나머지는 적재한다 (Codex). 실패는 그대로 보인다.
                    failed.append(Path(p).name)
                    _say(f"✗ {Path(p).name} 적재 실패 — {type(exc).__name__}: {exc}")
                    if not skip and "깨졌습니다" in str(exc):
                        can_skip.append(Path(p).name)   # 화면이 「깨진 행을 건너뛰고 적재」 를 제시한다(조언은 ingest_path 가 이미 적었다)
            if got:
                _summarize(list(got))
                _say(f"요약 저장 — {len(got)}근무 (다음 근무의 기준선 재료)")
            _job["can_skip"] = can_skip
            if failed and not got:
                _finish(err="적재 실패: " + ", ".join(failed))
                return
            _say("완료 — 목록에서 근무를 골라 「검출 + AI 초안」을 누르세요" + (f" (실패 {len(failed)}개는 위 로그)" if failed else ""))
            _finish(result={"ingested": sorted(got), "failed": failed})
        except Exception as exc:
            _say(f"✗ {type(exc).__name__}: {exc}")
            _finish(err=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-600:]}")
    return _spawn(work, requeue=paths)   # 스레드가 안 떴으면 False → 화면이 "시작됐다" 고 오보하지 않는다 (Codex)


def run_async(shift_id, csv_path=None, redo=False, reingest=False, why=None):
    """(재)적재(선택) → 검출·AI 초안. 별도 스레드. AI 가 5~10분 걸리므로 화면은 폴링한다."""
    mark_qa_active()
    _start(shift_id, "ingest" if csv_path else "run")

    def work():
        try:
            if csv_path:
                if reingest:
                    # 지움과 적재는 별 트랜잭션이다 — 새 파일 적재가 실패하면 이 근무의 원본은 비어 있게 되고 실행이
                    # "적재된 데이터가 없습니다" 로 멈춘다. 화면에 그대로 보이고 파일을 다시 올리면 복구되므로 스테이징은 두지 않는다.
                    with db.connect() as conn:
                        n = db.forget_raw(conn, shift_id)
                    _say(f"이 근무의 남은 원본 {n:,}점 지움 — {why or '다시 적재'} → 파일에서 다시 적재")
                _say(f"적재 시작 — {Path(csv_path).name}")
                counts = ingest_path(csv_path)
                for sid, n in sorted(counts.items()):
                    _say(f"{sid}: {n:,}점 적재")
                _job["step"] = "run"
            _say(f"[{shift_id}] 검출·초안 생성 시작 — 엔진 {pipeline.ports.engine_source()}")
            r = pipeline.run(shift_id, redo=redo, say=_say)
            _say(f"완료 — 이벤트 {r['events']}건 · 초안 {r['items']}개 · {r.get('ai','')}")
            _finish(result=r)
        except Exception as exc:  # 화면에 그대로 보인다
            _say(f"✗ {type(exc).__name__}: {exc}")
            _finish(err=f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-600:]}")
    _spawn(work)


def csv_for(shift_id):
    """이 근무의 원본 파일 — shift.source('csv:<이름>') 로 uploads/ 에서 찾는다. 없으면 None(회전 뒤 재적재 불가)."""
    with db.connect() as conn:
        row = conn.execute("SELECT source FROM shift WHERE id = ?", (shift_id,)).fetchone()
    if row and row["source"] and str(row["source"]).startswith("csv:"):
        p = UPLOAD_DIR / Path(row["source"][4:]).name
        if p.exists():
            return p
    return None


def sources():
    """드롭박스 = 적재된 근무 전부. 정답지 유무와 초안 상태를 붙인다."""
    with db.connect() as conn:
        rows = [dict(r) for r in db.list_shifts(conn)]
    out = [{"shift_id": r["id"], "has_key": r["id"] in KEYS, "status": r.get("draft_status"), "source": r.get("source")} for r in rows]
    out.sort(key=lambda s: s["shift_id"])
    return out


def scoreboard():
    """올라온 정답지 전부를 한 표로 대조. DB 에 이벤트가 있는 근무만 채점된다. 정답지가 없으면 None."""
    if not KEYS:
        return None
    try:
        return score_mod.score_shifts([KEYS[s] for s in sorted(KEYS)])
    except Exception as exc:
        return {"error": str(exc)}


_last_upload = {"files": [], "added": [], "warn": [], "info": []}


def last_upload():
    return dict(_last_upload)


def note_upload(saved, key_sids, queued):
    """업로드 결과를 화면이 읽는다 — 조용히 목록에서 빠지는 일이 없게 무엇이 왔고 무엇이 빠졌는지 말한다."""
    csvs = [f for f in saved if f.endswith(".csv")]
    jsons = [f for f in saved if f.endswith(".json")]
    warn, info = [], []
    if csvs:
        info.append("적재가 시작됐습니다 — 끝나면 목록에 그 근무가 뜹니다." if not queued else "다른 작업이 끝나면 자동으로 적재합니다.")
    if csvs and not jsons:
        info.append("정답지 JSON 은 없습니다 — 초안 생성은 되고, 정답지 대조는 그 근무의 asu_answer_*.json 을 올리면 됩니다.")
    if jsons:
        with db.connect() as conn:
            have = {r["id"] for r in db.list_shifts(conn)}
        for sid in key_sids:
            if sid not in have and not csvs:
                warn.append(f"정답지 {sid} 만 왔습니다 — 그 근무의 CSV 를 올리면 목록에 뜹니다.")
    _last_upload.update({"files": saved, "added": key_sids, "warn": warn, "info": info})


def mark_qa_active():
    """사용자가 리셋·실행을 누른 시각. 정시 리셋 타이머는 이 마커가 2시간 이내면 건너뛴다 —
    QA 중 04:00 리셋이 돌아 빈 상태가 기준선으로 되돌아간 사고(2026-08-27, 경모님 재현)."""
    (ROOT / "app" / ".qa_active").write_text(str(int(time.time())))


def save_upload(name, data):
    """생성기가 내려준 CSV/정답지 JSON 을 받는다. 파일명은 그대로, 폴더만 고정. → (경로, 정답지의 근무 ID 들)"""
    UPLOAD_DIR.mkdir(exist_ok=True)
    safe = Path(name).name.replace("..", "_")
    p = UPLOAD_DIR / safe
    p.write_bytes(data)
    sids = _register_key(p) if safe.endswith(".json") else []
    return p, sids


def _register_key(p):
    """정답지 JSON 하나를 KEYS 에 올린다. 두 형식 — 근무 하나짜리(shift_id·injected 최상위, 새 생성기) /
    묶음(shift_list, asu_answer_all.json). 업로드 직후와 서버 시작 시 둘 다 여기로."""
    d = json.loads(p.read_bytes().decode("utf-8"))
    if isinstance(d, dict) and isinstance(d.get("shift_list"), list):
        shifts = d["shift_list"]
    elif isinstance(d, dict) and "shift_id" in d and isinstance(d.get("injected"), list):
        shifts = [d]
    else:
        raise ValueError(f"{p.name}: 정답지 형식이 아닙니다 (shift_id·injected 또는 shift_list 가 없음)")
    seen = _load_seen()
    sids, changed = [], False
    for sh in shifts:
        sid = sh["shift_id"]
        # 사후 대조 판정용. 같은 정답지를 다시 올리면 파일 mtime 이 새로워져 '사전' 이 '사후' 로 뒤집힌다(Codex) —
        # 근무별 최초 수신 시각을 따로 남겨 두고 그것만 쓴다.
        if sid not in seen:
            seen[sid] = p.stat().st_mtime
            changed = True
        KEYS[sid] = {**sh, "key_file": p.name, "uploaded_at": seen[sid]}
        sids.append(sid)
    if changed:
        _save_seen(seen)
    return sids


_seen = None            # 메모리가 정본. 파일은 시작 시 한 번 읽고, 바뀔 때마다 원자적으로 쓴다
_seen_readonly = False  # 파일이 있는데 못 읽었으면 덮어쓰지 않는다 — 일시 장애 뒤 저장이 최초 시각 전체를 지우는 것 방지 (Codex)


def _load_seen():
    global _seen, _seen_readonly
    if _seen is None:
        try:
            _seen = json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _seen = {}
        except (OSError, ValueError) as exc:
            print(f"  !! {SEEN_FILE.name} 을 읽지 못함 — 최초 수신 시각을 메모리에만 두고 파일은 덮어쓰지 않는다: {exc}", file=sys.stderr)
            _seen, _seen_readonly = {}, True
    return _seen


def _save_seen(seen):
    if _seen_readonly:
        return
    UPLOAD_DIR.mkdir(exist_ok=True)
    import os
    tmp = SEEN_FILE.with_suffix(f".{os.getpid()}.tmp")   # 프로세스별 임시 이름 — 서버와 CLI 가 동시에 저장해도 충돌 없음 (Codex)
    tmp.write_text(json.dumps(seen), encoding="utf-8")
    tmp.replace(SEEN_FILE)   # 원자적 교체 — 쓰다 죽어도 반쪽 파일이 남지 않는다


def _reload_uploads():
    """서버 시작 시 이전에 올라온 정답지를 다시 등록한다 — 재시작(배포·타이머) 뒤 목록에서 사라지지 않게.
    깨진 JSON 하나가 공개 서비스 기동을 막지 않게 건너뛰고 크게 남긴다. CSV 는 DB(shift 표)가 기억한다."""
    try:
        files = sorted(UPLOAD_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime)
    except OSError as exc:
        print(f"  !! uploads/ 를 읽지 못함 — 정답지 없이 시작: {exc}", file=sys.stderr)
        return
    for j in files:
        try:
            _register_key(j)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            print(f"  !! 업로드 정답지 {j.name} 등록 실패 — 건너뜀: {exc}", file=sys.stderr)


def clear_runtime(wipe_uploads):
    """「완전 빈 상태로」 는 DB 만 아니라 메모리 상태와 올린 파일까지 비운다 — 아니면 정답지 대조표·마지막 실행 링크가 이전
    DB 를 가리키고, 재시작 때 reconcile_uploads 가 남은 CSV 를 다시 적재해 빈 상태가 깨진다 (홀리스틱 Codex 2026-08-28).
    작업 락을 잡은 채 부른다(리셋 핸들러)."""
    global _seen
    KEYS.clear(); _last_run.clear(); _last_upload.update({"files": [], "added": [], "warn": [], "info": []})
    with _qlock:
        _pending.clear()
    _job.update({"can_skip": [], "advice": None, "result": None, "shift_id": None, "lines": [], "error": None})
    if wipe_uploads and UPLOAD_DIR.exists():
        for p in UPLOAD_DIR.iterdir():
            if p.is_file():
                p.unlink()
        _seen = {}


def reconcile_uploads():
    """서버 시작 시: uploads/ 에 있는데 DB(shift.source)에 없는 CSV 를 적재한다 — 정답지 등록 실패로 목록에 못 오른
    파일(경모님 라이브 3근무, 2026-08-27)과 적재 도중 재시작된 경우를 살린다. 서버(serve)만 부른다."""
    try:
        csvs = sorted(UPLOAD_DIR.glob("*.csv"), key=lambda x: x.stat().st_mtime)
    except OSError:
        return []
    if not csvs:
        return []
    with db.connect() as conn:
        known = {Path(str(r["source"])[4:]).name for r in db.list_shifts(conn) if str(r["source"] or "").startswith("csv:")}
    todo = [p for p in csvs if p.name not in known]
    if todo:
        print(f"  uploads/ 에 적재 안 된 CSV {len(todo)}개 — 적재 시작: {[p.name for p in todo]}", flush=True)
        ingest_async(todo)
    return todo


def ingest_skip(name):
    """실패한 업로드 파일을 깨진 행을 건너뛰며 다시 적재한다(화면 버튼). uploads/ 안의 파일만."""
    p = UPLOAD_DIR / Path(name).name
    if not p.exists() or p.suffix != ".csv":
        raise FileNotFoundError(f"{name} 은 올라온 CSV 가 아닙니다")
    return ingest_async([p], skip_bad=True)


_reload_uploads()
