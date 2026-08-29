"""승인 처리.

근무자가 항목별로 채택·제외를 정하고 코멘트를 달면, 그것이 확정 일지가 되고
색인에 들어가 **다음 근무의 초안 재료**가 된다. 이 순환이 ENGRA 의 핵심이다.

제외한 항목도 지우지 않는다. 무엇이 걸러졌는지가 감도 조정과 근무자 간
기준 차이를 측정할 유일한 데이터이기 때문이다.
"""
import db


def decide(shift_id, decisions, confirmed_by="근무자"):
    """decisions: {항목id: {"adopted": bool, "comment": str}}"""
    with db.connect() as conn:
        draft = db.load_draft(conn, shift_id)
        if draft is None:
            raise ValueError(f"{shift_id} 의 초안이 없습니다.")

        known = {it["id"] for it in draft["items"]}
        unknown = set(decisions) - known
        if unknown:
            raise ValueError(f"이 초안에 없는 항목입니다: {sorted(unknown)}")

        for item_id, d in decisions.items():
            # 근무자가 중요도를 바꿨으면 그 값이 최종이다. AI 판정은 제안이지 결정이 아니다.
            if d.get("comment"):
                d["comment"] = str(d["comment"])[:1000]     # 공개 폼. 무제한이면 디스크 증식 경로
            sev = d.get("severity")
            if sev in ("상", "중", "하"):
                conn.execute(
                    """UPDATE draft_item SET adopted = ?, comment = ?, decided_at = ?, severity = ?
                       WHERE id = ? AND draft_id = ?""",
                    (1 if d.get("adopted") else 0, d.get("comment"), db.now(), sev, item_id, draft["id"]),
                )
            else:
                conn.execute(
                    """UPDATE draft_item SET adopted = ?, comment = ?, decided_at = ?
                       WHERE id = ? AND draft_id = ?""",
                    (1 if d.get("adopted") else 0, d.get("comment"), db.now(), item_id, draft["id"]),
                )

        # 손대지 않은 항목은 제외로 본다. 미결정 상태로 확정되면
        # 나중에 "왜 안 적혔는지" 를 알 수 없다.
        conn.execute(
            "UPDATE draft_item SET adopted = 0, decided_at = ? "
            "WHERE draft_id = ? AND adopted IS NULL",
            (db.now(), draft["id"]),
        )

        final = db.load_draft(conn, shift_id)
        adopted = [it for it in final["items"] if it["adopted"] == 1]
        excluded = [it for it in final["items"] if it["adopted"] == 0]

        body = render(final["shift_id"], adopted)
        # 이미 확정된 근무를 다시 승인하는 경우 이전 확정본을 이력으로 남긴다.
        # confirm_handover 는 DELETE 후 INSERT 라 그냥 두면 이전 판이 흔적 없이 사라진다 —
        # 기획서 4-3 "확정 일지는 변경 이력이 남는 구조" 가 화면(재검토)에서만 지켜지고
        # 명령줄에서는 깨져 있었다.
        round_no = None
        if db.load_handover(conn, shift_id) is not None:
            round_no = db.reopen_handover(conn, shift_id, reason=f"재승인 — {confirmed_by}")
        db.confirm_handover(conn, shift_id, confirmed_by, body, len(adopted), len(excluded))
        db.index_handover(conn, shift_id, adopted)

    return {
        "shift_id": shift_id,
        "adopted": len(adopted),
        "excluded": len(excluded),
        "body": body,
        "prev_round": round_no,      # 재승인이면 이력으로 남긴 회차. 처음이면 None
    }


def add_manual(shift_id, title, body, tag=None, severity="중"):
    """근무자가 직접 추가하는 항목. 감지되지 않은 것도 일지에 들어가야 한다."""
    with db.connect() as conn:
        draft = db.load_draft(conn, shift_id)
        if draft is None:
            raise ValueError(f"{shift_id} 의 초안이 없습니다.")
        seq = max((it["seq"] or 0) for it in draft["items"]) + 1 if draft["items"] else 1
        cur = conn.execute(
            """INSERT INTO draft_item
               (draft_id, event_id, seq, origin, tag, title, body, severity, adopted)
               VALUES (?, NULL, ?, 'manual', ?, ?, ?, ?, 1)""",
            (draft["id"], seq, tag, title, body, severity),
        )
        return cur.lastrowid


def render(shift_id, adopted):
    """확정 일지 본문."""
    lines = [f"[{shift_id}] 인수인계 일지", ""]
    if not adopted:
        lines.append("특이사항 없음")
    for i, it in enumerate(adopted, start=1):
        mark = {"detected": "", "quality": " (원본 품질)"}.get(it["origin"], " (직접 추가)")
        lines.append(f"{i}. [{it['severity'] or '-'}] {it['title']}{mark}")
        if it.get("body"):
            lines.append(f"   {it['body']}")
        if it.get("evidence"):
            lines.append(f"   근거: {it['evidence']}")
        if it.get("comment"):
            lines.append(f"   코멘트: {it['comment']}")
        lines.append("")
    return "\n".join(lines).rstrip()
