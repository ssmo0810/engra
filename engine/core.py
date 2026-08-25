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
import math
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG_MASTER = ROOT / "docs" / "tag_master.csv"

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
                 "s_noise", "raw_noise", "q", "base")

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
        slope, _r = ols(self.meds)
        flat = [m - slope * i for i, m in enumerate(self.meds)]
        self.s_level = max(sigma(flat), q, self._floor())
        self.s_noise = max(self.raw_noise, q * 0.5, self._floor())

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
    return views
