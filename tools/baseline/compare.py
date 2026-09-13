#!/usr/bin/env python3
"""사람 기준치 채점 — 같은 근무를 사람과 ENGRA 가 각각 훑은 결과를 **같은 규칙**으로 잰다.

    python3 tools/baseline/compare.py tools/out/baseline/2026-09-14-day --engra
    python3 tools/baseline/compare.py tools/out/baseline/2026-09-14-day --human 기록.json
    python3 tools/baseline/compare.py tools/out/baseline/2026-09-14-day --engra --human 기록.json

채점은 `tools/score.py` 의 `score_shifts` 하나로 한다. 사람이 적은 발견 한 줄(태그·시작·끝·종류)을
엔진 이벤트 한 건과 똑같이 다룬다 — 주입 구간과 시간이 겹치고 영향 태그면 탐지, 아니면 오탐.
정답지에는 탱크 출하 같은 계획 운전(operation)도 들어 있고 엔진 채점과 똑같이 찾을 대상으로 센다.
그래서 시험자에게는 「이상」이 아니라 「인계할 변화」를 찾으라고 지시한다(README).

**분모는 정답지만으로 정한다.** `score_shifts` 는 근무 끝을 넘기는 주입을 「잡았으면 탐지로 세고, 못 잡았으면
추적 중으로 뺀다」. 한 시스템만 잴 때는 맞는 규칙이지만 둘을 나란히 두면 한쪽만 잡은 주입 때문에 분모가
달라진다 — 2026-09-13 리허설에서 ENGRA 8 · 사람 7 로 어긋났다. 그래서 비교는 근무 안에서 끝나는 주입만
분모로 세고, 넘기는 주입은 잡았는지만 따로 적는다.

**발견 시각은 같은 형식·같은 출발점으로 낸다.** 사람 시계는 파일을 불러온 뒤 「시작」을 누른 때부터 돈다.
ENGRA 도 적재를 마친 뒤부터 재서, 검출·초안이 끝나는 순간을 모든 적중의 발견 시각으로 붙인다(결과가
한꺼번에 나온다). 적재 시간은 따로 적는다.

**채점 전에 거부하는 것** — 봉인 값이 다를 때, 태그 마스터가 엔진 정본과 다를 때(사람과 엔진이 다른 한계선을
본 셈), 기록지가 다른 CSV·다른 태그 마스터로 작성됐거나 종료하지 않았거나, 시작·종료·경과가 서로 맞지 않거나,
번호 중복·근무 밖 구간·목록에 없는 태그와 종류·시험 시간 밖 발견 시각이 있을 때. 조용히 걸러 내지 않고 이유를
모두 보여 주고 멈춘다.

`--engra` 는 그 폴더 안의 별도 DB(engra.db)에 적재·실행한다. 라이브·시드 DB 는 건드리지 않는다.
AI 문장 단계는 기본으로 끈다 — `ENGRA_LLM=off` 를 명시해 넘기고 결과에도 그렇게 적는다. 여기서
재는 것은 「이상을 찾는 시간」이고, AI 가 문장을 다듬는 시간은 따로 적는다. 켜고 재려면 `--llm cli`.

한 사람이 한 번 한 결과는 표본 1이다. 숫자는 「파일럿 관찰」로만 쓴다.
"""
import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FORMAT = "engra-baseline-findings/1"
SEALED = ("shift.csv", "tag_master.csv", "sealed/answer_key.json")
# viewer.html 의 KINDS 와 같은 목록 — 엔진 이벤트 종류 이름이라, 채점이 이 이름으로 추세형·알람을 가른다
KINDS = {"이탈", "헌팅", "드리프트", "레벨시프트", "고착", "임계근접", "상관이탈",
         "H 초과", "HH 초과", "L 미달", "LL 미달", "기타"}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def check_seal(folder):
    """seal.txt 에 적힌 값과 지금 파일이 같은지, 태그 마스터가 엔진 정본과 같은지. 다르면 채점하지 않는다."""
    seals = {}
    for line in (folder / "seal.txt").read_text(encoding="utf-8").splitlines():
        p = line.split()
        if len(p) == 3 and p[1] == "SHA-256":
            seals[p[0]] = p[2]
    for name in SEALED:
        now = sha256(folder / name) if (folder / name).exists() else None
        if seals.get(name) is None or seals[name] != now:
            raise SystemExit(f"봉인 불일치 — {name}\n  seal.txt {seals.get(name)}\n  지금     {now}")
    # 사람은 폴더의 복사본을, ENGRA 는 docs/ 정본을 읽는다. 생성 뒤 정본이 바뀌었으면 둘이 다른 한계선을 본다
    if sha256(ROOT / "docs" / "tag_master.csv") != seals["tag_master.csv"]:
        raise SystemExit("태그 마스터가 엔진 정본(docs/tag_master.csv)과 다릅니다 — 생성 뒤 정본이 바뀌었으면 다시 생성하세요")
    return seals


def run_engra(folder, shift_id, llm):
    """cli.py 를 사람이 치는 순서 그대로 부른다. 단계별 벽시계 시간을 잰다.

    지난 근무 이력 없이 새 DB 에 이 근무 하나만 넣는다 — 기준선은 이번 근무로 잠정 생성된다.
    engine/api.py `build_baseline` 설명대로 검출기는 근무 안에서 「평소」를 구하고 기준선은 레벨 비교
    보조라서, 팀 정본 근무를 이력으로 먼저 넣는 단계는 두지 않았다. 결과 줄에 「기준선 잠정」을 적는다.
    """
    for f in folder.glob("engra.db*"):
        f.unlink()   # 앞선 실행의 이벤트가 남아 있으면 이번 결과와 섞인다
    env = dict(os.environ, ENGRA_DB=str(folder / "engra.db"), ENGRA_LLM=llm)
    cli = [sys.executable, str(ROOT / "app" / "cli.py")]
    times, last = {}, ""
    for step, args in (("준비", ["init"]), ("적재", ["ingest", str(folder / "shift.csv")]),
                       ("검출·초안", ["run", shift_id])):
        t0 = time.perf_counter()
        r = subprocess.run(cli + args, env=env, capture_output=True, text=True, cwd=ROOT)
        times[step] = time.perf_counter() - t0
        if r.returncode != 0:
            raise SystemExit(f"ENGRA {step} 실패\n{r.stdout[-800:]}\n{r.stderr[-800:]}")
        last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    return times, last


def _instant(v):
    """뷰어가 쓰는 ISO 시각(끝에 Z). Python 3.10 의 fromisoformat 은 Z 를 못 읽는다."""
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def human_events(path, key, seals, tags):
    """기록지를 검증하고 이벤트 모양으로 바꾼다. 문제가 하나라도 있으면 전부 보여 주고 멈춘다."""
    rec = json.loads(Path(path).read_text(encoding="utf-8"))
    bad = []
    if rec.get("format") != FORMAT:
        bad.append(f"형식 {rec.get('format')!r} — {FORMAT} 가 아님")
    if rec.get("shift_id") != key["shift_id"]:
        bad.append(f"기록지 근무 {rec.get('shift_id')} ≠ 정답지 근무 {key['shift_id']}")
    if rec.get("csv_sha256") != seals["shift.csv"]:
        bad.append("이 폴더의 shift.csv 가 아닌 파일로 작성됨 (SHA-256 불일치)")
    if rec.get("tag_master_sha256") != seals["tag_master.csv"]:
        bad.append("이 폴더의 tag_master.csv 를 함께 열지 않고 작성됨 — 한계선 조건이 엔진과 다름")
    started, ended = _instant(rec.get("started_at")), _instant(rec.get("ended_at"))
    if rec.get("ended_at") is None:
        bad.append("「종료」 전에 내보낸 기록 — 뷰어에서 종료한 뒤 다시 내보내세요")
    elif started is None or ended is None:
        bad.append("started_at·ended_at 시각 형식이 올바르지 않음")
    elapsed = rec.get("elapsed_sec")
    if not isinstance(elapsed, (int, float)) or elapsed <= 0:
        bad.append(f"elapsed_sec {elapsed!r} 가 올바르지 않음")
        elapsed = None
    elif started and ended and abs((ended - started).total_seconds() - elapsed) > 2:
        # 뷰어는 세 값을 같은 두 시각에서 만든다 — 어긋나면 손으로 고친 기록이다
        bad.append(f"elapsed_sec {elapsed} 가 시작~종료 {(ended - started).total_seconds():.0f}초와 맞지 않음")
    findings = rec.get("findings")
    if not isinstance(findings, list) or not all(isinstance(f, dict) for f in findings):
        bad.append("findings 가 발견 객체의 목록이 아님")
        findings = []
    ids = [f.get("id") for f in findings]
    if not all(isinstance(i, int) for i in ids):
        bad.append("발견 번호가 정수가 아님")
    elif len(ids) != len(set(ids)):
        bad.append("발견 번호가 중복됨")
    events = []
    for f in findings:
        label = f"발견 {f.get('id')}"
        try:
            s, e = datetime.fromisoformat(f["start"]), datetime.fromisoformat(f["end"])
        except (KeyError, TypeError, ValueError):
            bad.append(f"{label}: 시작·끝 시각 형식이 올바르지 않음")
            continue
        if not (datetime.fromisoformat(key["from"]) <= s <= e <= datetime.fromisoformat(key["to"])):
            bad.append(f"{label}: 구간 {f['start']}~{f['end']} 이 근무 밖이거나 끝이 시작보다 앞섬")
        if f.get("tag") not in tags:
            bad.append(f"{label}: 태그 {f.get('tag')!r} 가 태그 마스터에 없음")
        if f.get("kind") not in KINDS:
            bad.append(f"{label}: 종류 {f.get('kind')!r} 가 뷰어 목록에 없음")
        at = f.get("found_at_sec")
        if not isinstance(at, (int, float)) or at < 0 or (elapsed is not None and at > elapsed):
            bad.append(f"{label}: 발견 경과 {at!r} 초가 시험 시간(0~{elapsed}) 밖")
        events.append({"id": f.get("id"), "tag": f.get("tag"), "kind": f.get("kind"),
                       "start_ts": f["start"], "end_ts": f["end"], "evidence": f.get("note")})
    if bad:
        raise SystemExit("기록지를 채점하지 않습니다:\n  - " + "\n  - ".join(bad))
    return rec, events


def rows_of(key, r, min_sec):
    """주입별 판정. 이벤트가 0건이면 score_shifts 가 근무를 건너뛰므로 여기서 같은 규칙으로 채운다."""
    rows = (r["detail"].get(key["shift_id"]) or {}).get("injected")
    if rows is not None:
        return rows
    def status(inj):
        if inj.get("carried_in") and inj["duration_sec"] < min_sec:
            return "carried_tail"
        return "tracking" if inj.get("continues_next") else "miss"
    return [{**inj, "hit": False, "status": status(inj), "matched": [], "iou": None} for inj in key["injected"]]


def countable(inj, min_sec):
    """비교 분모에 넣는 주입인가 — 결과가 아니라 정답지만 보고 정한다."""
    if inj.get("continues_next"):
        return False
    return not (inj.get("carried_in") and inj["duration_sec"] < min_sec)


def tally(rows, r, min_sec):
    counted = [x for x in rows if countable(x, min_sec)]
    hits = [x for x in counted if x["status"] == "hit"]
    ious = sorted(x["iou"] for x in hits if x["iou"] is not None)
    mid = None
    if ious:
        h = len(ious) // 2
        mid = ious[h] if len(ious) % 2 else (ious[h - 1] + ious[h]) / 2
    outside = [x for x in rows if not countable(x, min_sec)]
    return {"n": len(counted), "hit": len(hits), "hits": hits, "fp": r["fp"], "iou_mid": mid,
            "out_n": len(outside), "out_hit": sum(1 for x in outside if x["status"] == "hit")}


def fmt_sec(s):
    return f"{s:.0f}초" if s < 120 else f"{s / 60:.0f}분"


def cell(x, when, min_sec):
    inside = countable(x, min_sec)
    if x["status"] == "hit":
        text = f"{fmt_sec(when(x))}에 찾음 · {x['matched'][0]['kind']}"
        return text if inside else text + " (분모 밖)"
    return "놓침" if inside else "근무 넘김 · 분모 밖"


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folder", help="make_shift.js 가 만든 폴더 (tools/out/baseline/<근무ID>)")
    ap.add_argument("--human", help="뷰어에서 내보낸 기록지 JSON")
    ap.add_argument("--engra", action="store_true", help="같은 CSV 로 ENGRA 를 돌려 채점")
    ap.add_argument("--llm", choices=["off", "cli", "api"], default="off", help="ENGRA AI 단계 (기본 off — 찾는 시간만 잰다)")
    a = ap.parse_args(argv[1:])
    if not (a.human or a.engra):
        ap.error("--human 또는 --engra 중 하나는 있어야 합니다")

    folder = Path(a.folder).resolve()
    # score.py 가 불러오는 db 모듈은 import 시점에 ENGRA_DB 로 파일을 정한다 — 그 전에 이 폴더 DB 를 가리킨다
    os.environ["ENGRA_DB"] = str(folder / "engra.db")
    sys.path.insert(0, str(ROOT / "tools"))
    import score   # noqa: E402
    min_sec = score.MIN_DETECTABLE_SEC

    seals = check_seal(folder)
    key = json.loads((folder / "sealed" / "answer_key.json").read_text(encoding="utf-8"))
    sid = key["shift_id"]
    with open(folder / "tag_master.csv", encoding="utf-8-sig", newline="") as f:
        tags = {row["tag"] for row in csv.DictReader(f)}
    # 기록지 검증은 ENGRA 를 돌리기 전에 한다 — 거부될 기록 때문에 엔진을 기다리지 않게
    human = human_events(a.human, key, seals, tags) if a.human else None
    print(f"근무 {sid} · 봉인·태그 마스터 확인 OK (shift.csv {seals['shift.csv'][:12]}…)")

    sides = []   # (이름, 주입별 행, 집계, 시간 설명, 적중 → 발견 초)
    if a.engra:
        times, last = run_engra(folder, sid, a.llm)
        r = score.score_shifts([key])
        rows = rows_of(key, r, min_sec)
        # 사람 시계는 파일을 불러온 뒤 「시작」부터 돈다 — ENGRA 발견 시각도 적재를 뺀 검출·초안 끝 시점으로 잰다
        done = times["검출·초안"]
        took = f"적재 {times['적재']:.0f}초(발견 시각에서 뺌) + 검출·초안 {times['검출·초안']:.0f}초"
        took += " (AI 문장 단계 제외 · ENGRA_LLM=off)" if a.llm == "off" else f" (AI {a.llm} 포함)"
        took += " · 새 DB·지난 근무 없음(기준선 잠정)"
        print(f"ENGRA — {last}")
        sides.append(("ENGRA", rows, tally(rows, r, min_sec), took, lambda x, done=done: done))
    if human:
        rec, events = human
        r = score.score_shifts([key], events_of=lambda s: events if s == sid else [])
        rows = rows_of(key, r, min_sec)
        found_at = {f["id"]: f["found_at_sec"] for f in rec["findings"]}
        took = f"기록 {len(events)}건 · 총 {fmt_sec(rec['elapsed_sec'])}"
        sides.append((f"사람({rec.get('tester') or '익명'})", rows, tally(rows, r, min_sec), took,
                      lambda x: min(found_at[m["id"]] for m in x["matched"])))

    print()
    print(f"{'#':>3}  {'시나리오':<24}{'트리거':<9}{'주입 구간':<13}" + "".join(f"{s[0]:<30}" for s in sides))
    for i, inj in enumerate(key["injected"]):
        name = inj["name"] + (" (운전)" if inj.get("operation") else "")   # 계획 운전 — 이것도 찾을 대상으로 센다
        line = f"{inj['scenario_id']:>3}  {name[:22]:<24}{inj['trigger_tag']:<9}{inj['start'][11:16]}~{inj['end'][11:16]}  "
        line += "".join(f"{cell(rows[i], when, min_sec):<30}" for _n, rows, _t, _took, when in sides)
        print(line)

    print()
    for name, rows, t, took, when in sides:
        iou = f" · 시각 일치 IoU 중앙 {t['iou_mid']:.2f}" if t["iou_mid"] is not None else ""
        print(f"{name}: 찾은 주입 {t['hit']}/{t['n']} · 오탐 {t['fp']}건{iou} · {took}")
        if t["out_n"]:
            print(f"  근무 끝을 넘기는 주입 {t['out_n']}건은 분모 밖 — 그중 이 근무에서 잡은 것 {t['out_hit']}건")
        if t["hits"]:
            secs = sorted(when(x) for x in t["hits"])
            marks = [m for m in (600, 1200, 1800, 2700, 3600, 5400, 7200) if m <= max(secs[-1], 600)]
            print(f"  첫 발견 {fmt_sec(secs[0])} · 마지막 발견 {fmt_sec(secs[-1])} · 누적 "
                  + " · ".join(f"{m // 60}분 {sum(1 for v in secs if v <= m)}건" for m in marks))
    print("\n표본 1회 — 「파일럿 관찰」로만 인용합니다. 채점 규칙: tools/score.py score_shifts (엔진과 동일), 분모는 정답지 기준.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
