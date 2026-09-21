"""초안을 설문 생성용 JSON 으로 덤프한다.

서버 화면(_view_pending)이 항목마다 보여주는 것과 같은 필드를 같은 순서로 담는다.
화면에 있는데 여기 없으면 설문이 화면과 달라진다 — 필드를 뺄 때는 화면도 같이 본다.
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "engra_app"))

# engra 저장소 위치. 이 파일은 docs/조사/<회차>/도구/ 에 있으므로 기본값은 네 단계 위.
# 다른 곳에서 돌릴 때는 ENGRA_REPO 로 지정한다.
REPO = os.environ.get("ENGRA_REPO") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, REPO)

import db  # noqa: E402
import pipeline  # noqa: E402


SIGMA_PAREN = re.compile(r"\s*[(（]\s*약?\s*[\d.]+\s*σ\s*[)）]")      # "(29σ)"
SIGMA_RESID = re.compile(r"잔차\s*[\d.]+\s*σ")                        # "잔차 6.4σ"
SIGMA_BARE = re.compile(r"(?<![\w.])[\d.]+\s*σ")                      # 남은 "6.4σ"
CORR_R = re.compile(r"\s*[,，]?\s*[(（]?\s*r\s*-?[\d.]+\s*[)）]?")      # "(r -0.945)"
SPREAD = re.compile(r"평소\s*산포의")


def pct_by_event(conn, shift_id):
    """이벤트마다 「평소보다 몇 % 벗어났나」를 실제 값에서 계산한다.

    σ 는 통계량이라 근무자에게 아무것도 말해 주지 않는다 (임도영 2026-09-14).
    바꿔 적으려면 숫자가 필요한데, 지어내면 안 되므로 이벤트가 실제로 들고 있는 값에서만 뽑는다:
      · 순간 이탈류 — metrics 의 value 와 local_median 이 있으니 그 둘로 바로 나온다.
      · 상관 이탈 — 값이 없다. 감지 구간 파형을 기준선 중앙값과 대 봐서 최대 이탈폭을 쓴다.
    둘 다 안 되면 숫자 없이 σ 표현만 지운다 — 없는 정밀도를 지어내는 것보다 낫다.
    """
    out = {}
    base = {r["tag"]: r["median"] for r in conn.execute("SELECT tag, median FROM baseline")}
    for e in conn.execute("SELECT id, tag, kind, metrics_json, waveform_json "
                          "FROM event WHERE shift_id=?", (shift_id,)):
        try:
            m = json.loads(e["metrics_json"] or "{}")
        except ValueError:
            m = {}
        pct = None
        v, lm = m.get("value"), m.get("local_median")
        if isinstance(v, (int, float)) and isinstance(lm, (int, float)) and lm:
            pct = (v - lm) / lm * 100.0
        elif e["waveform_json"] and base.get(e["tag"]):
            try:
                w = json.loads(e["waveform_json"])
            except ValueError:
                w = None
            vals = (w or {}).get("v") if isinstance(w, dict) else w
            med = base[e["tag"]]
            if vals and med:
                devs = [(x - med) / med * 100.0 for x in vals]
                pct = max(devs, key=abs)
        if pct is not None:
            out[e["id"]] = pct
    return out


def plain(text, pct):
    """통계 용어를 근무자 말로 바꾼다. pct 가 없으면 표현만 지운다."""
    if not text:
        return text
    if pct is None:
        say = ""
    else:
        way = "낮아짐" if pct < 0 else "높아짐"
        say = f"평소보다 최대 {abs(pct):.0f}% {way}"

    text = SIGMA_RESID.sub(say or "평소와 다르게 움직임", text)
    text = SIGMA_PAREN.sub(f" ({say})" if say else "", text)
    text = SIGMA_BARE.sub(say or "평소와 다름", text)
    text = CORR_R.sub("", text)
    text = SPREAD.sub("평소 오르내림의", text)
    return text.replace("  ", " ").strip()


def get(it, key, default=None):
    try:
        v = it[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


def main(shift_id, out_path):
    with db.connect() as conn:
        d = db.load_draft(conn, shift_id)
        quality = db.load_quality(conn, shift_id)
        pcts = pct_by_event(conn, shift_id)
        # 항목 → 그 항목의 대표 이벤트. σ 가 가리키는 것이 그 이벤트다.
        ev_of = {r[0]: r[1] for r in conn.execute(
            "SELECT di.id, di.event_id FROM draft_item di "
            "JOIN draft dr ON dr.id = di.draft_id WHERE dr.shift_id=?", (shift_id,))}
    if d is None:
        raise SystemExit(f"{shift_id} 초안이 없습니다.")

    qs = pipeline.quality_summary(quality)

    items = []
    changed = []
    for it in d["items"]:
        pre = get(it, "precedents", []) or []
        pct = pcts.get(ev_of.get(get(it, "id")))
        raw_title = get(it, "title", "")
        items.append({
            "id": get(it, "id"),
            "severity": get(it, "severity", "-"),
            "severity_rule": get(it, "severity_rule"),
            "severity_reason": plain(get(it, "severity_reason"), pct),
            "handover_worthy": get(it, "handover_worthy"),
            "title": plain(raw_title, pct),
            "body": plain(get(it, "body", ""), pct),
            "evidence": plain(get(it, "evidence", ""), pct),
            "related_tags_ai": get(it, "related_tags_ai", []) or [],
            "related_note": get(it, "related_note"),
            "precedents": [
                {
                    "shift_id": p.get("shift_id"),
                    "confirmed_at": (p.get("confirmed_at") or "")[:10],
                    "text": p.get("text") or "",
                }
                for p in pre[:3]
            ],
            "precedents_all": len(get(it, "precedents_all", []) or []),
            "precedent_note": get(it, "precedent_note"),
            "prev_excluded": get(it, "prev_excluded"),
            # 화면에서 기본으로 켜져 있는지 — 앞 근무자가 제외한 것은 꺼진 채 최하단으로 간다
            "default_on": get(it, "adopted") == 1 or (get(it, "adopted") is None and not get(it, "prev_excluded")),
            "demoted": bool(get(it, "prev_excluded")),
        })
        if plain(raw_title, pct) != raw_title:
            changed.append((get(it, "id"), raw_title, plain(raw_title, pct)))

    out = {
        "shift_id": shift_id,
        "status": d["status"],
        "generator": d["generator"],
        "generated_at": d["generated_at"],
        "quality": list(qs) if qs else None,
        "items": items,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"{shift_id}: 항목 {len(items)}개 → {out_path}")
    if changed:
        print("  쉬운 표현으로 바꾼 제목:")
        for cid, a, b in changed:
            print(f"    #{cid}")
            print(f"      전: {a}")
            print(f"      후: {b}")
    for i in items:
        print(f"  #{i['id']} [{i['severity']}] {i['title']}"
              f"{'  (최하단·이전 제외)' if i['demoted'] else ''}"
              f"{'  과거조치 ' + str(len(i['precedents'])) + '건' if i['precedents'] else ''}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
