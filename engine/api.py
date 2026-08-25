"""검출 엔진 — `app/ports.py` 가 찾는 네 함수.

    summarize(series)                       -> {태그: 요약}
    build_baseline(summaries)               -> {태그: 기준선}
    detect(series, baselines)               -> [이벤트, ...]
    compose(shift, events, find_precedents) -> [초안항목, ...]

이 파일은 얇다. 실제 계산은 `engine/core.py`(블록·통계)와
`engine/detectors.py`(증상별 검출기 7종)에 있고, 여기서는 순서를 정하고
결과를 묶는 일만 한다.

**임시 엔진과 무엇이 다른가.** 임시 엔진은 알람 한계선(LL/L/H/HH)을 넘었는지만
본다. 그래서 `docs/scenarios.md` 20종 중 알람이 울리는 7종만 사정권이다.
이 엔진은 알람에 걸리지 않는 13종 — 헌팅·드리프트·고착·상관 붕괴·순간 이탈·
임계 근접 — 을 잡는 것이 존재 이유다. 알람 7종도 그대로 잡되, **알람이 울리기
전에 추세로 먼저** 잡는 것을 목표로 한다.
"""
import statistics

from engine import detectors
from engine.core import (MAD_TO_SIGMA, SPIKE_Z, TAGS, build_views, epoch, hhmm,
                         label, mad, median, mod_z, spec_of)

ENGINE_VERSION = "0.1"


# --- 1. 요약 ----------------------------------------------------------

def summarize(series):
    """{태그: [(ts, value)]} -> {태그: 요약 통계}.

    `app/db.py` 의 shift_summary 표에 그대로 들어가므로 **열 이름을 바꾸면 안 된다.**

    스파이크를 뺀 뒤에 통계를 낸다(Issue #2). 999ppm 한 점이 median/MAD 에
    들어가면 그 태그의 기준선이 통째로 부풀고, 다음 근무부터 **같은 이상을 못
    잡는다.** 탐지는 원본으로 하고 기준선만 정제하는 것이 요점이다.
    """
    out = {}
    for tag, points in series.items():
        if not points:
            continue
        raw = [v for _, v in points]
        med0 = median(raw)
        mad0 = mad(raw, med0)
        clean = [v for v in raw if abs(mod_z(v, med0, mad0)) <= SPIKE_Z] if mad0 else raw
        if not clean:
            clean = raw

        med = median(clean)
        try:
            hours = (epoch(points[-1][0]) - epoch(points[0][0])) / 3600
        except ValueError:
            hours = 0.0
        out[tag] = {
            "n": len(raw),
            "min": min(clean),
            "max": max(clean),
            "mean": statistics.fmean(clean),
            "median": med,
            "mad": mad(clean, med),
            "std": statistics.pstdev(clean) if len(clean) > 1 else 0.0,
            "first": clean[0],
            "last": clean[-1],
            "slope": (clean[-1] - clean[0]) / hours if hours else 0.0,
            "dropped": len(raw) - len(clean),
        }
    return out


# --- 2. 기준선 --------------------------------------------------------

def build_baseline(summaries):
    """지난 근무들의 요약 -> 태그별 기준선.

    중앙값의 중앙값을 쓴다. 한 근무가 통째로 이상이어도 기준선이 끌려가지 않는다.

    **기준선이 없어도 검출은 돈다.** 데모는 CSV 한 장을 넣고 바로 돌리는 경우가
    많고, 그때 `pipeline.py` 는 이번 구간으로 잠정 기준선을 만든다. 검출기는
    근무 안에서 스스로 '평소'를 구하므로(`core.TagView`) 기준선은 레벨 비교
    보조로만 쓴다. 여기서 만든 값이 부실해도 탐지가 무너지지 않는 구조다.
    """
    out = {}
    for tag, rows in summaries.items():
        meds = [r["median"] for r in rows if r.get("median") is not None]
        mads = [r["mad"] for r in rows if r.get("mad") is not None]
        if not meds:
            continue
        med = median(meds)
        m = median(mads) if mads else 0.0
        sig = m * MAD_TO_SIGMA
        out[tag] = {
            "median": med,
            "mad": m,
            "std": sig,
            "band_lo": med - 3.5 * sig,
            "band_hi": med + 3.5 * sig,
            "n_shifts": len(rows),
            "params": {
                "method": "median-of-medians",
                "k": 3.5,
                "engine": ENGINE_VERSION,
                "shift_median_spread": round(mad(meds, med) * MAD_TO_SIGMA, 6),
            },
        }
    return out


# --- 3. 검출 ----------------------------------------------------------

def _neighbours(tag):
    """태그 마스터 links 로 직접 연결된 태그들 (방향 무시)."""
    out = {t for t, _g, _l in spec_of(tag).get("links", ())}
    for src, spec in TAGS.items():
        if any(t == tag for t, _g, _l in spec.get("links", ())):
            out.add(src)
    return out


def _linked(a, b):
    """직접 연결됐거나, **같은 태그를 공유**하면 한 계통으로 본다.

    처음에는 태그 번호 첫 자리(계통 번호)가 같으면 묶었는데, 8xx 안에 LOX 탱크와
    LIN 탱크가 같이 들어 있어 **관계없는 두 탱크의 사건이 한 줄로 합쳐졌다.**
    선언된 연결만 쓰되 한 다리 건넌 것까지 인정하면, 서로를 직접 가리키지 않는
    토출압·토출유량(둘 다 소비전력을 가리킨다)은 묶이고 두 탱크는 갈라진다.
    """
    na, nb = _neighbours(a), _neighbours(b)
    return b in na or a in nb or bool(na & nb)


def _overlap(x, y):
    return not (x["end_ts"] < y["start_ts"] or y["end_ts"] < x["start_ts"])


def _group(events):
    """같은 원인으로 동시에 뜬 것들을 한 사건으로 묶는다.

    묶지 않으면 초안 상위 다섯 줄에 **같은 사건이 네 줄** 들어간다. 시나리오 8은
    토출압과 유량이 함께 움직이는 하나의 사건이고, 15는 두 순도가 시차를 두고
    함께 떨어지는 하나의 사건이다. 근무자에게는 한 줄이어야 한다.

    묶는 조건은 **시간이 겹치고, 태그 마스터에 연결이 적혀 있거나 같은 계통**일 때.
    걸러내지는 않는다 — 묶기만 하고 전부 남긴다(`engine/README.md`).
    """
    parent = list(range(len(events)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    for i in range(len(events)):
        for j in range(i + 1, len(events)):
            x, y = events[i], events[j]
            if x["tag"] == y["tag"] or not _overlap(x, y):
                if x["tag"] == y["tag"]:
                    union(i, j)
                continue
            if _linked(x["tag"], y["tag"]):
                union(i, j)
            pair = y["metrics"].get("pair") or x["metrics"].get("pair") or ()
            if x["tag"] in pair and y["tag"] in pair:
                union(i, j)

    groups = {}
    for i, e in enumerate(events):
        groups.setdefault(find(i), []).append(e)

    for gid, members in enumerate(sorted(groups.values(),
                                         key=lambda m: -max(e["score"] for e in m)), 1):
        tags = sorted({e["tag"] for e in members})
        for e in members:
            e["metrics"]["group"] = gid
            if len(tags) > 1:
                e["metrics"]["related_tags"] = [t for t in tags if t != e["tag"]]
    return events


_SEV_RANK = {"상": 0, "중": 1, "하": 2}


def detect(series, baselines):
    """구간 시계열 + 기준선 -> 이벤트 목록.

    순서는 블록 접기 → 검출기 7종 → 묶기 → 정렬. 중요도 순으로 정렬하되
    **걸러내지 않고 전부 돌려준다.** 통계 단계는 재현율을 우선하고, 정밀도는
    중요도 판정과 근무자의 선택이 담당한다는 방침 그대로다.
    """
    views = build_views(series, baselines)
    if not views:
        return []
    events = detectors.run_all(views)
    events = _group(events)
    events.sort(key=lambda e: (_SEV_RANK.get(e["severity"], 3), -e["score"],
                               e["start_ts"] or ""))
    return events


# --- 4. 초안 작성 -----------------------------------------------------

_KIND_LEAD = {
    "헌팅": "변동폭이 평소보다 커진 구간이 있었습니다.",
    "드리프트": "근무 내내 한 방향으로 서서히 움직였습니다.",
    "레벨시프트": "한 번 이동한 뒤 그 수준을 유지했습니다.",
    "임계근접": "알람은 울리지 않았지만 한계선 가까이 머물렀습니다.",
    "이탈": "순간적으로 크게 벗어났다가 곧 돌아왔습니다.",
    "고착": "값이 움직이지 않았습니다. 계기나 밸브를 확인해야 합니다.",
    "상관이탈": "평소 함께 움직이던 두 태그의 관계가 끊어졌습니다.",
}


def _lead(kind):
    if kind in _KIND_LEAD:
        return _KIND_LEAD[kind]
    return "알람 한계선을 넘었습니다."          # LL/L/H/HH 초과·미달


def compose(shift, events, find_precedents):
    """이벤트 + 과거 사례 -> 초안 항목.

    **한 사건에 한 항목.** `detect` 가 묶어 둔 group 단위로 접는다.

    문장 틀로 쓴다. 여기를 Claude 로 바꾸는 것은 박경모님 몫이고(HANDOFF 4-4),
    그때 이 함수가 만드는 `body` 대신 `evidence`·`metrics` 를 근거로 넘기면 된다.
    **숫자는 전부 metrics 에 들어 있으므로 모델이 지어낼 필요가 없다.**
    """
    buckets = {}
    for e in events:
        gid = (e.get("metrics") or {}).get("group") or f"solo-{e.get('id')}"
        buckets.setdefault(gid, []).append(e)

    items = []
    for members in buckets.values():
        # 대표는 점수가 아니라 **가장 오래 이어진 것**으로 고른다.
        # 순간 이탈은 점수가 크게 나오기 쉬운데, 3시간짜리 차압 상승을 놔두고
        # 그 안의 1분짜리 튐을 제목으로 뽑으면 인수인계 문장이 뒤집힌다.
        members.sort(key=lambda e: (_SEV_RANK.get(e.get("severity"), 3),
                                    -((e.get("metrics") or {}).get("duration_min") or 0),
                                    -(e.get("score") or 0)))
        head = members[0]
        tag = head["tag"]
        desc = spec_of(tag).get("description") or tag
        tags = sorted({m["tag"] for m in members})

        when = f"{hhmm(head.get('start_ts'))}~{hhmm(head.get('end_ts'))}"
        title = f"{tag} {desc} — {head['kind']}"
        if len(tags) > 1:
            title += f" 외 {len(tags) - 1}개 태그"

        lines = [f"{when} 구간. {_lead(head['kind'])}"]
        for m in members:
            lines.append(f"· {m.get('evidence')}")
        if len(tags) > 1:
            lines.append(f"함께 움직인 태그: {', '.join(t for t in tags if t != tag)}")

        precedents = find_precedents(tag, desc) or []
        items.append({
            "event_id": head.get("id"),
            "origin": "detected",
            "tag": tag,
            "severity": head.get("severity"),
            "title": title,
            "body": "\n".join(lines),
            "evidence": head.get("evidence"),
            "suggested_action": precedents[0]["text"][:120] if precedents else None,
            "precedents": precedents,
        })

    items.sort(key=lambda it: _SEV_RANK.get(it.get("severity"), 3))
    return items
