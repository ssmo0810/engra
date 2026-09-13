#!/usr/bin/env python3
"""수처리 데이터로 검출 성능을 잰다 — 엔진은 그대로, 태그 명세와 입구만 바꾼다.

    ENGRA_TAG_MASTER=docs/water/tag_master.csv \
      python3 engine/water_check.py <근무CSV> <정답지JSON> [<CSV> <JSON> ...]

**왜 따로 있나.** `tools/score.py` 는 `app/db.py` 에 이미 적재된 이벤트를 채점한다.
이 스크립트는 생성기가 내려준 CSV 를 직접 읽어 `engine` 네 함수만 돌리고,
**채점은 `tools/score.py` 의 `score_shifts` 를 그대로 부른다.** 채점 규칙이 두 벌이면
ASU 수치와 비교가 성립하지 않는다 — 규칙은 한 벌이어야 한다.

**증명하려는 것.** 수처리에서 바꾼 것은 두 가지뿐이다.
  ① 태그 명세  ENGRA_TAG_MASTER=docs/water/tag_master.csv
  ② 데이터 입구 이 스크립트의 CSV 파서
`engine/core.py`·`detectors.py`·`api.py` 의 검출 로직은 한 줄도 바뀌지 않았고,
문턱값(k≥3.5, |r|≥0.60 …)도 ASU 와 같은 값을 그대로 쓴다.
"""
import csv
import json
import os
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ensure_sqlite3():
    """LibreOffice 동봉 파이썬처럼 sqlite3 가 없는 인터프리터에서도 채점이 돌게 한다.

    `tools/score.py` 는 `app/db.py` 를 import 하지만, `events_of` 를 주면 DB 를
    열지 않는다(score.py 의 nullcontext 경로). import 만 통과시키면 된다.
    """
    try:
        import sqlite3  # noqa: F401
        return
    except ImportError:
        pass
    m = types.ModuleType("sqlite3")
    m.Row = object
    m.connect = lambda *a, **k: None
    m.register_adapter = lambda *a, **k: None
    m.register_converter = lambda *a, **k: None
    m.PARSE_DECLTYPES, m.PARSE_COLNAMES = 1, 2
    m.Error = m.IntegrityError = m.OperationalError = Exception
    sys.modules["sqlite3"] = m


def load_series(path):
    """생성기 CSV(`timestamp,tag,value` 세로형) -> {태그: [(ts, 값)]}."""
    series = {}
    with open(path, encoding="utf-8", newline="") as f:
        rows = csv.reader(f)
        next(rows, None)
        for row in rows:
            if len(row) != 3:
                continue
            ts, tag, raw = row
            if not raw:
                continue
            try:
                value = float(raw)
            except ValueError:
                continue           # 결함 주입 CSV 의 깨진 값
            series.setdefault(tag, []).append((ts, value))
    return series


def run_shift(csv_path):
    """CSV 한 근무 -> (이벤트, 소요초, 태그수, 점수)."""
    from engine import api
    t0 = time.time()
    series = load_series(csv_path)
    points = sum(len(v) for v in series.values())
    t1 = time.time()
    summaries = api.summarize(series)
    # app/pipeline.py 와 같은 「잠정 기준선」 경로다 — 지난 근무 요약이 없으면
    # 이번 구간으로 만든다. build_baseline 은 근무 여러 개의 요약을 받으므로 한 겹 싼다.
    baselines = api.build_baseline({t: [row] for t, row in summaries.items()})
    events = api.detect(series, baselines)
    t2 = time.time()
    for i, e in enumerate(events, 1):
        e.setdefault("id", i)
    return events, (t1 - t0, t2 - t1), len(series), points


def main(argv):
    if len(argv) < 2 or len(argv) % 2:
        print(__doc__)
        return 2
    _ensure_sqlite3()
    sys.path.insert(0, str(ROOT / "tools"))
    import score

    from engine import core
    print(f"태그 명세 : {core.TAG_MASTER}  ({len(core.TAGS)}점)")

    shifts, by_shift = [], {}
    for csv_path, ans_path in zip(argv[0::2], argv[1::2]):
        key = json.loads(Path(ans_path).read_text(encoding="utf-8"))
        entries = key["shift_list"] if "shift_list" in key else [key]
        events, (t_read, t_run), ntag, npoint = run_shift(csv_path)
        sid = entries[0]["shift_id"]
        by_shift[sid] = events
        shifts.extend(entries)
        print(f"  {sid}  태그 {ntag}점 · {npoint:,}점 읽기 {t_read:.1f}s · "
              f"검출 {t_run:.1f}s → 이벤트 {len(events)}건")

    result = score.score_shifts(shifts, events_of=lambda sid: by_shift.get(sid, []))

    # 출력은 tools/score.py 의 보고 형식을 그대로 빌린다 — ASU 수치와 같은 표로 읽히게.
    score.score = lambda *a, **k: result
    return score.main(["water_check", " ".join(argv[1::2])])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
