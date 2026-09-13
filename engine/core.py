"""검출 공통 기반 — 태그 마스터, 블록 요약, 로버스트 통계.

**왜 블록으로 줄이는가.** 한 근무는 12시간 × 2초 = 21,600점이고 태그가 53점이라
원본 그대로 이동창 통계를 돌리면 1억 회 이상 곱셈이 나온다. 순수 파이썬으로는
초안 생성 시간 안에 끝나지 않는다.

그래서 **30초 단위 블록으로 한 번만 접고**, 이후 모든 검출기는 블록 위에서 돈다
(12시간 → 1,440블록). 블록 하나에 다음을 담는다.

    med / mad   : 블록 안 15점의 중앙값과 중앙값절대편차 (스파이크 제거 후)
    lo / hi     : 블록 안 **원본** 최소·최대 (스파이크를 여기서 잡는다)

med·mad 만 남기면 3초짜리 스파이크(시나리오 12)와 10초짜리 급강하(시나리오 5)가
평균에 묻혀 사라진다. lo·hi 를 함께 들고 가는 것이 그 두 종을 살리는 장치다.

스파이크를 med·mad 계산에서 빼는 것은 Issue #2 다. 999ppm 한 점이 기준선에
들어가면 MAD 가 부풀어 **다른 시나리오의 탐지까지 같이 죽는다.**
"""
import csv
import os
import math
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 다른 공정에 붙일 때 바꾸는 것은 「태그 명세」와 「데이터 입구」 둘뿐이다.
# 명세 경로를 환경변수로 받아, 검출 코드는 한 줄도 건드리지 않고 공정을 바꾼다.
#     ENGRA_TAG_MASTER=docs/water/tag_master.csv   (수처리 65점)
# 값이 없으면 종전과 같은 ASU 정본을 쓴다 — 기존 측정치에 영향이 없다.
TAG_MASTER = Path(os.environ.get("ENGRA_TAG_MASTER") or ROOT / "docs" / "tag_master.csv")

BLOCK_SEC = 30          # 블록 길이. 시나리오 3(주기 20초)·13(주기 45초)이 블록 안에서 다 보인다
MAD_TO_SIGMA = 1.4826   # 정규분포에서 MAD × 이 값 = 표준편차
SPIKE_Z = 12.0          # 이 이탈도를 넘는 점은 통계에서 뺀다 (탐지에서는 뺴지 않는다)

_NUM_COLS = ("nominal", "normal_lo", "normal_hi", "LL", "L", "H", "HH")


# --- 태그 마스터 ------------------------------------------------------

def _parse_links(raw):
    """links 칸 "FI-602:0.95, PI-604:-0.40:20" -> [(대상, 이득, 지연초)].

    지연이 없으면 0. 이 목록이 상관·잔차 검출기의 유일한 입력이다 —
    53개 태그를 전부 짝지으면 1,378쌍이라 계산도 무겁고 의미 없는 쌍이
    대부분이다. 공정을 아는 사람이 적어 둔 연결만 본다.
    """
    out = []
    for part in (raw or "").split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) < 2 or not bits[0]:
            continue
        try:
            gain = float(bits[1])
        except ValueError:
            continue
        lag = 0.0
        if len(bits) > 2 and bits[2]:
            try:
                lag = float(bits[2])
            except ValueError:
                lag = 0.0
        out.append((bits[0], gain, lag))
    return out


def load_tags(path=TAG_MASTER):
    """docs/tag_master.csv -> {태그: 사양}. 없으면 빈 dict (스텁과 같은 태도)."""
    if not Path(path).exists():
        return {}
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            tag = (row.get("tag") or "").strip()
            if not tag:
                continue
            spec = {
                "tag": tag,
                "description": (row.get("description") or "").strip(),
                "unit": (row.get("unit") or "").strip(),
                "links": _parse_links(row.get("links")),
            }
            for key in _NUM_COLS:
                raw = (row.get(key) or "").strip()
                try:
                    spec[key] = float(raw) if raw else None
                except ValueError:
                    spec[key] = None
            lo, hi = spec.get("normal_lo"), spec.get("normal_hi")
            spec["span"] = abs(hi - lo) if (lo is not None and hi is not None) else None
            out[tag] = spec
    return out


TAGS = load_tags()


def spec_of(tag):
    return TAGS.get(tag) or {"tag": tag, "description": "", "unit": "", "links": [],
                             "span": None, **{k: None for k in _NUM_COLS}}


def unit_of(tag):
    return spec_of(tag).get("unit") or ""


def label(tag):
    d = spec_of(tag).get("description")
    return f"{tag}({d})" if d else tag


# --- 로버스트 통계 ----------------------------------------------------

def median(xs):
    return statistics.median(xs) if xs else 0.0


def mad(xs, med=None):
    if not xs:
        return 0.0
    m = median(xs) if med is None else med
    return statistics.median([abs(x - m) for x in xs])


def sigma(xs, med=None):
    """MAD 기반 표준편차 추정. 평균·표준편차를 쓰지 않는 이유는 engine/README 참고."""
    return mad(xs, med) * MAD_TO_SIGMA


def mod_z(value, med, mad_):
    """수정 z-score. MAD 가 0이면 판정 불가로 보고 0을 돌려준다."""
    return 0.6745 * (value - med) / mad_ if mad_ else 0.0


def quantum(values):
    """기록된 값의 최소 눈금.

    **이걸 모르면 검출기가 통째로 망가진다.** 생성기와 DCS 는 태그마다 자리수를
    정해 반올림하는데, 정상범위 폭이 0.07 인 차압 태그가 소수 2자리로 기록되면
    눈금(0.01)이 실제 노이즈(0.001)보다 열 배 크다. 그러면 블록 안 MAD 가 0 이
    되고, 한 눈금 움직인 것이 수백 σ 로 계산된다.

    실측에서 이 현상으로 오탐이 30건 나왔다. 눈금을 재서 산포의 **바닥값**으로
    깔아 주는 것이 유일한 해법이다 — 한 눈금보다 작은 차이는 애초에 기록되지
    않았으므로 의미를 부여할 수 없다.
    """
    vs = sorted(set(values))
    if len(vs) < 2:
        return 0.0
    # 서로 다른 값이 **둘뿐인** 경우를 빼먹었다가 오탐이 났다. 정상범위 폭이
    # 0.07 인 차압 태그는 근무 내내 0.08 과 0.09 두 값만 기록되는데, 그때
    # 눈금을 0 으로 돌려주면 바닥값이 사라져 한 눈금 이동이 1,429σ 가 된다.
    eps = max(abs(vs[0]), abs(vs[-1]), 1.0) * 1e-9
    gaps = [b - a for a, b in zip(vs, vs[1:]) if b - a > eps]
    return min(gaps) if gaps else 0.0


def ols(ys):
    """등간격 y 목록의 최소제곱 기울기(스텝당)와 상관계수 r.

    r 을 함께 돌려주는 것이 중요하다. 생성기의 정상 데이터에는 20~60분 주기의
    느린 진동이 이미 들어 있어서, 기울기만 보면 진동의 반주기를 드리프트로
    오인한다. 단조성(|r|)을 함께 요구하면 그 오탐이 사라진다.
    """
    n = len(ys)
    if n < 3:
        return 0.0, 0.0
    xm = (n - 1) / 2
    ym = sum(ys) / n
    sxy = sxx = syy = 0.0
    for i, y in enumerate(ys):
        dx, dy = i - xm, y - ym
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0:
        return 0.0, 0.0
    slope = sxy / sxx
    r = sxy / math.sqrt(sxx * syy) if syy > 0 else 0.0
    return slope, r


def pearson(xs, ys):
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    xm = sum(xs[:n]) / n
    ym = sum(ys[:n]) / n
    sxy = sxx = syy = 0.0
    for i in range(n):
        dx, dy = xs[i] - xm, ys[i] - ym
        sxy += dx * dy
        sxx += dx * dx
        syy += dy * dy
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


# --- 잡음인자(외기) ---------------------------------------------------
#
# **선언이지 하드코딩이 아니다.** 6시그마는 인자를 두 종류로 나눈다 — 값을 정할
# 수 있는 제어인자(밸브 개도·유량 설정)와, 영향은 주는데 정할 수 없는 잡음인자
# (대기 온습도·원료 조성). 잡음인자는 없앨 수 없으므로 **그 몫을 모형에 넣어
# 설명해 버리고 남은 것으로만 이상을 판정한다**(강건설계).
#
# 이 목록은 원래 `docs/tag_master.csv` 의 열이어야 한다. 그쪽은 임도영님 담당이라
# 마감 일정 때문에 engine/ 안에 두었다. 옮길 때 열 이름은 `noise_factor` 로 하고
# 여기를 지우면 된다 — 그 외 코드는 손댈 곳이 없다.
NOISE_TAGS = ("TI-101", "MI-102")

# 어떤 태그를 보정할지 정하는 두 관문. 관계가 옅은 태그까지 건드리면 멀쩡한
# 신호를 깎아내린다.
#
# **처음에는 "추세를 뺀 산포가 25% 이상 줄면 보정" 으로 걸었다가 거의 듣지
# 않았다.** 그 산포는 정상 데이터에 원래 들어 있는 20~60분 진동이 지배해서,
# 하루 주기의 완만한 활 모양을 걷어내도 별로 줄지 않는다. 지표를 잘못 고른
# 것이지 추정이 안 된 것이 아니었다 — 실제 곡률 상관은 외기 감응 태그가
# 0.30~0.79, 무관한 태그가 0.01~0.09 로 이미 뚜렷이 갈려 있었다.
NOISE_MIN_R = 0.20       # 곡률이 닮았는가 — 관계가 있다는 증거
NOISE_MIN_SWING = 1.0    # 외기가 흔드는 폭이 그 태그 산포의 몇 배 이상인가 — 판정에 영향을 주는가


def _solve(a, b):
    """작은 정규방정식 a·x = b 를 푼다 (미지수 4개 이하). 부분 피벗 가우스 소거."""
    n = len(b)
    m = [list(row) + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None          # 특이행렬 — 외기끼리 겹쳤거나 값이 고정된 태그
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r == c:
                continue
            f = m[r][c] / m[c][c]
            if f:
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def lstsq(cols, y):
    """설계행렬(열 목록)에 대한 최소제곱 계수. 열이 4개 이하라 정규방정식으로 충분하다."""
    n = len(y)
    a = [[sum(ci[t] * cj[t] for t in range(n)) for cj in cols] for ci in cols]
    b = [sum(ci[t] * y[t] for t in range(n)) for ci in cols]
    return _solve(a, b)


def _decurve(ys):
    """직선(기울기+절편) 성분을 지우고 **휘어 있는 몫만** 남긴다.

    고장 램프는 곧게 오르므로 여기서 사라지고, 하루 주기로 휘는 외기는 남는다.
    두 가지를 갈라내는 유일한 단서가 이 곡률이다 — `TagView.fit_noise()` 참고.
    """
    n = len(ys)
    slope, _r = ols(ys)
    m = sum(ys) / n
    xm = (n - 1) / 2
    return [y - m - slope * (i - xm) for i, y in enumerate(ys)]


def detrended_sigma(ys):
    """직선 추세를 뺀 뒤의 산포. 추세 자체를 산포로 세지 않기 위한 것."""
    slope, _r = ols(ys)
    return sigma([y - slope * i for i, y in enumerate(ys)])


# --- 시각 -------------------------------------------------------------

_TS_CACHE = {}


def epoch(ts):
    """ISO 문자열 -> 초. 태그 53점이 같은 타임스탬프를 공유하므로 캐시가 크게 듣는다."""
    got = _TS_CACHE.get(ts)
    if got is None:
        got = datetime.fromisoformat(ts).timestamp()
        _TS_CACHE[ts] = got
    return got


def iso(sec):
    return datetime.fromtimestamp(sec).isoformat(timespec="seconds")


def hhmm(ts):
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except (ValueError, TypeError):
        return "??:??"


# --- 블록 -------------------------------------------------------------

class Block:
    """30초 구간 하나. 슬롯을 고정해 1,440 × 53 개를 만들어도 메모리가 튀지 않는다."""

    __slots__ = ("t0", "t1", "n", "med", "mad", "lo", "hi")

    def __init__(self, t0, t1, n, med, mad_, lo, hi):
        self.t0, self.t1, self.n = t0, t1, n
        self.med, self.mad = med, mad_
        self.lo, self.hi = lo, hi


def _fold(bucket, t0, t1, center, scale):
    """한 블록의 통계. center·scale 은 태그 전체의 로버스트 기준으로, 스파이크 판정용."""
    lo, hi = min(bucket), max(bucket)
    if scale:
        clean = [v for v in bucket if abs(0.6745 * (v - center) / scale) <= SPIKE_Z]
        if not clean:                      # 블록 전체가 스파이크면 통계를 포기하고 원본을 쓴다
            clean = bucket
    else:
        clean = bucket
    m = median(clean)
    return Block(t0, t1, len(bucket), m, mad(clean, m), lo, hi)


def to_blocks(points, block_sec=BLOCK_SEC):
    """[(ts, value)] -> [Block]. 시간이 비면 그 블록은 만들지 않는다(결측 구간을 잇지 않는다)."""
    if not points:
        return []
    values = [v for _, v in points]
    center = median(values)
    scale = mad(values, center)

    blocks = []
    bucket = []
    start = None
    edge = None
    for ts, value in points:
        t = epoch(ts)
        if edge is None or t >= edge:
            if bucket:
                blocks.append(_fold(bucket, start, edge, center, scale))
            start = math.floor(t / block_sec) * block_sec
            edge = start + block_sec
            bucket = []
        bucket.append(value)
    if bucket:
        blocks.append(_fold(bucket, start, edge, center, scale))
    return blocks


class TagView:
    """한 태그의 블록열 + 그 태그의 '평소'.

    **평소를 이번 근무 안에서 구한다.** 데모는 CSV 한 장을 넣고 바로 돌리므로
    지난 근무가 없는 경우가 정상이다(`pipeline.py` 가 그때 잠정 기준선을 만든다).
    중앙값·MAD 는 절반 넘게 오염되지 않는 한 흔들리지 않으므로, 12시간 중
    한두 시간짜리 이상은 스스로를 정상으로 만들지 못한다.

    지난 근무 기준선이 있으면 레벨 비교에만 보조로 쓴다(`base`).
    """

    __slots__ = ("tag", "spec", "blocks", "meds", "mads", "level", "s_level",
                 "s_noise", "raw_noise", "q", "base", "adj", "s_adj", "amb")

    def __init__(self, tag, blocks, base=None, q=0.0):
        self.tag = tag
        self.spec = spec_of(tag)
        self.blocks = blocks
        self.meds = [b.med for b in blocks]
        self.mads = [b.mad for b in blocks]
        self.base = base or {}
        self.q = q

        self.level = median(self.meds)
        # 기록되지 않은 차이는 판정 근거가 될 수 없다 (quantum 주석 참고).
        # 레벨은 한 눈금, 노이즈는 반 눈금을 바닥으로 깐다.
        self.raw_noise = median(self.mads) * MAD_TO_SIGMA

        # **추세를 뺀 뒤에 산포를 잰다.** 근무 내내 이어지는 드리프트(시나리오 2)는
        # 자기 자신을 '평소 산포' 로 만들어 버린다. 12시간에 8.2℃ 오른 태그의
        # 원본 MAD 는 3.0℃ 라, 8.2℃ 변화가 겨우 2.7배로 계산돼 탐지에서 빠졌다.
        # 추세를 제거하면 남는 것은 진동 1.5℃ 뿐이고 같은 변화가 7.8배가 된다.
        self.s_level = max(detrended_sigma(self.meds), q, self._floor())
        self.s_noise = max(self.raw_noise, q * 0.5, self._floor())

        # 외기 보정 전 기본값. `fit_noise()` 가 성공하면 갈아끼운다.
        self.adj = self.meds
        self.s_adj = self.s_level
        self.amb = None

    def fit_noise(self, refs):
        """외기 몫을 걷어낸 계열을 만든다. 성공하면 `adj`·`s_adj`·`amb` 를 채운다.

        **곡률로 계수를 재고, 그 계수로 외기 전체를 뺀다.** 이 두 단계를 나눈
        것이 이 함수의 전부다.

        왜 나눠야 하는가. 12시간 근무 안에서 외기 반주기는 거의 단조라서
        **"직선" 과 "외기" 가 서로 구분되지 않는다.** 처음에는

            값 = 상수 + b·시간 + a·외기

        로 한 번에 풀었는데, 시간항과 외기가 사실상 같은 모양이라 몫을 어떻게
        나눌지 결정되지 않았다. 53태그 중 4개만 보정되고 나머지는 감소율 0.0% 로
        무산됐다. 시간항을 빼면 이번에는 외기 계수가 고장 램프까지 삼킨다.

        빠져나갈 구멍은 **외기에는 곡률이 있고 고장 램프에는 없다**는 점이다.
        기온은 하루 주기로 휘지만 고장은 곧게 오른다. 그래서

          ① 양쪽에서 직선 성분을 지우고 **곡률끼리만** 맞춰 계수 a 를 구한다.
             고장의 직선 성분은 이미 지워졌으므로 a 를 오염시키지 못한다.
          ② 그 a 로 **직선 성분까지 포함한 외기 전체**를 뺀다.

        남는 것은 외기로 설명되지 않는 변화뿐이고, 고장 램프는 손대지 않았으므로
        그대로 살아 있다. 시나리오 2(토출온도 +0.68℃/h)가 걸린 야간 근무에서
        대기온도가 7.6℃ 떨어지는 동안 토출온도는 0.55℃ 밖에 오르지 않았다 —
        고장이 올린 8.2℃ 를 외기가 거의 같은 크기로 끌어내린 것이다. 이 절차라야
        그 8.2℃ 가 되살아난다.
        """
        n = len(self.meds)
        if n < 20 or any(len(r) != n for r in refs):
            return

        # ① 곡률끼리만 맞춘다. 직선을 지운 뒤라 절편·시간항이 필요 없다.
        y_c = _decurve(self.meds)
        r_c = [_decurve(r) for r in refs]
        coef = lstsq(r_c, y_c)
        if coef is None:
            return

        # 주입된 이상이 계수를 끌고 가지 않게 한 번 다듬는다 — 크게 튄 블록을
        # 빼고 다시 맞춘다. 이상 구간이 외기 계수를 정하면 안 된다.
        fit = [sum(c * col[i] for c, col in zip(coef, r_c)) for i in range(n)]
        res = [y - f for y, f in zip(y_c, fit)]
        sr = sigma(res)
        if sr > 0:
            keep = [i for i in range(n) if abs(res[i]) <= 3 * sr]
            if len(keep) >= max(20, n // 2):
                again = lstsq([[col[i] for i in keep] for col in r_c],
                              [y_c[i] for i in keep])
                if again is not None:
                    coef = again

        # ② 그 계수로 외기 원본(직선 성분 포함)을 뺀다.
        part = [sum(coef[k] * refs[k][i] for k in range(len(refs)))
                for i in range(n)]
        adj = [y - p for y, p in zip(self.meds, part)]

        # 외기가 이 태그를 실제로 흔드는가. 두 가지를 함께 본다 — 곡률이 닮았는가
        # (관계의 증거), 그리고 그 몫이 판정을 바꿀 만큼 큰가(크기).
        fitted = [sum(coef[k] * r_c[k][i] for k in range(len(refs)))
                  for i in range(n)]
        if abs(pearson(y_c, fitted)) < NOISE_MIN_R:
            return
        if max(part) - min(part) < NOISE_MIN_SWING * self.s_level:
            return

        self.adj = adj
        self.s_adj = max(detrended_sigma(adj), self.q, self._floor())
        self.amb = part          # 구간별 "외기로 설명되는 몫" 을 초안에 적기 위해 남긴다

    def _floor(self):
        """분산이 0인 태그(고착·과도한 반올림)에서 0으로 나누지 않기 위한 바닥값."""
        span = self.spec.get("span")
        return abs(span) * 1e-4 if span else 1e-9

    def resolves(self):
        """실제 노이즈가 기록 눈금보다 큰가.

        아니라면 그 태그는 값이 오래 멈춰 있는 것이 정상이므로 **고착 판정을 할 수
        없다.** 눈금이 노이즈를 삼킨 태그에 고착 검사를 돌리면 근무 내내 고착이다.
        """
        return self.raw_noise > self.q > 0 or self.q == 0

    def __len__(self):
        return len(self.blocks)

    def window(self, i, j):
        return self.meds[i:j]

    def span_ts(self, i, j):
        """블록 인덱스 구간 -> (시작 ISO, 종료 ISO)."""
        j = min(max(j, i + 1), len(self.blocks))
        return iso(self.blocks[i].t0), iso(self.blocks[j - 1].t1)


def build_views(series, baselines=None):
    """{태그: [(ts, v)]} -> {태그: TagView}. 태그 마스터에 없는 태그는 버린다."""
    baselines = baselines or {}
    views = {}
    for tag, points in series.items():
        if not points or tag not in TAGS:
            continue
        blocks = to_blocks(points)
        if len(blocks) >= 4:
            q = quantum([v for _, v in points])
            views[tag] = TagView(tag, blocks, baselines.get(tag), q)
    _apply_noise_model(views)
    return views


def _apply_noise_model(views):
    """잡음인자(외기) 계열을 기준자로 만들어 전 태그에 보정을 건다.

    외기는 24시간 주기인데 근무는 12시간이라, **한 근무 안에서는 반주기만 보여
    단조 상승이나 단조 하강으로 나타난다.** 드리프트 검출기 눈에는 그것이 완벽한
    드리프트다. 독립 측정에서 오탐 67건 중 54건이 이 현상이었고, 54건 전부가
    외기 감응 태그였다.
    """
    if not views:
        return
    refs = []
    n = None
    for tag in NOISE_TAGS:
        v = views.get(tag)
        if v is None:
            continue
        if n is None:
            n = len(v.meds)
        if len(v.meds) != n:
            continue
        m = median(v.meds)
        s = sigma(v.meds) or (max(v.meds) - min(v.meds)) or 1.0
        refs.append([(x - m) / s for x in v.meds])
    refs = _independent(refs)
    if not refs:
        return          # 외기 태그가 없는 데이터셋 — 보정 없이 지금까지대로 돈다
    for v in views.values():
        v.fit_noise(refs)


def _independent(refs):
    """기준자끼리 겹치는 몫을 걷어내고, 남는 정보가 없는 것은 버린다.

    **이걸 빼먹어서 첫 판이 거의 듣지 않았다.** 대기 온도와 대기 습도를 그대로
    둘 다 넣었더니 두 계열의 상관이 **r = −0.9998** 이었다. 생성기가 온도와
    습도를 같은 일주기 함수 하나로 만들기 때문에, 서로 다른 태그로 보여도 실은
    같은 신호다. 거의 동일한 설명변수 두 개를 회귀에 넣으면 정규방정식이
    특이행렬에 가까워져 계수가 불안정해진다. 실제로 53태그 중 4개만 보정되고
    나머지는 감소율 0.0% 로 무산됐다.

    그람-슈미트로 앞선 기준자와 겹치는 성분을 빼고, 남은 크기가 원래의 10% 도
    안 되면 **새로운 정보가 없는 것으로 보고 버린다.** 온습도가 실제로 따로 노는
    데이터가 들어오면 둘 다 살아남는다 — 데이터가 정하게 두는 것이 요점이다.
    """
    out = []
    for r in refs:
        v = list(r)
        for u in out:
            d = sum(x * x for x in u)
            if d <= 0:
                continue
            c = sum(a * b for a, b in zip(v, u)) / d
            v = [a - c * b for a, b in zip(v, u)]
        rms = math.sqrt(sum(x * x for x in v) / len(v)) if v else 0.0
        if rms > 0.1:        # 기준자는 산포 1로 맞춰 두었으므로 10% 가 기준이 된다
            out.append(v)
    return out
