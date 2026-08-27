"""데이터만으로 태그 사이 연결(상관·시차)을 추정한다 — 공정을 모르는 상태에서 얼마나 파악되나 (경모님 2026-08-27 Q).

입력: ENGRA_DB 의 raw_sample(적재된 근무 전부). 태그별 5분 평균으로 줄이고, 모든 태그 쌍에 대해 시차 -30~+30분 안에서
피어슨 r 의 최대값을 찾는다. |r| >= THRESH 인 쌍을 '추정 연결' 로 낸다.
비교: docs/tag_master.csv 의 links(임도영님이 공정 지식으로 적은 29개)를 데이터가 얼마나 재발견하는지(재현율)와
추정 연결 중 손으로 적힌 것의 비율(정밀도). 결과는 tools/out/links_inferred.csv.

한계(정직하게): 상관은 인과가 아니다 — 외기 같은 공통 원인에 함께 반응하는 태그도 높게 나온다. 그래서 이건 태그 마스터를
'제안' 하는 도구이고, 확정은 사람이 한다. 또 이번 입력은 결함이 심어진 근무들이라 순수 정상 데이터보다 상관이 과장될 수 있다.
"""
import csv, math, os, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import db  # noqa: E402

THRESH = 0.7
MAX_LAG_MIN = 30
BIN_MIN = 5          # 5분 평균 — 순수 파이썬으로 1,378쌍 × 13시차를 1분 안에 돌리기 위해
LAG_STEP = 5


def minute_series(conn):
    """{tag: {minute_epoch: mean}}"""
    acc = defaultdict(lambda: defaultdict(lambda: [0.0, 0]))
    for tag, ts, v in conn.execute("SELECT tag, ts, value FROM raw_sample"):
        m = int(datetime.fromisoformat(ts).timestamp() // (60 * BIN_MIN))
        a = acc[tag][m]; a[0] += v; a[1] += 1
    return {t: {m: s / n for m, (s, n) in d.items()} for t, d in acc.items()}


def pearson(xs, ys):
    n = len(xs)
    if n < 30:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs)); sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def best_lag(a, b):
    """a(t) 와 b(t+lag) 의 상관이 최대인 lag(분)와 r"""
    best = (0.0, 0)
    keys = sorted(a)
    for lag in range(-MAX_LAG_MIN // BIN_MIN, MAX_LAG_MIN // BIN_MIN + 1, max(1, LAG_STEP // BIN_MIN)):
        xs, ys = [], []
        for k in keys:
            if (k + lag) in b:
                xs.append(a[k]); ys.append(b[k + lag])
        r = pearson(xs, ys)
        if r is not None and abs(r) > abs(best[0]):
            best = (r, lag)
    return best


def hand_links():
    pairs = {}
    with open(ROOT / "docs" / "tag_master.csv", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            for item in (row.get("links") or "").split(","):
                item = item.strip()
                if not item:
                    continue
                tgt = item.split(":")[0].strip()
                pairs[frozenset((row["tag"], tgt))] = item
    return pairs


def main():
    with db.connect() as conn:
        series = minute_series(conn)
    tags = sorted(series)
    print(f"태그 {len(tags)}개 · 분 단위 표본 {sum(len(s) for s in series.values()):,}점")
    found = []
    for i, a in enumerate(tags):
        for b in tags[i + 1:]:
            r, lag = best_lag(series[a], series[b])
            if abs(r) >= THRESH:
                found.append((a, b, round(r, 3), lag))
    found.sort(key=lambda x: -abs(x[2]))
    hand = hand_links()
    fset = {frozenset((a, b)) for a, b, _, _ in found}
    hit = [k for k in hand if k in fset]
    out = ROOT / "tools" / "out"; out.mkdir(exist_ok=True)
    with open(out / "links_inferred.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh); w.writerow(["tag_a", "tag_b", "r", "lag_min", "in_tag_master"])
        for a, b, r, lag in found:
            w.writerow([a, b, r, lag * BIN_MIN, "Y" if frozenset((a, b)) in hand else ""])
    print(f"|r| >= {THRESH} 추정 연결 {len(found)}쌍 → {out / 'links_inferred.csv'}")
    print(f"태그 마스터 손 연결 {len(hand)}개 중 데이터가 재발견 {len(hit)}개 (재현율 {100*len(hit)/len(hand):.0f}%)")
    print(f"추정 연결 중 손 연결과 일치 {len(hit)}개 (정밀도 {100*len(hit)/len(found):.0f}%)" if found else "")
    miss = [hand[k] for k in hand if k not in fset]
    print("재발견 못 한 손 연결:", miss[:12])
    print("상위 추정(손 연결 아님) 8개:", [(a, b, r, lag * BIN_MIN) for a, b, r, lag in found if frozenset((a, b)) not in hand][:8])


if __name__ == "__main__":
    main()
