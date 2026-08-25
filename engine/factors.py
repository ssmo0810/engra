"""6시그마 기반 이상감지 기준 — CTQ 공정능력과 핵심인자(Vital Few) 도출.

    python3 engine/factors.py                      # 개발용 픽스처로
    python3 engine/factors.py --csv data.csv       # 실제 근무 CSV 로
    python3 engine/factors.py --ctq AI-707,LI-705  # CTQ 를 직접 지정
    python3 engine/factors.py --json out.json      # 기준표를 파일로

---

**왜 6시그마인가.** "ASU 가 잘 돌고 있는가" 를 한 문장으로 답하려면 먼저
**무엇을 재서 답할 것인지**를 정해야 한다. 6시그마는 그 대상을 CTQ(Critical To
Quality) 라 부르고, 나머지 태그는 그 CTQ 를 움직이는 인자 X 로 본다.

    Y = f(X₁, X₂, … Xn)

우리가 정한 Y 는 **LP Column 4종** — 레벨·차압·순도·온도. 제품 품질과 탑 안정도가
여기서 결정되기 때문이다. 이 파일이 하는 일은 둘이다.

1. **Y 의 관리기준을 만든다** — 중심 μ, 산포 σ, 관리한계 μ±3σ, 공정능력 Cp/Cpk.
   관리한계는 **알람선이 아니다.** 알람선은 설비를 지키는 선이고, 관리한계는
   "평소와 달라졌다" 를 판정하는 선이다. ENGRA 가 잡으려는 것이 정확히 그 사이다.

2. **X 를 순위로 세운다** — 지연을 반영한 상관, 다중회귀 기여도, 파레토.
   운전 경험에서 나온 가설(예: 대기 습도·온도)이 실제로 상위에 오는지 확인한다.

**주의 — 이 분석은 데이터에 있는 것만 찾아낸다.** 모의 데이터의 태그 간 인과는
`docs/tag_master.csv` 의 `links` 칸이 전부 정의한다. links 에 없는 관계는 회귀에서
0 으로 나오는데, 그것은 "물리적으로 무관하다" 가 아니라 **"생성기가 그 관계를
만들지 않았다"** 는 뜻이다. 실측 데이터가 아닌 이상 이 구분을 흐리면 안 된다.
"""
import argparse
import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.core import (BLOCK_SEC, MAD_TO_SIGMA, TAGS, build_views, mad,  # noqa: E402
                         median, ols, sigma, spec_of)

# LP Column — "운전이 잘 되고 있는가" 를 대표하는 네 개.
CTQ = ("LI-705", "PDI-706", "AI-707", "TI-708")

# 운전 경험에서 나온 가설. 상위에 오르는지 따로 표시한다.
HYPOTHESIS = ("TI-101", "MI-102")

MAX_LAG_BLOCKS = 20        # 0~600초. tag_master 의 최대 지연이 600초다
MIN_R = 0.30               # 이 아래는 인자로 보지 않는다
MAX_FACTORS = 5            # 다중회귀에 넣을 상한
VITAL_R2 = 0.80            # 누적 설명력 80% 까지가 Vital Few

DIFF_BLOCKS = 5            # 2.5분 간격 차분. 아래 설명 참고
SUBGROUP_BLOCKS = 20       # 10분. 관리도 한 점의 간격


# ── 이 파일에서 가장 중요한 두 가지 보정 ────────────────────────────────
#
# ① **레벨 상관을 그대로 믿으면 안 된다.**
#    운전 데이터는 자기상관이 극단적으로 높다. 서로 아무 관계 없는 두 태그도
#    각자 완만하게 오르내리기만 하면 12시간 창에서 r=0.7 이 그냥 나온다
#    (허위회귀, spurious regression). 실제로 이 코드의 첫 판에서 LI-705 의
#    1위 인자로 MI-302 가 r=0.737 로 올라왔는데, 태그 마스터에 **둘 사이
#    연결이 아예 없다.** 둘 다 매끄러운 곡선이라는 것 말고는 공통점이 없었다.
#
#    그래서 **차분 상관**을 주 지표로 쓴다. "X 가 흔들렸을 때 Y 가 따라
#    흔들리는가" 가 원래 우리가 묻고 싶은 것이고, 그것이 곧 차분이다.
#    느린 공통 추세는 차분에서 사라진다.
#
# ② **유효표본수를 따로 센다.**
#    1,440점처럼 보여도 자기상관 때문에 독립 정보는 수십 개뿐이다.
#    유효표본수로 계산한 임계 상관(r_crit)을 넘지 못하면 인자로 보지 않는다.


# --- 통계 ------------------------------------------------------------

def _phi(z):
    """표준정규 누적분포."""
    return 0.5 * math.erfc(-z / math.sqrt(2))


def _z(xs):
    """표준화. 산포가 0이면 None (회귀에 넣을 수 없다)."""
    m = median(xs)
    s = mad(xs, m) * MAD_TO_SIGMA
    if s <= 0:
        return None
    return [(x - m) / s for x in xs]


def _corr(xs, ys):
    n = len(xs)
    xm, ym = sum(xs) / n, sum(ys) / n
    sxy = sxx = syy = 0.0
    for i in range(n):
        dx, dy = xs[i] - xm, ys[i] - ym
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


def _diff(xs, d=DIFF_BLOCKS):
    """d블록 간격 차분. 느린 공통 추세를 없애고 '흔들림' 만 남긴다."""
    return [xs[i + d] - xs[i] for i in range(len(xs) - d)]


def _lag1(xs):
    return _corr(xs[:-1], xs[1:]) if len(xs) > 2 else 0.0


def _n_eff(xs, ys):
    """자기상관을 반영한 유효표본수 (Bartlett 근사)."""
    rx, ry = _lag1(xs), _lag1(ys)
    f = (1 - rx * ry) / (1 + rx * ry) if abs(rx * ry) < 1 else 0.0
    return max(4.0, len(xs) * max(f, 0.0))


def _r_crit(n_eff, alpha_z=1.96):
    """유효표본수에서 나오는 95% 임계 상관. 이걸 못 넘으면 우연이다."""
    if n_eff <= 4:
        return 1.0
    return math.tanh(alpha_z / math.sqrt(n_eff - 3))


def _solve(a, b):
    """가우스 소거. 인자 5개 이하라 이 정도면 충분하다."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r == c:
                continue
            f = m[r][c] / m[c][c]
            for k in range(c, n + 1):
                m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def _multi_r2(y, cols):
    """표준화된 설명변수들로 y 를 설명한 R² 와 표준화 회귀계수."""
    k = len(cols)
    if not k:
        return 0.0, []
    a = [[sum(ci[t] * cj[t] for t in range(len(y))) for cj in cols] for ci in cols]
    b = [sum(ci[t] * y[t] for t in range(len(y))) for ci in cols]
    beta = _solve(a, b)
    if beta is None:
        return 0.0, []
    sse = 0.0
    for t in range(len(y)):
        pred = sum(beta[i] * cols[i][t] for i in range(k))
        sse += (y[t] - pred) ** 2
    sst = sum(v * v for v in y)
    return (1 - sse / sst if sst else 0.0), beta


# --- 데이터 ----------------------------------------------------------

def load_csv(path):
    """연결 규약 ① 형식(timestamp,tag,value) -> {태그: [(ts, value)]}."""
    series = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                series.setdefault(row["tag"], []).append(
                    (row["timestamp"], float(row["value"])))
            except (KeyError, ValueError, TypeError):
                continue
    for points in series.values():
        points.sort()
    return series


def aligned(views):
    """태그마다 블록 시각이 다를 수 있으므로 공통 시각축으로 맞춘다."""
    per = {t: {b.t0: b.med for b in v.blocks} for t, v in views.items()}
    common = None
    for stamps in per.values():
        keys = set(stamps)
        common = keys if common is None else (common & keys)
    axis = sorted(common or ())
    return axis, {t: [s[k] for k in axis] for t, s in per.items()}


# --- 1. 공정능력과 관리기준 -------------------------------------------

def subgroup(values, k=SUBGROUP_BLOCKS):
    """관리도 한 점 = 10분 중앙값.

    30초 값을 그대로 관리도에 올리면 안 된다. 연속공정 데이터는 옆 점과 거의
    같은 값이라, '연속 9점이 중심선 한쪽' 같은 규칙이 근무마다 수백 번 걸린다
    (첫 판에서 실제로 1,100회 나왔다). 규칙들은 **점들이 서로 독립**이라는
    가정 위에 서 있으므로, 합리적 부분군으로 묶어 간격을 벌려야 한다.
    """
    return [median(values[i:i + k]) for i in range(0, len(values) - k + 1, k)]


def capability(tag, values):
    """CTQ 하나의 관리기준. 이것이 곧 '이상감지 기준' 의 본체다.

    산포를 두 가지로 잰다 — 6시그마의 표준 구분이다.

      단기 σ (이동범위 기반)  → 공정 **능력** Cp/Cpk. 설비가 낼 수 있는 실력
      장기 σ (전체 산포)      → 공정 **성능** Pp/Ppk. 실제로 낸 성적

    둘이 벌어져 있으면 설비가 아니라 **운전이 흔들린 것**이고, 그것이 바로
    인수인계에 적어야 할 내용이다.
    """
    spec = spec_of(tag)
    pts = subgroup(values)
    mu = median(pts)
    lsl, usl = spec.get("normal_lo"), spec.get("normal_hi")

    mr = [abs(pts[i] - pts[i - 1]) for i in range(1, len(pts))]
    sd_st = (sum(mr) / len(mr) / 1.128) if mr else 0.0     # I-MR 표준 상수 d2
    sd_lt = mad(pts, mu) * MAD_TO_SIGMA

    out = {
        "tag": tag,
        "description": spec.get("description"),
        "unit": spec.get("unit"),
        "n_blocks": len(values),
        "n_points": len(pts),
        "center": mu,
        "sigma_short": sd_st,
        "sigma_long": sd_lt,
        # 관리한계 — 평소와 달라졌는지 판정하는 선 (알람선과 다르다)
        "UCL": mu + 3 * sd_st,
        "LCL": mu - 3 * sd_st,
        "USL": usl, "LSL": lsl,
        "alarm_H": spec.get("H"), "alarm_L": spec.get("L"),
    }
    if lsl is not None and usl is not None:
        if sd_st > 0:
            cpu, cpl = (usl - mu) / (3 * sd_st), (mu - lsl) / (3 * sd_st)
            out["Cp"] = (usl - lsl) / (6 * sd_st)
            out["Cpk"] = min(cpu, cpl)
            out["Z_st"] = 3 * out["Cpk"]
            out["Z_lt"] = out["Z_st"] - 1.5        # 관례적 1.5σ 이동
            out["DPMO"] = 1e6 * (_phi(-3 * cpu) + _phi(-3 * cpl))
        if sd_lt > 0:
            out["Pp"] = (usl - lsl) / (6 * sd_lt)
            out["Ppk"] = min((usl - mu) / (3 * sd_lt), (mu - lsl) / (3 * sd_lt))
    return out


NELSON = (
    ("규칙1", "1점이 관리한계(3σ) 밖"),
    ("규칙2", "연속 9점이 중심선 한쪽"),
    ("규칙3", "연속 6점이 계속 증가 또는 감소"),
    ("규칙5", "연속 3점 중 2점이 2σ 밖"),
)


def control_violations(values, mu, sd):
    """개별값 관리도 위반. 통계적 공정관리의 표준 규칙이라 현장 수용성이 높다.

    우리 검출기와 목적이 같다 — 규칙1은 `이탈`, 규칙2·3은 `드리프트`·`레벨시프트`
    에 대응한다. 같은 것을 6시그마 언어로 말한 것이고, **기준을 설명할 때 이쪽이
    훨씬 잘 통한다.**
    """
    if sd <= 0:
        return {}
    n = len(values)
    hit = {k: 0 for k, _d in NELSON}
    for v in values:
        if abs(v - mu) > 3 * sd:
            hit["규칙1"] += 1
    run = 0
    for i in range(n):
        side = 1 if values[i] > mu else -1
        run = run + 1 if i and side == (1 if values[i - 1] > mu else -1) else 1
        if run >= 9:
            hit["규칙2"] += 1
    up = dn = 0
    for i in range(1, n):
        up = up + 1 if values[i] > values[i - 1] else 0
        dn = dn + 1 if values[i] < values[i - 1] else 0
        if up >= 6 or dn >= 6:
            hit["규칙3"] += 1
    for i in range(2, n):
        w = values[i - 2:i + 1]
        for s in (1, -1):
            if sum(1 for v in w if s * (v - mu) > 2 * sd) >= 2:
                hit["규칙5"] += 1
                break
    return hit


# --- 2. 인자 분석 -----------------------------------------------------

def scan_factors(y, cols, max_lag=MAX_LAG_BLOCKS):
    """지연을 훑어 각 인자의 최대 상관과 그때의 지연을 찾는다.

    ASU 는 앞단 변화가 탑에 닿기까지 시간이 걸린다. 지연을 안 보고 동시각으로만
    상관을 재면 **실제로 영향을 주는 인자를 무관한 것으로 판정한다.**
    """
    out = []
    n = len(y)
    dy = _diff(y)
    for tag, xs in cols.items():
        best = None
        for lag in range(0, max_lag + 1):
            if n - lag < 60:
                break
            x_al = xs[:n - lag] if lag else xs[:n]
            y_al = y[lag:] if lag else y
            m = min(len(x_al), len(y_al))
            rd = _corr(_diff(x_al[:m]), _diff(y_al[:m]))
            if best is None or abs(rd) > abs(best[0]):
                best = (rd, lag, _corr(x_al[:m], y_al[:m]))
        rd, lag, rl = best
        x_al = xs[:n - lag] if lag else xs[:n]
        y_al = y[lag:] if lag else y
        m = min(len(x_al), len(y_al))
        ne = _n_eff(_diff(x_al[:m]), _diff(y_al[:m]))
        out.append({
            "tag": tag,
            "description": spec_of(tag).get("description"),
            "r": rd,                       # 주 지표 — 차분 상관
            "r_level": rl,                 # 참고 — 레벨 상관 (허위회귀 주의)
            "lag_sec": lag * BLOCK_SEC,
            "n_eff": round(ne, 1),
            "r_crit": round(_r_crit(ne), 3),
            "significant": abs(rd) > _r_crit(ne),
            "declared": False,             # analyze() 에서 갱신
        })
    out.sort(key=lambda d: -abs(d["r"]))
    return out


def declared_drivers(ctq):
    """태그 마스터 links 가 선언한 '이 CTQ 로 들어오는' 인자."""
    got = []
    for src, spec in TAGS.items():
        for tgt, gain, lag in spec.get("links", ()):
            if tgt == ctq:
                got.append((src, gain, lag))
    return got


def vital_few(y, cols, ranked):
    """전진선택으로 누적 설명력이 80% 에 닿을 때까지 인자를 더한다.

    상관 상위 몇 개를 그냥 쓰지 않는 이유는, 서로 비슷하게 움직이는 인자를 같이
    넣으면 **같은 설명을 두 번 세기** 때문이다.
    """
    chosen, chosen_cols, trail = [], [], []
    pool = [d for d in ranked if abs(d["r"]) >= MIN_R and d["significant"]]
    prev = 0.0
    while pool and len(chosen) < MAX_FACTORS:
        best = None
        for d in pool:
            lag = d["lag_sec"] // BLOCK_SEC
            xs = cols[d["tag"]]
            x = xs[:len(y) - lag] if lag else xs[:len(y)]
            yy = y[lag:] if lag else y
            m = min(len(x), len(yy))
            trial = [c[-m:] for c in chosen_cols] + [x[:m]]
            r2, beta = _multi_r2(yy[:m], trial)
            if best is None or r2 > best[0]:
                best = (r2, d, x[:m], beta, m)
        r2, d, col, beta, m = best
        if r2 - prev < 0.01:
            break
        chosen.append(dict(d, delta_r2=r2 - prev, cum_r2=r2,
                           beta=beta[-1] if beta else 0.0))
        chosen_cols = [c[-m:] for c in chosen_cols] + [col]
        y = y[:m] if len(y) > m else y
        trail.append(r2)
        pool = [p for p in pool if p["tag"] != d["tag"]]
        prev = r2
        if r2 >= VITAL_R2:
            break
    return chosen


def analyze(series, ctq_tags=CTQ, hypothesis=HYPOTHESIS):
    views = build_views(series)
    axis, cols = aligned(views)
    if len(axis) < 120:
        raise SystemExit(f"블록이 {len(axis)}개뿐입니다. 최소 1시간 분량이 필요합니다.")

    report = {"blocks": len(axis), "block_sec": BLOCK_SEC, "ctq": []}
    for tag in ctq_tags:
        if tag not in cols:
            continue
        raw = cols[tag]
        cap = capability(tag, raw)
        cap["nelson"] = control_violations(subgroup(raw), cap["center"],
                                           cap["sigma_short"])
        cap["declared_drivers"] = declared_drivers(tag)

        y = _z(raw)
        if y is None:
            cap["note"] = "산포가 0이라 인자 분석 불가"
            report["ctq"].append(cap)
            continue

        others = {}
        for t, vals in cols.items():
            if t == tag:
                continue
            z = _z(vals)
            if z is not None:
                others[t] = z

        ranked = scan_factors(y, others)
        for d in ranked:
            d["declared"] = any(s == d["tag"] for s, _g, _l in cap["declared_drivers"])
        cap["ranked"] = ranked
        cap["vital_few"] = vital_few(y, others, ranked)
        cap["hypothesis"] = [d for d in ranked if d["tag"] in hypothesis]
        cap["n_significant"] = sum(1 for d in ranked
                                   if abs(d["r"]) >= MIN_R and d["significant"])
        report["ctq"].append(cap)
    return report


# --- 출력 ------------------------------------------------------------

def _num(v, nd=3):
    return "-" if v is None else f"{v:,.{nd}f}"


def show(report):
    print(f"\n블록 {report['blocks']}개 × {report['block_sec']}초 "
          f"= {report['blocks'] * report['block_sec'] / 3600:.1f}시간\n")

    for cap in report["ctq"]:
        u = cap.get("unit") or ""
        print("=" * 78)
        print(f"CTQ  {cap['tag']}  {cap['description']}  [{u}]")
        print("=" * 78)
        print(f"  중심 μ {_num(cap['center'])}{u}   "
              f"단기 σ {_num(cap['sigma_short'], 4)}{u}   "
              f"장기 σ {_num(cap['sigma_long'], 4)}{u}")
        print(f"  관리도 점 {cap['n_points']}개 (10분 부분군)")
        print(f"  관리한계  LCL {_num(cap['LCL'])} ~ UCL {_num(cap['UCL'])}{u}"
              f"   ← 평소와 달라졌는지 보는 선")
        print(f"  정상범위  LSL {_num(cap['LSL'])} ~ USL {_num(cap['USL'])}{u}")
        print(f"  알람선    L {_num(cap['alarm_L'])} / H {_num(cap['alarm_H'])}{u}"
              f"   ← 설비를 지키는 선")
        if "Cpk" in cap:
            print(f"  공정능력  Cp {cap['Cp']:.2f}  Cpk {cap['Cpk']:.2f}  "
                  f"→ {cap['Z_st']:.1f}σ 수준 (장기 {cap['Z_lt']:.1f}σ, "
                  f"DPMO {cap['DPMO']:.1f})")
        if "Ppk" in cap:
            print(f"  공정성능  Pp {cap['Pp']:.2f}  Ppk {cap['Ppk']:.2f}"
                  + ("   ← 능력 대비 성능 저하 = 운전 변동"
                     if cap.get("Cpk", 0) - cap["Ppk"] > 0.3 else ""))
        viol = cap.get("nelson") or {}
        if any(viol.values()):
            hit = ", ".join(f"{k} {v}회" for k, v in viol.items() if v)
            print(f"  관리도 위반  {hit}")
        else:
            print("  관리도 위반  없음")

        if cap.get("note"):
            print(f"  ! {cap['note']}")
            continue

        print(f"\n  [Y = f(X)]  선언된 인자: "
              f"{', '.join(s for s, _g, _l in cap['declared_drivers']) or '없음'}")
        top = [d for d in cap["ranked"][:8]]
        print(f"  {'태그':<9} {'설명':<17} {'차분r':>7} {'레벨r':>7} "
              f"{'지연':>6} {'유효N':>7} {'임계':>6}  유의  선언")
        print("  " + "-" * 74)
        for d in top:
            print(f"  {d['tag']:<9} {(d['description'] or '')[:16]:<17} "
                  f"{d['r']:>7.3f} {d['r_level']:>7.3f} "
                  f"{d['lag_sec']:>5.0f}s {d['n_eff']:>7.1f} {d['r_crit']:>6.3f}  "
                  f"{'O' if d['significant'] else '·':<4}  "
                  f"{'O' if d['declared'] else '·'}")

        vf = cap.get("vital_few") or []
        if vf:
            print(f"\n  핵심인자 (Vital Few) — 누적 설명력 {vf[-1]['cum_r2'] * 100:.0f}%")
            for i, d in enumerate(vf, 1):
                print(f"   {i}. {d['tag']} {d['description']}  "
                      f"β*={d['beta']:+.3f}  기여 +{d['delta_r2'] * 100:.0f}%p  "
                      f"(지연 {d['lag_sec']:.0f}초)")
        else:
            print(f"\n  핵심인자 없음 — |r| {MIN_R} 이상인 인자가 하나도 없습니다.")

        hy = cap.get("hypothesis") or []
        if hy:
            print("\n  [가설 확인]")
            for d in hy:
                ok = abs(d["r"]) >= MIN_R and d["significant"]
                verdict = "지지됨" if ok else "**기각**"
                print(f"   {d['tag']} {d['description']}: "
                      f"차분r={d['r']:+.3f} (임계 {d['r_crit']:.3f}) → {verdict}")
    print()


def main(argv=None):
    ap = argparse.ArgumentParser(description="6시그마 CTQ 공정능력·핵심인자 분석")
    ap.add_argument("--csv", help="근무 구간 CSV (timestamp,tag,value)")
    ap.add_argument("--ctq", help="CTQ 태그 (쉼표 구분)", default=",".join(CTQ))
    ap.add_argument("--hypo", help="검증할 가설 인자", default=",".join(HYPOTHESIS))
    ap.add_argument("--json", help="기준표를 JSON 으로 저장")
    args = ap.parse_args(argv)

    if args.csv:
        series = load_csv(args.csv)
    else:
        from engine import _fixture
        print("입력이 없어 개발용 픽스처로 돕니다 (--csv 로 실제 데이터 지정).")
        series, _ans = _fixture.generate([], seed=1)

    report = analyze(series,
                     tuple(t.strip() for t in args.ctq.split(",") if t.strip()),
                     tuple(t.strip() for t in args.hypo.split(",") if t.strip()))
    show(report)
    if args.json:
        Path(args.json).write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=float),
            encoding="utf-8")
        print(f"기준표를 {args.json} 에 저장했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
