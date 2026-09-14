"""증상별 검출기 7종.

각 검출기는 `TagView` 하나(또는 연결된 두 개)를 받아 이벤트 목록을 돌려준다.
이벤트는 `app/README.md` 연결 규약 ②의 형식을 그대로 따른다.

    tag · start_ts · end_ts · kind · severity · score · metrics · evidence

**문턱값을 정한 근거는 각 함수의 주석에 남겨 두었다.** 초기값을 잡고 측정하며
조정한다는 `engine/README.md` 의 방침에 따라, 왜 그 숫자인지 모르면 조정할 수
없기 때문이다. 실측 후 조정한 값은 `PARAMS` 한 곳에서 바꾼다.

**정상 데이터에도 20~60분 주기의 느린 진동이 들어 있다**(생성기 `series()` 의
drift 성분). 이것을 이상으로 잡으면 매 근무 오탐이 쏟아진다. 드리프트 검출이
단조성(|r|)을 함께 요구하고, 헌팅 검출이 값이 아니라 변동폭을 보는 이유가
전부 여기에 있다.
"""
import math
from statistics import StatisticsError

from engine.core import (BLOCK_SEC, MAD_TO_SIGMA, label, mad, median, mod_z,
                         ols, pearson, sigma, spec_of, unit_of)

# --- 문턱값 한 곳 -----------------------------------------------------
# engine/README.md "채워야 할 것" 표의 확정값.

PARAMS = {
    # 드리프트 — 창 길이(분)를 여러 개 두고 가장 강한 것을 채택한다.
    # **30분 창은 뺐다.** 정상 데이터의 진동 주기가 20~60분이라, 30분 창은
    # 그 반주기를 통째로 담아 단조 상승처럼 보인다. 실측에서 오탐 7건이
    # 전부 30분 창에서 나왔다. 1시간부터는 진동이 되돌아오므로 |r| 이 떨어진다.
    "drift_windows_min": (60, 120, 240, 720),
    "drift_k": 3.5,          # 창 전체 변화량이 평소 산포의 몇 배여야 하는가
    "drift_r": 0.60,         # 단조성. 느린 진동을 걸러내는 장치
    # 헌팅 — 블록 안 변동폭이 평소의 몇 배인가
    "hunt_ratio": 3.0,
    "hunt_min_blocks": 2,    # 60초. 시나리오 3의 1회 지속이 60초라 그 이하로 못 내린다
    # 순간 이탈 — 국소 중앙값 대비 몇 σ 인가
    "spike_z": 8.0,
    "spike_max_blocks": 4,   # 2분을 넘으면 스파이크가 아니라 다른 현상이다
    # 레벨 시프트
    "step_k": 3.0,
    "step_min_blocks": 20,   # 앞뒤 각 10분은 있어야 단계 변화라 부를 수 있다
    # 임계 근접 — 정상값에서 한계까지 중 몇 %를 소진했는가
    "prox_frac": 0.85,
    "prox_min_blocks": 60,   # 30분
    # 고착
    "stuck_frac": 0.05,      # 블록 변동폭이 평소 노이즈의 5% 미만이면 멈춘 것으로 본다
    "stuck_min_blocks": 40,  # 20분
    # 상관 이탈
    "resid_z": 3.0,
    "resid_win_blocks": 40,  # 20분 평균
    "resid_min_blocks": 40,
    "resid_min_r": 0.50,     # 평소에 이만큼도 안 붙어 있는 쌍은 붕괴를 논할 수 없다
}

SEV_HIGH, SEV_MID, SEV_LOW = "상", "중", "하"


def _pct_span(tag, amount):
    """변화량을 그 태그 **정상범위 폭 대비 %** 로 환산한다.

    근무자가 읽는 것은 σ 나 배수가 아니라 "정상범위를 얼마나 먹었는가" 다
    (임도영 2026-09-14). 정상범위가 태그 마스터에 없으면 None — 그때는 % 를 적지 않는다.
    판정은 그대로 평소 산포 대비로 하고, **표시만** 바꾼다.
    """
    if amount is None:
        return None
    span = spec_of(tag).get("span")
    if not span:
        return None
    return abs(amount) / span * 100.0


def _round_pct(tag, amount):
    """metrics 에 실을 값. AI 프롬프트가 이것만 받고 σ 는 못 본다."""
    p = _pct_span(tag, amount)
    return None if p is None else round(p)


def _pct_txt(tag, amount, lead="정상범위 폭의"):
    """정상범위 폭 대비 표기. 1,000% 를 넘으면 배수로 — 분석기 순간 이상값처럼
    정상값의 2,000배가 튀는 태그에서 「71,323%」 가 나와 읽을 수 없었다(실측 2026-09-14)."""
    p = _pct_span(tag, amount)
    if p is None:
        return None
    if p >= 1000:
        return f"{lead} {p / 100:,.0f}배"
    return f"{lead} {p:.0f}%"


def _paren(*parts):
    """None 을 걸러 ' (a, b)' 로. 남는 것이 없으면 빈 문자열."""
    keep = [p for p in parts if p]
    return f" ({', '.join(keep)})" if keep else ""


def _fmt(v, unit=""):
    """수치를 사람이 읽는 자리수로. 아주 작은 값이 0으로 보이지 않게 한다.

    1,000 이상 쉼표 정수 · 10 이상 소수 1자리 · 1 이상 2자리 · 1 미만 유효숫자 4자리 고정소수 · 0 은 0.
    1 미만을 `.4g` 로 찍던 때는 0.0001 보다 작은 값이 3.2e-05 같은 지수 표기로 근거 문장·화면·정적 스냅숏·AI 입력에 실렸다
    (경모님 지적 2026-09-14). 규칙은 **반올림한 뒤의 크기**로 고른다 — 원래 값으로 고르면 0.99996 이 「1」, 999.95 가
    「1000.0」 이 되어 같은 줄의 1.00 · 1,000 과 모양이 갈렸다(반증 워커). app/numfmt.fmt 와 같은 규칙이다.
    """
    if v is None:
        return "?"
    if v == 0:
        return f"0{unit}"
    a = abs(v)
    if a < 1:
        d = 3 - math.floor(math.log10(a))
        if round(a, d) < 1:
            return f"{v:.{d}f}".rstrip("0").rstrip(".") + unit
        a = 1.0                                   # 반올림하면 1 이 된다 — 1 이상 규칙으로
    if a < 10 and round(a, 2) < 10:
        return f"{v:.2f}{unit}"
    if a < 1000 and round(a, 1) < 1000:
        return f"{v:.1f}{unit}"
    return f"{v:,.0f}{unit}"


def _event(view, i, j, kind, severity, score, metrics, evidence):
    start_ts, end_ts = view.span_ts(i, j)
    return {
        "tag": view.tag,
        "kind": kind,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "severity": severity,
        "score": round(float(score), 3),
        "metrics": dict(metrics, blocks=j - i,
                        duration_min=round((j - i) * BLOCK_SEC / 60, 1)),
        "evidence": evidence,
    }


def _runs(flags, min_len):
    """참인 구간 중 min_len 이상인 것만 [(시작, 끝)] 으로."""
    out = []
    i = 0
    n = len(flags)
    while i < n:
        if not flags[i]:
            i += 1
            continue
        j = i
        while j < n and flags[j]:
            j += 1
        if j - i >= min_len:
            out.append((i, j))
        i = j
    return out


def _sev_by_z(z, high=10.0, mid=5.0):
    if z >= high:
        return SEV_HIGH
    if z >= mid:
        return SEV_MID
    return SEV_LOW


# --- 1. 알람 한계선 초과 ----------------------------------------------

def limit_crossings(view):
    """LL/L/H/HH 를 실제로 넘은 구간.

    임시 엔진이 하던 일을 그대로 가져왔다. **알람이 울린 건도 인수인계장에
    들어가야 하기 때문이다**(`docs/scenarios.md`). 근무자가 이미 아는 사건이라도
    빠지면 똑같이 누락이다. 다만 이건 ENGRA 의 가치가 아니라 최소한의 바닥이다.
    """
    spec, unit = view.spec, unit_of(view.tag)
    events = []
    for key, above, sev in (("HH", True, SEV_HIGH), ("H", True, SEV_MID),
                            ("LL", False, SEV_HIGH), ("L", False, SEV_MID)):
        limit = spec.get(key)
        if limit is None:
            continue
        flags = [(b.hi > limit) if above else (b.lo < limit) for b in view.blocks]
        runs = _runs(flags, 1)
        if not runs:
            continue
        i, j = runs[0][0], runs[-1][1]
        seg = view.blocks[i:j]
        peak = max(b.hi for b in seg) if above else min(b.lo for b in seg)
        direction = "초과" if above else "미달"
        events.append(_event(
            view, i, j, f"{key} {direction}", sev, abs(peak - limit),
            {"limit": limit, "peak": round(peak, 4), "unit": unit,
             "episodes": len(runs), "alarm": True},
            f"{label(view.tag)} {key} 한계 {_fmt(limit, unit)} {direction} — "
            f"{'최고' if above else '최저'} {_fmt(peak, unit)}",
        ))
        break      # 같은 방향에서 더 심한 것 하나만 남긴다
    return events


# --- 2. 드리프트 ------------------------------------------------------

def _cusum_narrow(src, i, j, s_adj):
    """넓은 창에서 잡은 드리프트의 **보고 구간**만 좁힌다. 검출 여부는 안 바꾼다.

    드리프트 검출은 창을 통째로 놓고 회귀선을 그어 판정하므로, 결과 구간이 그 창 전체가
    된다 — 「12시간 내내 이상」 이 아니라 「12시간 창으로 봤다」 는 뜻인데 읽는 사람에게는
    구분이 안 된다. 실측(2026-08-29)으로 검출 구간이 정답의 2.2배를 덮었다(시간축 정밀도 0.40).

    창 앞쪽을 기준선으로 잡고 CUSUM 을 누적해, **한 방향으로 벌어지기 시작한 지점**을 찾는다.
    O(n) 이고 난수가 없어 재현성이 깨지지 않는다.

    못 좁히겠으면 원래 구간을 그대로 돌려준다 — 좁히는 것은 부가 기능이지 판정이 아니다.
    """
    n = j - i
    if n < 20 or s_adj <= 0:
        return i, j
    head = max(5, n // 8)                       # 창 앞 1/8 을 기준선으로 본다
    base = median(src[i:i + head])
    slack = 0.5 * s_adj                         # 이 정도 흔들림은 흘려보낸다
    up = src[j - 1] >= base
    pos = 0.0
    start = None
    for t in range(i + head, j):
        d = (src[t] - base) if up else (base - src[t])
        pos = max(0.0, pos + d - slack)
        if start is None and pos > 0:
            start = t                           # 벌어지기 시작한 자리 후보
        elif pos == 0.0:
            start = None                        # 되돌아왔으면 후보 취소
        if pos > 4.0 * s_adj and start is not None:
            return start, j                     # 확실히 벌어진 시점부터 보고한다
    return i, j


def drift(view):
    """서서히 이동해 한 시점에도 알람이 울리지 않는 변화.

    **창 길이를 여러 개 둔 이유.** 시나리오 2(시간당 0.68℃)는 정상 진동 진폭보다
    작아서 1시간 창으로는 절대 안 보인다. 12시간 창에서만 8.2℃ 라는 큰 변화가
    된다. 반대로 시나리오 20(30분에 440kW)은 짧은 창에서 즉시 잡힌다.
    하나의 창으로는 두 종을 같이 잡을 수 없다.

    판정은 **변화량 대 평소 산포**로 한다. 기울기의 유의성 검정을 쓰지 않은 것은
    데이터의 진동이 백색잡음이 아니어서 검정 가정이 깨지기 때문이다.

    **원본이 아니라 외기를 걷어낸 계열(`view.adj`)에서 잰다.** 대기 온습도는
    24시간 주기인데 근무는 12시간이라 한 근무 안에서는 반주기만 보이고, 그것이
    단조 상승·하강이라 드리프트와 구분되지 않는다. 걷어내지 않으면 없는 고장을
    만들어 내고(오탐 54건), 반대로 외기가 진짜 상승을 상쇄하는 근무에서는 있는
    고장을 지운다(시나리오 2 미검출). `core.fit_noise()` 주석 참고.
    """
    n = len(view)
    if n < 20:
        return []
    src = view.adj
    found = []
    for win_min in PARAMS["drift_windows_min"]:
        w = max(10, min(n, int(win_min * 60 / BLOCK_SEC)))
        for i in range(0, n - w + 1, max(1, w // 4)):
            ys = src[i:i + w]
            slope, r = ols(ys)
            delta = slope * (w - 1)
            k = abs(delta) / view.s_adj
            if k < PARAMS["drift_k"] or abs(r) < PARAMS["drift_r"]:
                continue
            found.append((k, i, i + w, delta, slope, r))
    if not found:
        return []

    # 가장 강한 창이 아니라 **가장 먼저 나타난 창**을 고른다 (세기가 비슷하다면).
    # 이상이 끝나고 값이 되돌아오는 구간이 대개 더 가파르기 때문에, 최대치만
    # 보면 "상승 추세" 를 "하강 추세" 로 보고하게 된다. 실제로 필터 차압
    # 상승 시나리오가 원복 구간을 집어 '하강 추세' 로 나왔다.
    strongest = max(k for k, *_rest in found)
    k, i, j, delta, slope, r = min(
        (f for f in found if f[0] >= strongest * 0.6), key=lambda f: f[1])
    unit = unit_of(view.tag)
    per_hour = slope * (3600 / BLOCK_SEC)
    up = delta > 0

    # 이 속도면 알람까지 얼마나 남았나. 근무자가 바로 쓸 수 있는 유일한 숫자다.
    spec = view.spec
    limit = spec.get("H") if up else spec.get("L")
    if limit is None:
        limit = spec.get("HH") if up else spec.get("LL")
    last = view.meds[j - 1]
    eta = None
    if limit is not None and per_hour:
        gap = (limit - last) / per_hour
        if 0 < gap < 48:
            eta = round(gap, 1)

    sev = SEV_HIGH if (eta is not None and eta <= 12) else _sev_by_z(k)
    hint = f" · 이 속도면 {'H' if up else 'L'} 한계까지 약 {eta}시간" if eta else ""

    # 외기 몫을 함께 적는다. 근무자가 "날씨 탓" 과 "설비 이상" 을 구분하려면 이
    # 한 줄이 필요하다. 특히 외기가 반대로 움직여 상쇄한 근무에서는 화면상 거의
    # 변하지 않은 태그가 왜 이상으로 올라왔는지 이것 없이는 설명되지 않는다.
    extra, amb_note = {}, ""
    if view.amb is not None:
        raw_delta = view.meds[j - 1] - view.meds[i]
        amb_delta = view.amb[j - 1] - view.amb[i]
        if abs(amb_delta) > abs(raw_delta) * 0.15:
            extra = {"raw_delta": round(raw_delta, 4),
                     "ambient_delta": round(amb_delta, 4)}
            amb_note = (f" · 실측 변화는 {_fmt(raw_delta, unit)} 이나 "
                        f"외기로 설명되는 몫 {_fmt(amb_delta, unit)} 을 제외한 값")

    ri, rj = _cusum_narrow(src, i, j, view.s_adj)
    return [_event(
        view, ri, rj, "드리프트", sev, k,
        {"delta": round(delta, 4), "per_hour": round(per_hour, 4), "r": round(r, 3),
         "k_sigma": round(k, 2), "pct_range": _round_pct(view.tag, delta),
         "eta_to_limit_h": eta, "limit": limit, "unit": unit,
         **extra},
        f"{label(view.tag)} {'상승' if up else '하강'} 추세 — "
        f"{_fmt(abs(per_hour), unit)}/시간, 구간 변화 {_fmt(delta, unit)}"
        f"{_paren(_pct_txt(view.tag, delta), f'평소 흔들림의 {k:.1f}배')}{hint}{amb_note}",
    )]


# --- 3. 레벨 시프트 ---------------------------------------------------

def level_shift(view):
    """한 번 이동한 뒤 그 자리에 머무는 변화 (시나리오 16~20 의 모양).

    누적합(CUSUM) 대신 **변화점 한 번 탐색**을 쓴다. 정상 데이터의 느린 진동에
    CUSUM 은 반주기마다 걸린다 — 진폭 1.4σ 가 15분 지속되면 누적합이 문턱을
    넘어 버린다. 앞뒤 중앙값 차이는 진동에 반응하지 않는다.
    """
    n = len(view)
    w = PARAMS["step_min_blocks"]
    # 아래 탐색이 range(w, n - w, ...) 라 n 이 2w 와 같으면 후보가 하나도 안 나온다.
    # 가드가 `<` 였을 때 그 경계에서 best 가 None 인 채로 언팩돼 적재 직후 run 이
    # TypeError 로 죽었다 (짧은 근무·부분 업로드에서 재현).
    if n <= 2 * w:
        return []
    best = None
    for c in range(w, n - w, max(1, w // 4)):
        a = median(view.meds[max(0, c - 4 * w):c])
        b = median(view.meds[c:min(n, c + 4 * w)])
        k = abs(b - a) / view.s_level
        if best is None or k > best[0]:
            best = (k, c, a, b)
    k, c, a, b = best
    if k < PARAMS["step_k"]:
        return []

    unit = unit_of(view.tag)
    return [_event(
        view, max(0, c - w), min(n, c + 4 * w), "레벨시프트", _sev_by_z(k),
        k,
        {"before": round(a, 4), "after": round(b, 4), "step": round(b - a, 4),
         "k_sigma": round(k, 2), "pct_range": _round_pct(view.tag, b - a), "unit": unit},
        f"{label(view.tag)} 단계 변화 — {_fmt(a, unit)} → {_fmt(b, unit)}"
        f"{_paren(f'{_fmt(b - a, unit)} 이동', _pct_txt(view.tag, b - a), f'평소 흔들림의 {k:.1f}배')}"
        f" 이후 그 수준 유지",
    )]


# --- 4. 헌팅 ----------------------------------------------------------

def hunting(view):
    """값이 아니라 **변동폭**이 커지는 현상.

    블록(30초) 안의 MAD 를 그 태그의 평소 블록 MAD 와 비교한다. 블록보다 짧은
    주기의 진동(시나리오 3 은 20초, 13 은 45초)도 블록 안 산포로 그대로 드러나기
    때문에, 원본 주기까지 내려가지 않아도 놓치지 않는다.

    회차마다 진폭이 커지는 것(시나리오 3)이 악화 신호이므로 **에피소드별 최대
    비율의 추세**를 함께 싣는다. 반복 횟수(시나리오 13)도 같이 센다.
    """
    ref = view.s_noise                     # 눈금 바닥이 깔린 값 (core.TagView)
    if ref <= 0:
        return []
    ratios = [(b.mad * MAD_TO_SIGMA) / ref for b in view.blocks]
    runs = _runs([r >= PARAMS["hunt_ratio"] for r in ratios],
                 PARAMS["hunt_min_blocks"])
    if not runs:
        return []

    peaks = [max(ratios[i:j]) for i, j in runs]
    top = max(peaks)
    growing = len(peaks) >= 3 and peaks[-1] > peaks[0] * 1.3
    i, j = runs[0][0], runs[-1][1]
    unit = unit_of(view.tag)
    seg = view.blocks[runs[peaks.index(top)][0]:runs[peaks.index(top)][1]]
    amp = max(b.hi for b in seg) - min(b.lo for b in seg)

    sev = SEV_HIGH if (growing or top >= 10) else _sev_by_z(top, high=8, mid=4)
    note = " · **회차마다 진폭이 커지는 중**" if growing else ""
    if len(runs) > 1:
        note += f" · {len(runs)}회 반복"
    return [_event(
        view, i, j, "헌팅", sev, top,
        {"ratio": round(top, 2), "episodes": len(runs),
         "episode_peaks": [round(p, 2) for p in peaks],
         "growing": growing, "amplitude": round(amp, 4),
         "pct_range": _round_pct(view.tag, amp), "unit": unit},
        f"{label(view.tag)} 변동폭 확대 — 흔들리는 폭이 평소의 {top:.1f}배"
        f"{_paren(f'진폭 약 {_fmt(amp, unit)}', _pct_txt(view.tag, amp))}{note}",
    )]


# --- 5. 순간 이탈 -----------------------------------------------------

def spike(view):
    """3초짜리 폭등, 10초짜리 급강하처럼 **알람 on-delay 보다 짧은** 이탈.

    비교 대상은 **바로 앞뒤 블록의 중간값**이고, 잣대는 **블록 사이의 평소
    이동량**이다. 둘 다 처음에는 "±5블록 국소 중앙값 / 노이즈 σ" 로 잡았다가
    바꿨다 — 변동이 빠른 태그(drift_level 3)는 5분이면 정상적으로 크게 움직이는데,
    그 이동을 노이즈 σ 로 나누니 15σ 가 나와 근무당 오탐이 열 건 넘게 쌓였다.

    앞뒤 중간값과 비교하면 **완만한 이동은 상쇄되고 튀는 점만 남는다.**

    999ppm 같은 값은 통계에서 이미 빠져 있다(`core.to_blocks`). 여기서는 탐지만
    한다 — **기준선에서 빼는 것과 근무자에게 알리는 것은 다른 문제다**(Issue #2).
    """
    n = len(view)
    if n < 12:
        return []
    unit = unit_of(view.tag)
    steps = [abs(view.meds[i + 1] - view.meds[i]) for i in range(n - 1)]
    scale = max(median(steps) * MAD_TO_SIGMA, view.s_noise)

    zs = []
    for i, b in enumerate(view.blocks):
        if 0 < i < n - 1:
            c = (view.meds[i - 1] + view.meds[i + 1]) / 2
        else:
            c = b.med
        d = max(abs(b.hi - c), abs(c - b.lo))
        zs.append(d / scale)

    events = []
    for i, j in _runs([z >= PARAMS["spike_z"] for z in zs], 1):
        if j - i > PARAMS["spike_max_blocks"]:
            continue                       # 길면 스파이크가 아니다. 다른 검출기 몫
        seg = view.blocks[i:j]
        c = median(view.meds[max(0, i - 3):i] + view.meds[j:min(n, j + 3)]) \
            or median(view.meds)
        peak = max(seg, key=lambda b: max(abs(b.hi - c), abs(c - b.lo)))
        value = peak.hi if abs(peak.hi - c) >= abs(c - peak.lo) else peak.lo
        events.append((max(zs[i:j]), i, j, value, c))

    if not events:
        return []
    events.sort(reverse=True, key=lambda e: e[0])
    z, i, j, value, c = events[0]
    times = len(events)
    return [_event(
        view, i, j, "이탈", _sev_by_z(z, high=20, mid=10), z,
        {"z": round(z, 1), "value": round(value, 4), "local_median": round(c, 4),
         "jump": round(value - c, 4), "pct_range": _round_pct(view.tag, value - c),
         "episodes": times, "unit": unit},
        f"{label(view.tag)} 순간 이탈 — {_fmt(c, unit)} 에서 {_fmt(value, unit)} 로 튐"
        f"{_paren(_pct_txt(view.tag, value - c))}"
        f"{f' · 근무 중 {times}회' if times > 1 else ''}, "
        f"알람 지속시간 미달로 미발생",
    )]


# --- 6. 임계 근접 -----------------------------------------------------

def near_limit(view):
    """알람은 안 울렸지만 위험했던 구간.

    정상값에서 한계까지의 거리 중 몇 %를 소진했는지로 본다. 한계는 정상범위
    상·하한과 알람선 중 **더 가까운 쪽**을 쓴다 — 시나리오 4 는 정상 상한(20℃)이
    기준이고 시나리오 10 은 알람선(H 88%)이 기준인데, 둘 다 "먼저 닿는 선"이다.
    """
    spec = view.spec
    nom = spec.get("nominal")
    if nom is None:
        return []
    unit = unit_of(view.tag)
    out = []
    for name, cands, above in (("상한", (spec.get("normal_hi"), spec.get("H")), True),
                               ("하한", (spec.get("normal_lo"), spec.get("L")), False)):
        limits = [v for v in cands if v is not None]
        if not limits:
            continue
        ref = min(limits) if above else max(limits)
        gap = ref - nom
        if not gap:
            continue
        fracs = [(m - nom) / gap for m in view.meds]
        runs = _runs([f >= PARAMS["prox_frac"] for f in fracs],
                     PARAMS["prox_min_blocks"])
        for i, j in runs:
            seg = fracs[i:j]
            # 진동의 꼭짓점이 잠깐 스친 것과 눌러앉은 것을 가른다
            if median(seg) < PARAMS["prox_frac"]:
                continue
            top = max(seg)
            value = max(view.meds[i:j]) if above else min(view.meds[i:j])
            out.append(_event(
                view, i, j, "임계근접", SEV_MID if top >= 0.95 else SEV_LOW, top * 10,
                {"fraction": round(top, 3), "limit": ref, "side": name,
                 "value": round(value, 4), "unit": unit},
                f"{label(view.tag)} {name} {_fmt(ref, unit)} 근접 — "
                f"{_fmt(value, unit)}({top * 100:.0f}% 소진)에서 "
                f"{round((j - i) * BLOCK_SEC / 60)}분 체류, 알람 미발생",
            ))
    return out


# --- 7. 고착 ----------------------------------------------------------

def stuck(view):
    """지령이 바뀌어도 값이 움직이지 않는 상태 — 계기 고장·밸브 고착.

    **조용한 것과 죽은 것은 다르다.** 이걸 못 잡으면 "이상 없음"으로 인계된다.
    반올림 때문에 원래 변동이 안 보이는 태그를 오탐하지 않도록, 그 태그가 근무
    중 다른 구간에서는 실제로 움직였을 때만 판정한다.
    """
    if not view.resolves() or view.raw_noise <= 0:
        return []
    # 값이 **눈금 하나만큼도 움직이지 않은** 구간을 찾는다. 블록 MAD 만 보면
    # 반올림 때문에 원래 0 인 태그가 근무 내내 고착으로 잡힌다.
    cut = max(view.q, view.raw_noise * PARAMS["stuck_frac"])
    n = len(view)
    w = PARAMS["stuck_min_blocks"]
    flags = [False] * n
    for i in range(n - w + 1):
        seg = view.meds[i:i + w]
        if max(seg) - min(seg) <= cut and max(view.mads[i:i + w]) <= cut:
            for k in range(i, i + w):
                flags[k] = True
    runs = _runs(flags, w)
    if not runs:
        return []
    i, j = max(runs, key=lambda r: r[1] - r[0])
    minutes = (j - i) * BLOCK_SEC / 60
    unit = unit_of(view.tag)
    value = median(view.meds[i:j])
    return [_event(
        view, i, j, "고착", SEV_HIGH, minutes,
        {"value": round(value, 4), "minutes": round(minutes), "unit": unit},
        f"{label(view.tag)} 값 고착 — {_fmt(value, unit)} 에서 "
        f"{round(minutes)}분간 변화 없음. 값 자체는 정상범위라 알람 미발생",
    )]


# --- 8. 상관 이탈 -----------------------------------------------------

def residual(src, tgt, gain, lag_sec):
    """평소 동조하던 두 태그의 관계가 깨진 구간.

    태그 마스터 `links` 에 적힌 쌍만 본다. 지연을 반영해 선형으로 맞춘 뒤,
    **잔차의 크기**가 커지는 구간을 찾는다. 잔차의 평균이 아니라 절댓값의
    평균을 쓰는 이유는, 한쪽이 멈추고 다른 쪽이 진동하면 잔차가 ± 로 오가면서
    평균이 0 이 되어 버리기 때문이다.
    """
    lag = int(round(lag_sec / BLOCK_SEC))
    n = min(len(src), len(tgt))
    if n < 60:
        return []
    xs = [src.meds[max(0, i - lag)] for i in range(n)]
    ys = tgt.meds[:n]
    if abs(pearson(xs, ys)) < PARAMS["resid_min_r"]:
        return []                          # 평소에도 안 붙어 있으면 붕괴를 말할 수 없다

    xm, ym = median(xs), median(ys)
    sx = sigma(xs, xm)
    if not sx:
        return []
    b = sum((x - xm) * (y - ym) for x, y in zip(xs, ys)) / \
        sum((x - xm) ** 2 for x in xs)
    err = [abs((y - ym) - b * (x - xm)) for x, y in zip(xs, ys)]
    m_err = median(err)
    s_err = mad(err, m_err) * MAD_TO_SIGMA
    if not s_err:
        return []

    w = PARAMS["resid_win_blocks"]
    roll = []
    acc = sum(err[:w])
    roll.append(acc / w)
    for i in range(w, n):
        acc += err[i] - err[i - w]
        roll.append(acc / w)
    zs = [(v - m_err) / s_err for v in roll]
    runs = _runs([z >= PARAMS["resid_z"] for z in zs], PARAMS["resid_min_blocks"])
    if not runs:
        return []

    i, j = max(runs, key=lambda r: max(zs[r[0]:r[1]]))
    z = max(zs[i:j])
    return [_event(
        tgt, i, min(j + w, n), "상관이탈", SEV_HIGH, z,
        {"pair": [src.tag, tgt.tag], "gain": gain, "lag_sec": lag_sec,
         "z": round(z, 1), "baseline_r": round(pearson(xs, ys), 2)},
        f"{label(src.tag)} 와 {label(tgt.tag)} 의 평소 연동이 끊김 — "
        f"두 값의 어긋남이 평소의 {z:.1f}배. 단일 태그로는 보이지 않는 이상",
    )]


# --- 실행 -------------------------------------------------------------

SINGLE = (limit_crossings, drift, level_shift, hunting, spike, near_limit, stuck)


def run_all(views):
    """단일 태그 검출기 7종 + 연결 쌍 검출기를 모두 돌린다."""
    events = []
    for view in views.values():
        for fn in SINGLE:
            try:
                events.extend(fn(view))
            except (ValueError, ZeroDivisionError, IndexError, StatisticsError):
                # 태그 하나가 검출기 하나를 깨뜨려도 나머지 52점은 계속 본다.
                continue

    seen = set()
    for tag, view in views.items():
        for other, gain, lag in view.spec.get("links", ()):
            pair = (tag, other)
            if other not in views or pair in seen:
                continue
            seen.add(pair)
            try:
                events.extend(residual(view, views[other], gain, lag))
            except (ValueError, ZeroDivisionError, IndexError, StatisticsError):
                continue
    return events
