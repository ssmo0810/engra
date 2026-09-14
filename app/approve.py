"""승인 처리.

근무자가 항목별로 채택·제외를 정하고 코멘트를 달면, 그것이 확정 일지가 되고
색인에 들어가 **다음 근무의 초안 재료**가 된다. 이 순환이 ENGRA 의 핵심이다.

제외한 항목도 지우지 않는다. 무엇이 걸러졌는지가 감도 조정과 근무자 간
기준 차이를 측정할 유일한 데이터이기 때문이다.
"""
import db


class StatusMissing(ValueError):
    """채택 항목에 완료/진행중 표시가 없다. items = [(항목id, 제목)] — 화면이 어느 항목인지 짚어 준다."""

    def __init__(self, items):
        self.items = items
        super().__init__("채택한 항목마다 완료/진행중을 골라야 승인됩니다: "
                         + ", ".join(f"#{i} {t or ''}".strip() for i, t in items))


def decide(shift_id, decisions, confirmed_by="근무자", carried=None):
    """decisions: {항목id: {"adopted": bool, "comment": str, "status": '완료'|'진행중'}}
    carried:   {원 항목id: {"status": '완료'|'진행중', "comment": str}} — 이월 항목을 이번 근무에서 어떻게 했나.

    채택한 항목은 상태가 있어야 확정된다(기본값 없음 — 안 읽고 승인하는 것을 막는다, 심사평 08).
    검사는 폼이 아니라 여기서 한다. 폼을 우회해도 상태 없는 채택은 저장되지 않는다.
    """
    with db.connect() as conn:
        draft = db.load_draft(conn, shift_id)
        if draft is None:
            raise ValueError(f"{shift_id} 의 초안이 없습니다.")

        known = {it["id"] for it in draft["items"] if it["origin"] != "carried"}   # 이월 행은 이번 판단으로 다시 쓴다
        unknown = set(decisions) - known
        if unknown:
            raise ValueError(f"이 초안에 없는 항목입니다: {sorted(unknown)}")

        for item_id, d in decisions.items():
            # 근무자가 중요도를 바꿨으면 그 값이 최종이다. AI 판정은 제안이지 결정이 아니다.
            if d.get("comment"):
                d["comment"] = str(d["comment"])[:1000]     # 공개 폼. 무제한이면 디스크 증식 경로
            adopted_flag = 1 if d.get("adopted") else 0
            # 제외 항목의 상태는 뜻이 없다. 잘못 들어온 값은 버린다 — 재검토 때 옛 상태가 따라오지 않게.
            status = d.get("status") if adopted_flag else None
            if status is not None and status not in db.ITEM_STATUSES:
                raise ValueError(f"상태는 {'/'.join(db.ITEM_STATUSES)} 중 하나입니다. 받은 것: {status!r}")
            sev = d.get("severity")
            if sev in ("상", "중", "하"):
                conn.execute(
                    """UPDATE draft_item SET adopted = ?, comment = ?, decided_at = ?, status = ?, severity = ?
                       WHERE id = ? AND draft_id = ?""",
                    (adopted_flag, d.get("comment"), db.now(), status, sev, item_id, draft["id"]),
                )
            else:
                conn.execute(
                    """UPDATE draft_item SET adopted = ?, comment = ?, decided_at = ?, status = ?
                       WHERE id = ? AND draft_id = ?""",
                    (adopted_flag, d.get("comment"), db.now(), status, item_id, draft["id"]),
                )

        # 손대지 않은 항목은 제외로 본다. 미결정 상태로 확정되면
        # 나중에 "왜 안 적혔는지" 를 알 수 없다.
        conn.execute(
            "UPDATE draft_item SET adopted = 0, decided_at = ? "
            "WHERE draft_id = ? AND adopted IS NULL",
            (db.now(), draft["id"]),
        )

        # 이월 항목 판단. 이 근무 앞에 열려 있는 것만 가리킬 수 있다.
        opened = {o["id"]: o for o in db.open_items(conn, shift_id)}
        carried = dict(carried or {})
        bad = set(carried) - set(opened)
        if bad:
            raise ValueError(f"이 근무 앞에 열려 있는 이월 항목이 아닙니다: {sorted(bad)}")
        for root_id, ch in carried.items():
            if ch.get("status") not in db.ITEM_STATUSES:
                raise ValueError(f"이월 항목 #{root_id} 의 상태는 {'/'.join(db.ITEM_STATUSES)} 중 하나입니다. 받은 것: {ch.get('status')!r}")
            if ch.get("comment"):
                ch["comment"] = str(ch["comment"])[:1000]
        db.save_carried(conn, draft["id"], carried, opened)

        final = db.load_draft(conn, shift_id)
        # 최종 상태를 DB 에서 다시 읽어 검사한다 — decisions 에 안 실린 채택(재검토 뒤 남은 것)도 걸린다.
        # 예외가 with 블록을 빠져나가면 위 UPDATE 는 되돌아간다.
        missing = [(it["id"], it["title"]) for it in final["items"]
                   if it["adopted"] == 1 and it["status"] not in db.ITEM_STATUSES]
        if missing:
            raise StatusMissing(missing)
        adopted = [it for it in final["items"] if it["adopted"] == 1 and it["origin"] != "carried"]
        excluded = [it for it in final["items"] if it["adopted"] == 0]
        carried_rows = [it for it in final["items"] if it["origin"] == "carried"]

        body = render(final["shift_id"], adopted, carried_rows)
        # 이미 확정된 근무를 다시 승인하는 경우 이전 확정본을 이력으로 남긴다.
        # confirm_handover 는 DELETE 후 INSERT 라 그냥 두면 이전 판이 흔적 없이 사라진다 —
        # 기획서 4-3 "확정 일지는 변경 이력이 남는 구조" 가 화면(재검토)에서만 지켜지고
        # 명령줄에서는 깨져 있었다.
        round_no = None
        if db.load_handover(conn, shift_id) is not None:
            round_no = db.reopen_handover(conn, shift_id, reason=f"재승인 — {confirmed_by}")
        db.confirm_handover(conn, shift_id, confirmed_by, body, len(adopted), len(excluded))
        db.index_handover(conn, shift_id, adopted + carried_rows)

    return {
        "shift_id": shift_id,
        "adopted": len(adopted),
        "excluded": len(excluded),
        "carried": len(carried_rows),
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


def render(shift_id, adopted, carried=()):
    """확정 일지 본문. carried = 이월 항목에 대한 이번 근무의 판단 행(origin='carried')."""
    lines = [f"[{shift_id}] 인수인계 일지", ""]
    if not adopted:
        lines.append("특이사항 없음")
    for i, it in enumerate(adopted, start=1):
        mark = {"detected": "", "quality": " (원본 품질)"}.get(it["origin"], " (직접 추가)")
        status = f" — {it['status']}" if it.get("status") else ""
        lines.append(f"{i}. [{it['severity'] or '-'}] {it['title']}{mark}{status}")
        if it.get("body"):
            lines.append(f"   {it['body']}")
        if it.get("evidence"):
            lines.append(f"   근거: {it['evidence']}")
        if it.get("comment"):
            lines.append(f"   코멘트: {it['comment']}")
        lines.append("")
    if carried:
        lines += ["", f"이월 항목 {len(carried)}건"]
        for it in carried:
            verb = "완료로 닫음" if it.get("status") == "완료" else "계속 진행중"
            lines.append(f"- {it['title']} (원 항목 #{it['carried_from']}) — {verb}")
            if it.get("comment"):
                lines.append(f"   코멘트: {it['comment']}")
    return "\n".join(lines).rstrip()
