"""AI 과거 조치 판정의 원자료를 뽑는다 — 공식수치 ⑤ 「후보 50 → 채택 33 · 기각 17」의 근거.

초안 항목마다 저장소는 두 목록을 남긴다.
  precedents_all_json  같은 태그로 찾은 과거 일지 전부 (모델에는 앞 3건까지 넘긴다 — app/llm.py)
  precedent_json       그중 AI 가 이번 증상에 맞다고 판정한 것
둘의 차이가 기각이다. 기각 사유는 항목의 precedent_note 에 AI 가 쓴 문장이다.

    python tools/precedent_dump.py <DB 경로> [출력 CSV]

집계(근무·항목·후보·채택·기각·사유 기재·중요도 재판정)를 먼저 출력해 공식수치와 대조할 수 있게 하고,
기각 건을 한 줄씩 CSV 로 쓴다. DB 는 읽기 전용으로 연다.
"""
import csv
import io
import json
import sqlite3
import sys

MODEL_SEES = 3   # app/llm.py _prompt 가 항목당 넘기는 과거 사례 수


def _key(p):
    return (p.get("shift_id"), (p.get("text") or "")[:120])


def dump(db_path, out_path=None):
    c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        """SELECT d.shift_id, i.seq, i.tag, i.title, i.severity, i.severity_rule, i.precedent_note,
                  i.precedents_all_json AS pa, i.precedent_json AS pf
           FROM draft_item i JOIN draft d ON d.id = i.draft_id
           ORDER BY d.shift_id, i.seq""").fetchall()

    cand = fit = with_cand = noted = resev = 0
    rejected = []
    for r in rows:
        if r["severity_rule"] and r["severity"] != r["severity_rule"]:
            resev += 1
        seen = json.loads(r["pa"] or "[]")[:MODEL_SEES]
        kept = {_key(p) for p in json.loads(r["pf"] or "[]")}
        if not seen:
            continue
        with_cand += 1
        cand += len(seen)
        fit += len(kept)
        if (r["precedent_note"] or "").strip():
            noted += 1
        for p in seen:
            if _key(p) not in kept:
                rejected.append({
                    "no": len(rejected) + 1,
                    "근무": r["shift_id"],
                    "태그": r["tag"],
                    "이번항목": (r["title"] or "").strip(),
                    "기각된_과거일지": p.get("shift_id", ""),
                    "과거일지_내용": " ".join((p.get("text") or "").split())[:160],
                    "AI판정사유": " ".join((r["precedent_note"] or "").split()),
                })

    shifts = len({r["shift_id"] for r in rows})
    print(f"근무 {shifts} · 항목 {len(rows)}")
    print(f"과거 조치 후보 {cand} → 채택 {fit} · 기각 {cand - fit}")
    print(f"판정 사유 기재 {noted}/{with_cand} · 중요도 재판정 {resev}/{len(rows)}")

    if out_path and rejected:
        # utf-8-sig — 엑셀에서 한글이 깨지지 않게
        with io.open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rejected[0]))
            w.writeheader()
            w.writerows(rejected)
        print(f"기각 {len(rejected)}건 → {out_path}")
    return rejected


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("사용법: python tools/precedent_dump.py <DB 경로> [출력 CSV]")
    dump(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
