"""ENGRA 초안 채택률 설문 응답 집계.

응답 시트를 CSV 로 받아 안건별 채택률·제외 사유·자유 의견을 정리한다.

주의 — 시트에 컬럼이 여러 벌 있다.
폼을 다시 지을 때마다 구글이 새 문항을 뒤에 덧붙이고 옛 컬럼을 남겨 둔다.
지금 응답이 들어오는 것은 「채택하시겠습니까?」 계열 한 벌뿐이고 나머지는 빈 칸이다.
문항 문구를 또 바꾸면 여기 SELECTOR 도 같이 바꿔야 한다.
"""
import csv
import io
import json
import sys

SELECTOR = "채택하시겠습니까"          # 현재 살아 있는 문항을 고르는 말
FREE_PREFIX = "ENGRA 를 써 보니"
ADOPT = "채택한다"

TITLES = [
    ("상", "AI-707 순도 하강 드리프트"),
    ("중", "SI-507 터빈 회전수 순간 이탈"),
    ("상", "LI-701 HP 레벨 급상승"),
    ("상", "ZI-805 밸브 개도 상승"),
    ("상", "ZI-201–FI-204 연동 끊김"),
]


def load(path):
    raw = open(path, encoding="utf-8-sig").read()
    # 브라우저에서 받아 오면 JSON 문자열 한 덩어리로 저장된다 — 벗겨 낸다.
    try:
        val = json.loads(raw)
        if isinstance(val, str):
            raw = val
    except ValueError:
        pass
    rows = list(csv.reader(io.StringIO(raw)))
    return rows[0], [r for r in rows[1:] if any(x.strip() for x in r)]


def main(path):
    hdr, data = load(path)
    cur = [i for i, h in enumerate(hdr) if SELECTOR in h]
    free = next((i for i, h in enumerate(hdr) if h.startswith(FREE_PREFIX)), None)

    print(f"응답 {len(data)}건   (시트 컬럼 {len(hdr)}개 중 현재 문항 {len(cur)}개 사용)")
    if not data:
        print("아직 응답이 없습니다.")
        return
    print()
    print("안건별 채택률")
    print("─" * 70)

    adopted_total = 0
    for n, ci in enumerate(cur, 1):
        vals = [r[ci].strip() for r in data if ci < len(r) and r[ci].strip()]
        a = sum(1 for v in vals if v.startswith(ADOPT))
        adopted_total += a
        pct = a / len(vals) * 100 if vals else 0
        bar = "█" * round(pct / 10) + "·" * (10 - round(pct / 10))
        sev, name = TITLES[n - 1] if n <= len(TITLES) else ("-", f"안건 {n}")
        print(f"  {n}. [{sev}] {name:<26} {bar} {a}/{len(vals)}  {pct:3.0f}%")
        for r in data:                       # 이유는 바로 다음 컬럼
            ri = ci + 1
            if ri < len(r) and r[ri].strip():
                mark = "채택" if r[ci].strip().startswith(ADOPT) else "제외"
                print(f"        └ ({mark}) {r[ri].strip()}")

    den = len(cur) * len(data)
    print("─" * 70)
    print(f"  전체 채택률  {adopted_total}/{den} = {adopted_total / den * 100:.0f}%")

    print()
    print("자유 의견")
    print("─" * 70)
    for i, r in enumerate(data, 1):
        v = r[free].strip() if (free is not None and free < len(r)) else ""
        print(f"  {i}. {v if v else '(무응답)'}")

    print()
    print("제출 시각: " + ", ".join(r[0] for r in data))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "responses.csv")
