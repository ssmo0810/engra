#!/usr/bin/env python3
"""확정 일지 항목에 근무 구간 추이 곡선(1분 평균)을 채워 넣는다 — 한 번 돌리는 도구.

왜 필요한가. 추이 그래프는 원본 표본(raw_sample)을 그때그때 다시 읽어 그렸는데, 원본은 보관 기간 뒤
회전으로 지워진다(db.rotate_raw). 그래서 오래된 확정 일지는 그래프를 영영 못 그렸다
(경모님 2026-09-14 「확정된 것들 트랜드 왜 안 나오냐 … 과거 데이터들 것도 다 마이그레이션」).
이제 초안을 만들 때와 확정할 때 곡선을 항목에 저장한다. 이 도구는 그 전에 확정된 기록 가운데
원본이 아직 남아 있는 근무를 채운다. 원본이 이미 지워진 항목은 채울 수 없으니 세기만 한다 —
그 항목은 화면에 「추이 보기」 버튼이 나오지 않는다.

    python3 tools/backfill_curves.py

서버가 켜질 때 자동으로 돌리지 않는다 — 큰 근무가 많으면 서버 시작이 느려진다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))


def backfill():
    """확정 근무마다 곡선이 빈 항목을 채운다.
    돌려주는 것: {"shifts": 확정 근무 수, "filled": 채운 항목 수, "no_raw": 원본이 지워져 못 채운 항목 수}"""
    import db     # 부를 때 가져온다 — 시험이 DB 모듈을 갈아 끼운다
    db.init()     # 이 변경 전 DB 에는 curve_json 칸이 없다. init 은 없는 표·칸만 더한다(여러 번 돌려도 안전)
    got = {"shifts": 0, "filled": 0, "no_raw": 0}
    with db.connect() as conn:
        confirmed = [r["id"] for r in db.list_shifts(conn) if r["draft_status"] == "confirmed"]
        for sid in confirmed:
            filled, no_raw = db.save_curves(conn, sid)
            got["shifts"] += 1
            got["filled"] += filled
            got["no_raw"] += no_raw
    return got


def main():
    got = backfill()
    print(f"확정 근무 {got['shifts']}개 · 곡선을 채운 항목 {got['filled']}개 · "
          f"원본이 지워져 못 채운 항목 {got['no_raw']}개")


if __name__ == "__main__":
    main()
