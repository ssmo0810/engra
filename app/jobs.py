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
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import collect
import db
import pipeline
import score as score_mod

# 화면에서 고를 수 있는 정본 데이터 — 정답지가 있는 것만. 정답지 없으면 채점이 안 된다.
SOURCES = []
for key in (ROOT / "data" / "asu_answer_key.json", ROOT / "data" / "sim" / "asu_answer_all.json"):
    if key.exists():
        d = json.loads(key.read_text(encoding="utf-8"))
        for sh in d["shift_list"]:
            csv = key.parent / sh["csv_file"]
            if csv.exists():
                SOURCES.append({"shift_id": sh["shift_id"], "csv": str(csv), "key": str(key),
                                "injected": len(sh["injected"]), "set": key.parent.name if key.parent.name != "data" else "기준"})
SOURCES.sort(key=lambda s: s["shift_id"])

UPLOAD_DIR = ROOT / "app" / "uploads"

_lock = threading.Lock()
upload_lock = threading.Lock()   # 업로드는 한 번에 하나 — 본문을 메모리에 다 올리므로 동시 2건이면 MemoryMax 를 넘는다
_job = {"running": False, "step": None, "lines": [], "error": None, "shift_id": None, "result": None}


def state():
    return dict(_job)


def _start(shift_id, step):
    if not _lock.acquire(blocking=False):
        raise RuntimeError("다른 작업이 돌고 있습니다. 끝난 뒤 다시 누르세요.")
    _job.update({"running": True, "step": step, "lines": [], "error": None, "shift_id": shift_id, "result": None})


def _finish(err=None, result=None):
    _job.update({"running": False, "error": err, "result": result})
    _lock.release()


def _say(msg):
    _job["lines"].append(msg)
    print(f"  [job] {msg}", flush=True)


def ingest_path(path):
    """CSV 하나를 적재한다. 여러 근무가 섞여 있어도 collect 가 나눈다."""
    src = collect.source_for(path, collect.BadRows())
    counts = collect.ingest(src)
    if src.bad and src.bad.count:
        _say(f"⚠ 깨진 행 {src.bad.count:,}개 건너뜀 ({src.bad.count/src.bad.total:.2%})")
    return counts


def run_async(shift_id, csv_path=None, redo=False):
    """적재(선택) → 검출·AI 초안. 별도 스레드. AI 가 5~6분 걸리므로 화면은 폴링한다."""
    mark_qa_active()
    _start(shift_id, "ingest" if csv_path else "run")

    def work():
        try:
            if csv_path:
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

    threading.Thread(target=work, daemon=True).start()


def scoreboard():
    """정답지가 있는 근무 전부 대조. DB 에 이벤트가 있는 근무만 채점된다."""
    keys = sorted({s["key"] for s in SOURCES})
    out = []
    for k in keys:
        try:
            r = score_mod.score(k)
            r["key"] = k
            out.append(r)
        except Exception as exc:
            out.append({"key": k, "error": str(exc)})
    return out


_last_upload = {"files": [], "added": [], "warn": []}


def last_upload():
    return dict(_last_upload)


def note_upload(saved):
    """업로드 결과를 화면이 읽는다. 정답지 없이 CSV 만 오면 그 사실을 말한다 — 조용히 목록에서 빠지지 않게."""
    csvs = [f for f in saved if f.endswith(".csv")]
    jsons = [f for f in saved if f.endswith(".json")]
    added = [s["shift_id"] for s in SOURCES if s["set"] == "업로드" and Path(s["csv"]).name in csvs]
    warn = []
    if csvs and not jsons and not added:
        warn.append("CSV 만 올라왔고 정답지 JSON 이 없습니다. 정답지가 있어야 목록에 뜨고 대조가 됩니다 — 생성기의 asu_answer_*.json 도 같이 올리세요.")
    for s in SOURCES:
        if s["set"] == "업로드" and not Path(s["csv"]).exists():
            warn.append(f"정답지는 {Path(s['csv']).name} 을 가리키는데 그 CSV 가 안 올라왔습니다 ({s['shift_id']}).")
    _last_upload.update({"files": saved, "added": added, "warn": warn})


def mark_qa_active():
    """사용자가 리셋·실행을 누른 시각. 정시 리셋 타이머는 이 마커가 2시간 이내면 건너뛴다 —
    QA 중 04:00 리셋이 돌아 빈 상태가 기준선으로 되돌아간 사고(2026-08-27, 경모님 재현)."""
    import time
    (ROOT / "app" / ".qa_active").write_text(str(int(time.time())))


def save_upload(name, data):
    """임도영님 생성기가 내려준 CSV/정답지 JSON 을 받는다. 파일명은 그대로, 폴더만 고정."""
    UPLOAD_DIR.mkdir(exist_ok=True)
    safe = Path(name).name.replace("..", "_")
    p = UPLOAD_DIR / safe
    p.write_bytes(data)
    if safe.endswith(".json"):
        d = json.loads(data.decode("utf-8"))
        for sh in d.get("shift_list", []):
            csv = UPLOAD_DIR / Path(sh["csv_file"]).name   # 절대경로·../ 로 uploads 밖을 못 가리킨다 (Codex 반증 2026-08-27)
            if not csv.exists():
                cand = [c for c in UPLOAD_DIR.glob("*.csv") if sh["shift_id"] in c.name]   # 이름이 달라도 근무 ID 로 붙인다
                if cand:
                    csv = cand[0]
            SOURCES[:] = [s for s in SOURCES if not (s["set"] == "업로드" and s["shift_id"] == sh["shift_id"])]
            SOURCES.append({"shift_id": sh["shift_id"], "csv": str(csv), "key": str(p),
                            "injected": len(sh["injected"]), "set": "업로드"})
        SOURCES.sort(key=lambda s: s["shift_id"])
    return p
