"""스모크 테스트용 소형 CSV.

**시나리오 데이터가 아니다.** 시나리오 20종과 정답지는 임도영님 생성기
(`docs/asu_dcs_overview.html`) 가 만든다. 여기 있는 것은 배관이 뚫렸는지
확인하려는 최소 데이터일 뿐이라, 태그도 몇 개 안 되고 파형도 단순하다.

실제 검증은 임도영님 CSV + 정답지로 한다.
"""
import csv
import math
import random
from datetime import datetime, timedelta

from config import SAMPLE_INTERVAL_SEC

# 태그 마스터에 실재하는 태그만 쓴다. 없는 태그를 넣으면 스텁이 건너뛴다.
TAGS = [
    # (태그, 기저값, 진폭, 주입할 이상)
    ("TI-101", 24.3, 0.4, None),
    ("MI-102", 61.2, 1.5, None),
    ("AI-707", 99.35, 0.05, "dip"),    # L(99.0%) 미달 — 스텁이 잡는다
    ("TI-205", 118.0, 0.6, "drift"),   # 완만히 상승 — 스텁은 못 잡는다
    ("MI-302", 0.8, 0.05, "breakthrough"),  # H(3ppm) 초과 — 스텁이 잡는다
]


def generate(path, kind="day", date=None, minutes=60, seed=42, step=SAMPLE_INTERVAL_SEC):
    """작은 구간 하나를 만든다. seed 가 같으면 같은 데이터가 나온다."""
    rnd = random.Random(seed)
    date = date or datetime.now().date()
    start = datetime(date.year, date.month, date.day, 6 if kind == "day" else 18)
    n = int(minutes * 60 / step)

    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "tag", "value"])
        for i in range(n):
            ts = (start + timedelta(seconds=i * step)).isoformat(timespec="seconds")
            frac = i / max(n - 1, 1)
            for tag, base, amp, inject in TAGS:
                value = base + amp * math.sin(i / 90) + rnd.gauss(0, amp * 0.25)
                if inject == "drift":
                    value += 3.0 * frac
                elif inject == "dip" and 0.30 < frac < 0.42:
                    # 근무 중반에 순도가 잠깐 내려앉는다
                    value -= 0.55 * math.sin((frac - 0.30) / 0.12 * math.pi)
                elif inject == "breakthrough" and frac > 0.6:
                    # 후반에 급상승해 H(3ppm)을 넘긴다
                    value += 4.0 * ((frac - 0.6) / 0.4) ** 2
                w.writerow([ts, tag, round(value, 3)])

    return path, n * len(TAGS)
