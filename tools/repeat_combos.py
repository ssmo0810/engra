#!/usr/bin/env python3
"""근무들에 걸쳐 반복되는 (태그, 종류) 를 찾는다.

**왜 필요한가.** 시연에서 「과거 조치」와 「이전에 제외한 것」 칸을 보이려면, 시연에 열
근무의 항목이 **앞 근무에도 나와야** 한다. 그런데 두 기능의 매칭 기준이 다르다.

    과거 조치   = 같은 **태그**        · 앞 근무에서 **채택**된 것
                  (app/db.py search_precedents · index_handover 는 채택만 색인한다)
    최하단 칸   = 같은 **(태그, 종류)** · 앞 근무에서 **제외**된 것
                  (app/db.py recent_exclusions, #30)

한 항목은 채택이거나 제외라 **둘 다 될 수 없다.** 그래서 두 기능을 한 화면에 같이
보이려면 서로 다른 태그로 나눠 배치해야 하고, 어느 태그가 어디에 반복되는지를 알아야 한다.

    python3 tools/repeat_combos.py                        # 반복 조합 전부
    python3 tools/repeat_combos.py --shift 2026-08-27-day # 그 근무 항목의 배치 후보

검출이 끝난 근무만 보인다(초안이 있어야 항목이 있다). 읽기만 하고 아무것도 바꾸지 않는다.
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app"))

import db  # noqa: E402


def load(conn):
    """(태그, 종류) -> [(근무, 중요도, 채택여부), ...]"""
    out = collections.defaultdict(list)
    for r in conn.execute(
        """SELECT d.shift_id sid, e.tag tag, e.kind kind, di.severity sev, di.adopted ad
             FROM draft_item di
             JOIN draft d ON d.id = di.draft_id
             JOIN event e ON e.id = di.event_id
            ORDER BY d.shift_id"""
    ):
        out[(r["tag"], r["kind"])].append((r["sid"], r["sev"], r["ad"]))
    return out


def mark(ad):
    return {1: "채택", 0: "제외"}.get(ad, "대기")


def show_all(combos):
    rep = {k: v for k, v in combos.items() if len(v) > 1}
    print("(태그, 종류) 조합 %d개 · 2근무 이상 반복 %d개\n" % (len(combos), len(rep)))
    if not rep:
        print("  반복되는 조합이 없습니다. 검출이 돈 근무가 하나뿐이면 이렇게 나옵니다.")
        return
    for (tag, kind), occ in sorted(rep.items(), key=lambda x: (-len(x[1]), x[0])):
        print("  %-9s %-9s %d근무 — %s"
              % (tag, kind, len(occ),
                 ", ".join("%s(%s)" % (s, mark(a)) for s, _, a in occ)))


def show_shift(conn, combos, shift_id):
    draft = db.load_draft(conn, shift_id)
    if draft is None:
        print("%s 의 초안이 없습니다. 먼저 검출을 돌리세요." % shift_id)
        return
    by_tag = collections.defaultdict(set)
    for (tag, kind), occ in combos.items():
        for s, _, _ in occ:
            by_tag[tag].add(s)

    kind_of = {}
    for it in draft["items"]:
        if it.get("event_id"):
            r = conn.execute("SELECT kind FROM event WHERE id = ?", (it["event_id"],)).fetchone()
            if r:
                kind_of[it["id"]] = r["kind"]

    print("%s 초안 %d항목 — 앞 근무에 같은 것이 있나\n" % (shift_id, len(draft["items"])))
    print("  %-9s %-9s %-30s %s" % ("태그", "종류", "과거 조치 후보(태그)", "최하단 칸 후보(태그+종류)"))
    print("  " + "-" * 92)
    for it in draft["items"]:
        tag = it.get("tag")
        kind = kind_of.get(it["id"])
        same_tag = sorted(s for s in by_tag.get(tag, ()) if s != shift_id)
        same_both = sorted(s for s, _, _ in combos.get((tag, kind), ()) if s != shift_id)
        print("  %-9s %-9s %-30s %s"
              % (tag or "-", kind or "-",
                 ", ".join(same_tag) or "없음",
                 ", ".join(same_both) or "없음"))
    print("\n  과거 조치를 보이려면  — 위 「과거 조치 후보」 근무에서 그 태그를 **채택**하고 코멘트를 답니다")
    print("  최하단 칸을 보이려면 — 위 「최하단 칸 후보」 근무에서 그 태그를 **제외**합니다")
    print("  둘은 서로 다른 태그로 나누세요. 한 항목은 채택이거나 제외라 둘 다 될 수 없습니다.")


def main():
    ap = argparse.ArgumentParser(description="근무들에 걸쳐 반복되는 (태그, 종류) 를 찾는다")
    ap.add_argument("--shift", help="이 근무의 항목을 앞 근무들과 대조한다")
    args = ap.parse_args()

    with db.connect() as conn:
        combos = load(conn)
        n = conn.execute("SELECT COUNT(*) FROM draft").fetchone()[0]
        if n < 2 and not args.shift:
            print("초안이 있는 근무가 %d개뿐입니다 — 반복을 세려면 둘 이상이 필요합니다.\n" % n)
        if args.shift:
            show_shift(conn, combos, args.shift)
        else:
            show_all(combos)


if __name__ == "__main__":
    main()
