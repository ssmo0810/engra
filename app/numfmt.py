"""앱이 찍는 숫자 표기 — 규칙 하나. 엔진 engine/detectors.py 의 `_fmt` 와 같은 규칙이다.

    1,000 이상 쉼표 붙은 정수 · 10 이상 소수 1자리 · 1 이상 2자리 · 1 미만 유효숫자 4자리 고정소수 · 0 은 0 · 지수 표기 없음
    규칙은 반올림한 뒤의 크기로 고른다(0.99996 → 1.00 · 999.95 → 1,000)

엔진 근거 문장의 숫자는 엔진 `_fmt` 가 찍고, 앱이 만드는 숫자(그래프 눈금·호버·최저·최고, 임시 엔진의 근거 문장,
저장 곡선의 반올림 자리수)는 여기서 찍는다. 그래프 눈금을 `:.4g` 로, 엔진이 1 미만 값을 `.4g` 로 찍어
`1.822e+04` · `3.2e-05` 같은 지수 표기가 화면에 나왔다(경모님 지적 2026-09-14).

엔진과 코드를 한 파일로 합치지 않은 이유: 엔진은 앱 없이(engine/selftest.py) 돌고, 앱은 엔진 없이(임시 엔진) 돌아야 한다.
둘이 갈리지 않게 시험(test_app_number_rule_matches_engine)이 같은 값을 두 함수에 넣어 맞춰 본다.
"""
import math


def decimals(v):
    """값 크기에 맞는 소수 자리수 — 반올림한 뒤의 크기로 고른다. 원래 값으로 고르면 0.99996 이 「1」, 999.95 가 「1000.0」 이
    되어 같은 줄의 1.00 · 1,000 과 모양이 갈렸다(반증 워커)."""
    a = abs(v)
    if a == 0:
        return 2
    if a < 1:
        d = 3 - math.floor(math.log10(a))
        if round(a, d) < 1:
            return d
        a = 1.0                                   # 반올림하면 1 이 된다 — 1 이상 규칙으로
    if a < 10 and round(a, 2) < 10:
        return 2
    if a < 1000 and round(a, 1) < 1000:
        return 1
    return 0


def fmt(v, unit="", d=None):
    """숫자 → 표기. d 를 주면 그 자리수로 고정한다(그래프 눈금끼리 자리를 맞출 때). 값이 없으면 ?."""
    if v is None:
        return "?"
    auto = d is None
    if auto:
        if v == 0:
            return f"0{unit}"
        d = decimals(v)
    sep = "," if abs(round(v, d)) >= 1000 else ""
    s = f"{v:{sep}.{d}f}"
    if auto and d > 2:                            # 1 미만은 유효숫자까지만 — 끝의 0 을 뗀다
        s = s.rstrip("0").rstrip(".")
    return s + unit
