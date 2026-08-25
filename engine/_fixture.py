"""개발용 모의 데이터 — `docs/asu_dcs_overview.html` 생성기의 파이썬 이식.

**공식 성능 측정용이 아니다.** 포함률은 임도영님이 만든
`asu_shift_*.csv` + `asu_answer_key.json` 으로 재는 것이 맞다(Issue #5).
이 파일은 그것이 도착하기 전에 **검출기가 실제로 동작하는지 확인하기 위한
개발 픽스처**이고, 문턱값을 만질 때마다 즉시 되돌려 볼 수 있게 하려는 것이다.

같은 난수 생성기·같은 노이즈 모델·같은 파형 함수·같은 연동 규칙을 그대로 옮겼기
때문에 파형의 성질은 동일하다. 다만 **시나리오 배치는 JS 와 다르다** —
JS 는 무작위로 2~3종만 넣지만, 여기서는 20종 전부를 태그가 겹치지 않게
여러 회차로 나눠 넣는다. 20종을 다 시험하려는 목적이기 때문이다.

원본: docs/asu_dcs_overview.html 의 rng / gauss / series / applyLinks / SCEN
"""
import csv
import math
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG_MASTER = ROOT / "docs" / "tag_master.csv"

STEP = 2                    # 초. 연결 규약 ①
DRIFT = (0.02, 0.06, 0.12)
NOISE = (0.005, 0.015, 0.035)


# --- JS 비트연산 재현 -------------------------------------------------

def _u32(x):
    return x & 0xFFFFFFFF


def _i32(x):
    x &= 0xFFFFFFFF
    return x - 0x100000000 if x & 0x80000000 else x


def _imul(a, b):
    return _i32(_i32(a) * _i32(b))


class Rng:
    """JS mulberry32. 같은 seed 면 같은 수열이 나온다."""

    def __init__(self, seed):
        self.a = _u32(seed)

    def __call__(self):
        self.a = _u32(self.a + 0x6D2B79F5)
        t = self.a
        t = _imul(t ^ (_u32(t) >> 15), _u32(t) | 1)
        t = _i32(_u32(t) ^ _u32(t + _imul(_u32(t) ^ (_u32(t) >> 7), _u32(t) | 61)))
        return _u32(_u32(t) ^ (_u32(t) >> 14)) / 4294967296.0


def _gauss(rnd):
    u = 1 - rnd()
    v = rnd()
    return math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * v)


def _hash(s):
    h = 0
    for ch in s:
        h = _i32(h * 31 + ord(ch))
    return h


# --- 태그 사양 --------------------------------------------------------

def load_spec(path=TAG_MASTER):
    """생성에 필요한 칸만 읽는다. nominal 은 자리수 계산 때문에 **문자열도** 둔다."""
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            tag = (row.get("tag") or "").strip()
            if not tag:
                continue
            out[tag] = {
                "nom_text": (row.get("nominal") or "0").strip(),
                "nom": float(row["nominal"]),
                "lo": float(row["normal_lo"]),
                "hi": float(row["normal_hi"]),
                "drift": int(row.get("drift_level") or 2),
                "noise": int(row.get("noise_level") or 2),
                "links": (row.get("links") or "").strip(),
            }
    return out


def _decimals(spec):
    text = spec["nom_text"]
    i = text.find(".")
    n = 0 if i < 0 else len(text) - i - 1
    return min(4, max(n, 2 if abs(spec["hi"] - spec["lo"]) < 5 else 1))


def _round_half_up(v, nd):
    """JS toFixed 와 같은 방향으로 반올림한다(파이썬 기본은 짝수 반올림)."""
    f = 10 ** nd
    return math.floor(abs(v) * f + 0.5) / f * (1 if v >= 0 else -1)


# --- 정상 베이스라인 --------------------------------------------------

def series(tag, spec, seed, n, step=STEP):
    d = spec
    rng_span = abs(d["hi"] - d["lo"]) or abs(d["nom"]) * 0.1 or 1
    amp = rng_span * DRIFT[min(2, max(0, d["drift"] - 1))]
    sig = rng_span * NOISE[min(2, max(0, d["noise"] - 1))]

    rnd = Rng(_i32(seed ^ _hash(tag)))
    p1 = 1200 + rnd() * 2400
    p2 = 300 + rnd() * 600
    f1 = rnd() * 6.283
    f2 = rnd() * 6.283

    nom = d["nom"]
    two_pi = 2 * math.pi
    out = [0.0] * n
    for i in range(n):
        t = i * step
        out[i] = (nom
                  + amp * math.sin(two_pi * t / p1 + f1)
                  + amp * 0.4 * math.sin(two_pi * t / p2 + f2)
                  + sig * _gauss(rnd))
    return out


def apply_links(data, spec, n, step=STEP):
    """태그 마스터 links 대로 변화를 이웃 태그에 전파한다."""
    add = {k: [0.0] * n for k in data}
    for src, d in spec.items():
        if src not in data or not d["links"]:
            continue
        s_nom = d["nom"]
        s_range = abs(d["hi"] - d["lo"]) or 1
        for part in d["links"].split(","):
            bits = [b.strip() for b in part.split(":")]
            tgt = bits[0]
            if len(bits) < 2 or tgt not in data:
                continue
            try:
                gain = float(bits[1])
            except ValueError:
                continue
            lag = int(round((float(bits[2]) if len(bits) > 2 and bits[2] else 0) / step))
            t_range = abs(spec[tgt]["hi"] - spec[tgt]["lo"]) or 1
            k = gain * (t_range / s_range)
            src_v, tgt_add = data[src], add[tgt]
            for i in range(n):
                tgt_add[i] += k * (src_v[max(0, i - lag)] - s_nom)
    for k, col in add.items():
        row = data[k]
        for i in range(n):
            row[i] += col[i]


# --- 파형 -------------------------------------------------------------

def ramp(D, tag, s, e, per_hour):
    row = D[tag]
    for i in range(s, e):
        row[i] += per_hour * (i - s) * STEP / 3600


def osc(D, tag, s, e, amp, period, decay):
    row = D[tag]
    span = (e - s) * STEP
    for i in range(s, e):
        t = (i - s) * STEP
        k = math.exp(-3 * t / span) if decay else 1.0
        row[i] += amp * k * math.sin(2 * math.pi * t / period)


def plateau(D, tag, s, e, target):
    row = D[tag]
    for i in range(s, e):
        row[i] = target + (row[i] - target) * 0.25


def freeze(D, tag, s, e):
    row = D[tag]
    v = row[s]
    for i in range(s, e):
        row[i] = v


def step_to(D, tag, s, e, delta, frac=0.5):
    row = D[tag]
    m = s + max(1, round((e - s) * frac))
    for i in range(s, e):
        f = (i - s) / (m - s) if i < m else 1.0
        row[i] += delta * f


# --- 시나리오 20종 ----------------------------------------------------

def _s3(D, s, e):
    for k, a in enumerate((6, 9, 13)):
        b = s + round(k * 7200 / STEP)
        n = round(60 / STEP)
        if b + n < e:
            osc(D, "ZI-601", b, b + n, a, 20, False)


def _s5(D, s, e):
    n = round(10 / STEP)
    for k in range(5):
        b = s + round(k * 1440 / STEP)
        for i in range(b, min(b + n, e)):
            D["FI-602"][i] -= 39800 * 0.05


def _s6(D, s, e):
    span = e - s
    lead = round(300 / STEP)
    for i in range(s, e):
        u = (i - s) / span
        g = (math.exp(3 * u) - 1) / (math.exp(3) - 1)
        D["MI-302"][i] += 1.7 * g
        j = i - lead
        if j >= s:
            D["PDI-303"][j] += 0.08 * g


def _s8(D, s, e):
    h = (e - s) * STEP / 3600
    ramp(D, "PI-203", s, e, 5.85 * 0.03 / h)
    ramp(D, "FI-204", s, e, -42500 * 0.06 / h)


def _s12(D, s, e):
    n = round(4 / STEP)
    for k in range(5):
        b = s + round(k * 1440 / STEP)
        for i in range(b, min(b + n, e)):
            D["MI-804"][i] = 999.0


def _s13(D, s, e):
    n = round(180 / STEP)
    for k in range(5):
        b = s + round(k * 1440 / STEP)
        if b + n < e:
            osc(D, "ZI-904", b, b + n, 5, 45, False)


def _s14(D, s, e):
    for i in range(s, e):
        D["SI-507"][i] -= 800


SCEN = [
    dict(id=1, nm="순도 헌팅", tag="AI-707", tags=["AI-707"], dur=1800,
         pre=lambda D, s, e: osc(D, "AI-707", s, e, 0.42, 180, True)),
    dict(id=2, nm="토출온도 드리프트", tag="TI-205", tags=["TI-205"], dur=43200,
         pre=lambda D, s, e: ramp(D, "TI-205", s, e, 0.68)),
    dict(id=3, nm="개도 반복변동 (진폭 증가)", tag="ZI-601",
         tags=["ZI-601", "FI-602", "PI-604", "LI-701"], dur=14460, pre=_s3),
    dict(id=4, nm="인입온도 임계 근접", tag="TI-401", tags=["TI-401"], dur=2400,
         pre=lambda D, s, e: plateau(D, "TI-401", s, e, 19)),
    dict(id=5, nm="공급유량 헌팅", tag="FI-602",
         tags=["FI-602", "PDI-702", "TI-704", "FI-710", "LI-701"], dur=7200, pre=_s5),
    dict(id=6, nm="흡착탑 수분 파과", tag="MI-302", tags=["MI-302", "PDI-303"],
         dur=5400, pre=_s6),
    dict(id=7, nm="탱크 인입밸브 고착", tag="ZI-801", tags=["ZI-801", "LI-802"],
         dur=7200,
         pre=lambda D, s, e: (freeze(D, "ZI-801", s, e), ramp(D, "LI-802", s, e, 6))),
    dict(id=8, nm="압축기 서지 전조", tag="PI-203",
         tags=["PI-203", "FI-204", "JI-206", "TI-205"], dur=1500, pre=_s8),
    dict(id=9, nm="Cold end 저온 이탈", tag="TI-403", tags=["TI-403"], dur=10800,
         pre=lambda D, s, e: ramp(D, "TI-403", s, e, 1.5)),
    dict(id=10, nm="LIN 탱크 레벨 임계 근접", tag="LI-806", tags=["LI-806"], dur=7200,
         pre=lambda D, s, e: plateau(D, "LI-806", s, e, 87.5)),
    dict(id=11, nm="개도–유량 상관 붕괴", tag="FI-602",
         tags=["FI-602", "ZI-601", "PI-604", "LI-701", "FI-710"], dur=3600,
         pre=lambda D, s, e: osc(D, "ZI-601", s, e, 4, 900, False),
         post=lambda D, s, e: freeze(D, "FI-602", s, e)),
    dict(id=12, nm="분석기 순간 이상값", tag="MI-804", tags=["MI-804"], dur=7200,
         post=_s12),
    dict(id=13, nm="GN2 공급압 반복 헌팅", tag="ZI-904",
         tags=["ZI-904", "PI-901", "FI-902"], dur=7200, pre=_s13),
    dict(id=14, nm="터빈 회전수 순간 강하", tag="SI-507", tags=["SI-507"], dur=20,
         pre=_s14),
    dict(id=15, nm="순도 동반 저하", tag="AI-707", tags=["AI-707", "AI-803"],
         dur=7200,
         pre=lambda D, s, e: ramp(D, "AI-707", s, e, -0.3 / ((e - s) * STEP / 3600))),
    dict(id=16, nm="흡착탑 과온", tag="TI-301", tags=["TI-301"], dur=2400,
         alarm="H 50℃", pre=lambda D, s, e: step_to(D, "TI-301", s, e, 22.9, 0.5)),
    dict(id=17, nm="GN2 공급압 저하", tag="PI-901", tags=["PI-901"], dur=3600,
         alarm="L 4.50 bar", pre=lambda D, s, e: step_to(D, "PI-901", s, e, -0.50, 0.4)),
    dict(id=18, nm="HP Column 고레벨", tag="LI-701", tags=["LI-701"], dur=5400,
         alarm="H 40%", pre=lambda D, s, e: step_to(D, "LI-701", s, e, 18.6, 0.6)),
    dict(id=19, nm="흡입 필터 차압 상승", tag="PDI-103", tags=["PDI-103"], dur=10800,
         alarm="H 0.030 bar",
         pre=lambda D, s, e: step_to(D, "PDI-103", s, e, 0.021, 1.0)),
    dict(id=20, nm="압축기 전력 과부하", tag="JI-206", tags=["JI-206"], dur=1800,
         alarm="H 3,600 kW", pre=lambda D, s, e: step_to(D, "JI-206", s, e, 440, 0.3)),
]

BY_ID = {s["id"]: s for s in SCEN}


# --- 회차 구성 --------------------------------------------------------

def pack(ids=None, shift_sec=43200, gap_sec=1800):
    """태그가 겹치지 않게 시나리오를 여러 회차로 나눈다.

    JS 는 한 번에 2~3종만 무작위로 넣는다. 20종을 전부 시험하려면 회차를
    나눠야 하고, 같은 근무 안에서는 **점유 태그가 겹치지 않아야** 서로의 파형을
    덮어쓰지 않는다.
    """
    todo = [BY_ID[i] for i in (ids or sorted(BY_ID))]
    rounds = []
    while todo:
        used_tags = set()
        cursor = 0
        picked, rest = [], []
        for s in todo:
            if set(s["tags"]) & used_tags or cursor + s["dur"] > shift_sec:
                rest.append(s)
                continue
            picked.append((s, cursor, cursor + s["dur"]))
            used_tags |= set(s["tags"])
            cursor += s["dur"] + gap_sec
        if not picked:                     # 남은 것이 한 근무에 안 들어가면 단독 회차로
            s = rest.pop(0)
            picked = [(s, 0, min(s["dur"], shift_sec))]
        rounds.append(picked)
        todo = rest
    return rounds


def generate(placements, seed=1, start="2026-08-22T06:00:00", shift_sec=43200):
    """한 회차를 만든다 -> ({태그: [(ts, value)]}, 정답지 dict)."""
    spec = load_spec()
    n = shift_sec // STEP
    t0 = datetime.fromisoformat(start)

    data = {tag: series(tag, d, seed, n) for tag, d in spec.items()}
    placed = [(s, si // STEP, min(n, ei // STEP)) for s, si, ei in placements]

    for s, i, j in placed:
        if s.get("pre"):
            s["pre"](data, i, j)
    apply_links(data, spec, n)
    for s, i, j in placed:
        if s.get("post"):
            s["post"](data, i, j)

    stamps = [(t0 + timedelta(seconds=i * STEP)).isoformat(timespec="seconds")
              for i in range(n)]
    out = {}
    for tag, row in data.items():
        nd = _decimals(spec[tag])
        out[tag] = [(stamps[i], _round_half_up(row[i], nd)) for i in range(n)]

    answer = {
        "source": "engine/_fixture.py (개발용 · 공식 측정 아님)",
        "seed": seed,
        "sampling_interval_sec": STEP,
        "window": [stamps[0], stamps[-1]],
        "injected": [
            {
                "scenario_id": s["id"],
                "name": s["nm"],
                "trigger_tag": s["tag"],
                "affected_tags": s["tags"],
                "start": stamps[i],
                "end": stamps[min(j, n - 1)],
                "duration_min": round((j - i) * STEP / 60),
                "dcs_alarm": s.get("alarm") or
                             ("연동 기여로 한계 초과" if s["id"] in (6, 10) else "미발생"),
            }
            for s, i, j in placed
        ],
    }
    return out, answer


def to_csv(series_map, path):
    """연결 규약 ① 형식으로 저장 — timestamp,tag,value."""
    tags = sorted(series_map)
    n = len(series_map[tags[0]])
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "tag", "value"])
        for i in range(n):
            for tag in tags:
                ts, v = series_map[tag][i]
                w.writerow([ts, tag, v])
    return path
