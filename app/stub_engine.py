"""임시 엔진 — 정기영님 `engine/api.py` 가 나오면 자동으로 대체된다.

**일부러 단순하게 만들었다.** 하는 일은 태그 마스터의 알람 한계선(LL/L/H/HH)을
넘었는지 보는 것뿐이다. 헌팅·드리프트·상관 붕괴·값 고착처럼 알람에 안 걸리는
신호는 **하나도 못 잡는다.**

그게 의도다. `docs/scenarios.md` 20종 중 알람이 울리는 건 7종(6·10·16~20)이므로
이 스텁의 사정권은 거기까지이고, 알람 미발생 13종은 구조적으로 못 잡는다.
그 13종을 잡아내는 것이 곧 `engine/` 이 존재하는 이유이고, 통합 후 개선폭이
그대로 수치로 나온다.

주의 — 위 7/13 은 `docs/scenarios.md` 에서 도출한 설계값이지 실측이 아니다.
실측 포함률은 임도영님 정답지(`asu_answer_key.json`)가 도착한 뒤에만 말할 수 있다(#5).

여기 있는 알고리즘을 개선하지 말 것. 고칠 곳은 `engine/` 이다.
"""
import csv
import statistics
from datetime import datetime

from config import TAG_MASTER

_MAD_TO_SIGMA = 1.4826


# --- 태그 마스터 ------------------------------------------------------

def _load_tag_master():
    if not TAG_MASTER.exists():
        return {}
    out = {}
    with open(TAG_MASTER, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            tag = (row.get("tag") or "").strip()
            if not tag:
                continue
            spec = {"description": row.get("description", ""), "unit": row.get("unit", "")}
            for key in ("nominal", "normal_lo", "normal_hi", "LL", "L", "H", "HH"):
                raw = (row.get(key) or "").strip()
                spec[key] = float(raw) if raw else None
            out[tag] = spec
    return out


TAGS = _load_tag_master()


# --- 요약 -------------------------------------------------------------

def _mad(values, med):
    return statistics.median([abs(v - med) for v in values]) if values else 0.0


def summarize(series):
    """{태그: [(ts, value)]} -> {태그: 요약 통계}"""
    out = {}
    for tag, points in series.items():
        if not points:
            continue
        values = [v for _, v in points]
        med = statistics.median(values)
        try:
            hours = (
                datetime.fromisoformat(points[-1][0]) - datetime.fromisoformat(points[0][0])
            ).total_seconds() / 3600
        except ValueError:
            hours = 0
        out[tag] = {
            "n": len(values),
            "min": min(values),
            "max": max(values),
            "mean": statistics.fmean(values),
            "median": med,
            "mad": _mad(values, med),
            "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
            "first": values[0],
            "last": values[-1],
            "slope": (values[-1] - values[0]) / hours if hours else 0.0,
        }
    return out


# --- 기준선 -----------------------------------------------------------

def build_baseline(summaries):
    """지난 근무들의 요약 -> 태그별 기준선.

    중앙값의 중앙값을 쓴다. 한 근무에 이상이 있어도 기준선이 끌려가지 않는다.
    """
    out = {}
    for tag, rows in summaries.items():
        meds = [r["median"] for r in rows if r.get("median") is not None]
        mads = [r["mad"] for r in rows if r.get("mad") is not None]
        if not meds:
            continue
        med = statistics.median(meds)
        mad = statistics.median(mads) if mads else 0.0
        sigma = mad * _MAD_TO_SIGMA
        out[tag] = {
            "median": med,
            "mad": mad,
            "std": sigma,
            "band_lo": med - 3.5 * sigma,
            "band_hi": med + 3.5 * sigma,
            "n_shifts": len(rows),
            "params": {"method": "median-of-medians", "k": 3.5},
        }
    return out


# --- 검출 (알람 한계선만 본다) ----------------------------------------

def _josa(word, with_jong, without_jong):
    """받침 유무에 따라 조사를 고른다 ("미달이" / "초과가")."""
    if not word:
        return without_jong
    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return with_jong if (code - 0xAC00) % 28 else without_jong
    return without_jong


def _crossings(points, limit, above):
    """한계선을 넘은 첫 시각과 마지막 시각, 그리고 최대 이탈값."""
    hit = [(ts, v) for ts, v in points if (v > limit if above else v < limit)]
    if not hit:
        return None
    peak = max(hit, key=lambda p: p[1]) if above else min(hit, key=lambda p: p[1])
    return hit[0][0], hit[-1][0], peak[1]


def detect(series, baselines):
    """알람 한계선을 넘은 구간만 이벤트로 만든다. 그 외에는 아무것도 못 본다."""
    events = []
    for tag, points in series.items():
        spec = TAGS.get(tag)
        if not spec or not points:
            continue
        unit = spec.get("unit") or ""
        for key, above, severity in (
            ("HH", True, "상"), ("H", True, "중"), ("LL", False, "상"), ("L", False, "중"),
        ):
            limit = spec.get(key)
            if limit is None:
                continue
            found = _crossings(points, limit, above)
            if not found:
                continue
            start_ts, end_ts, peak = found
            direction = "초과" if above else "미달"
            extreme = "최고" if above else "최저"
            events.append({
                "tag": tag,
                "kind": f"{key} {direction}",
                "start_ts": start_ts,
                "end_ts": end_ts,
                "severity": severity,
                "score": abs(peak - limit),
                "metrics": {"limit": limit, "peak": round(peak, 3), "unit": unit},
                "evidence": (
                    f"{tag}({spec['description']}) {key} 한계 {limit}{unit} {direction} — "
                    f"{extreme} {round(peak, 3)}{unit}"
                ),
            })
            break  # 같은 방향에서 더 심한 것 하나만 남긴다
    events.sort(key=lambda e: (e["start_ts"] or "", e["tag"]))
    return events


# --- 초안 작성 (LLM 없이 문장 틀로만) ---------------------------------

def _hhmm(ts):
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M")
    except (ValueError, TypeError):
        return "??:??"


def compose(shift, events, find_precedents):
    """이벤트 -> 초안 항목. 2주 차에 Claude 로 교체할 자리다."""
    items = []
    for e in events:
        spec = TAGS.get(e["tag"], {})
        desc = spec.get("description") or e["tag"]
        precedents = find_precedents(e["tag"], desc) or []
        items.append({
            "event_id": e.get("id"),
            "origin": "detected",
            "tag": e["tag"],
            "severity": e.get("severity"),
            "title": f"{e['tag']} {desc} — {e['kind']}",
            "body": (
                f"{_hhmm(e.get('start_ts'))}~{_hhmm(e.get('end_ts'))} 구간에서 "
                f"{e['kind']}{_josa(e['kind'], '이', '가')} 확인되었습니다."
            ),
            "evidence": e.get("evidence"),
            "suggested_action": precedents[0]["text"][:120] if precedents else None,
            "precedents": precedents,
        })
    return items
