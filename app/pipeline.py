"""한 근무 구간을 처음부터 끝까지 돌린다.

    적재된 원본 -> 요약 -> 기준선 갱신 -> 검출 -> 초안

검출과 초안 작성은 직접 하지 않고 `ports` 를 거친다. 엔진이 준비되면
이 파일은 그대로 두고 `engine/api.py` 만 생기면 된다.
"""
import db
import llm
import ports

WAVE_PAD_SEC = 30 * 60      # 감지 구간 앞뒤 30분
WAVE_POINTS = 120           # 다운샘플 점 수 — SVG 폴리라인 한 줄이면 충분하다


def _waveform(points, start_ts, end_ts):
    """[(ts, value)] 에서 감지 구간 ±30분을 WAVE_POINTS 개로 줄인다. 그릴 재료만 남긴다."""
    from datetime import datetime, timedelta
    if not points or not start_ts or not end_ts:
        return None
    try:
        s = datetime.fromisoformat(start_ts) - timedelta(seconds=WAVE_PAD_SEC)
        t = datetime.fromisoformat(end_ts) + timedelta(seconds=WAVE_PAD_SEC)
    except ValueError:
        return None
    win = [(ts, v) for ts, v in points if s <= datetime.fromisoformat(ts) <= t]
    if len(win) < 2:
        return None
    step = max(1, len(win) // WAVE_POINTS)
    samp = win[::step]
    return {
        "t0": samp[0][0], "t1": samp[-1][0],
        "mark": [start_ts, end_ts],
        "v": [round(v, 4) for _, v in samp],
    }


def quality_summary(q):
    """품질 기록 → (제목 조각, 본문). 깨진 행(태그 귀속)과 시각 깨진 행(미귀속)이 각각 있을 때/없을 때 문구가 다르다 (Codex).
    화면 배너와 초안 항목이 같은 문장을 쓴다."""
    if not q or not (q.get("bad_rows") or q.get("unattributed_rows") or q.get("gaps")):
        return None
    by_tag = q.get("by_tag") or {}
    parts = []
    if q.get("bad_rows"):
        parts.append(f"{', '.join(list(by_tag)[:3])} 등 {q['bad_rows']}행" if by_tag else f"{q['bad_rows']}행")
    if q.get("unattributed_rows"):
        parts.append(f"시각 깨진 행 {q['unattributed_rows']}행")
    if q.get("gaps"):
        parts.append(f"계측 결측 구간 {len(q['gaps'])}개(" + ", ".join(f"{g['tag']} {g['minutes']}분" for g in q["gaps"][:3]) + ")")
    head = " + ".join(parts) + (" (사용자가 건너뛰기를 택함)" if q.get("skipped_by_user") else "")
    body = ("적재 시 값이 비었거나 형식이 깨진 행을 건너뜀" if (q.get("bad_rows") or q.get("unattributed_rows")) else "값이 들어오지 않은 구간이 있음")
    if q.get("ratio_file"):
        body += f" (파일 전체의 {q['ratio_file']:.1%})"
    if by_tag:
        body += ". 태그별: " + ", ".join(f"{t} {n}행" for t, n in by_tag.items()) + " — 이 태그들의 검출 결과는 결측 구간을 반영하지 않으므로 신뢰도가 낮다"
    if q.get("unattributed_rows"):
        body += f". 시각이 깨져 어느 근무인지 알 수 없는 행 {q['unattributed_rows']}행은 이 파일의 모든 근무에 걸쳐 있을 수 있다"
    return head, body + "."


def quality_items(q):
    """초안 맨 앞에 들어가는 '원본 데이터 품질' 항목들. 경모님(2026-08-27) 케이스 구분:
    - 계측 결측 구간(태그가 10분 넘게 값 없음): 구간마다 한 항목 — 그 태그·그 시간대는 실제 값을 못 받았다.
    - 산발 결측(여러 태그에 흩어짐): 요약 한 항목 — 전송 문제 의심, 재수집 권고.
    - 소량(1% 이하 몇 행): 요약 한 항목."""
    if not q:
        return []
    items = []
    for g in (q.get("gaps") or [])[:8]:
        items.append({"origin": "quality", "tag": g["tag"], "event_id": None,
                      "title": f"계측 결측 — {g['tag']} {g['start'][11:16]}~{g['end'][11:16]} ({g['minutes']}분) 실제 값 없음",
                      "body": f"{g['tag']} 는 이 구간에 값이 들어오지 않았다(행이 빠졌거나 비어 있음). 이 태그의 이 시간대 검출·추세는 판단 불가 — 계측기·통신 상태 확인 대상.",
                      "evidence": f"{g['start']} ~ {g['end']} 값 없음", "severity": "중", "suggested_action": None, "precedents": []})
    # 갭 항목이 이미 덮은 태그의 깨진 행은 요약에서 뺀다 — 같은 사건이 두 항목으로 보였다(실제 AI 실행이 "[0]과 동일 사건" 이라 지적).
    # 연속 구간(runs)에 든 깨진 행만 뺀다 — 같은 태그라도 구간 밖 산발 행은 요약에 남아야 한다 (Codex).
    rest = (q.get("bad_rows") or 0) - sum(r.get("rows", 0) for r in (q.get("runs") or []))
    qs = quality_summary(q)
    if qs and (rest > 0 or q.get("unattributed_rows")):
        head, body = qs
        kind = q.get("pattern") or ""
        title = ("산발 결측 — " if kind == "산발" else "원본 데이터 결측/형식 오류 — ") + head
        if kind == "산발":
            body = f"{q.get('affected_tags', 0)}개 태그에 흩어져 값이 비었다 — 데이터 전송 문제로 보인다. RTDB 에서 다시 내려받아 확인 권고. " + body
        top = list((q.get("by_tag") or {}))[:1]
        items.append({"origin": "quality", "tag": top[0] if top else "-", "event_id": None, "title": title, "body": body,
                      "evidence": ("예: " + " / ".join(q.get("samples") or [])) if q.get("samples") else "적재 로그 참조",
                      "severity": "중", "suggested_action": None, "precedents": []})
    return items


def quality_item(q):
    """(호환) 첫 항목만."""
    its = quality_items(q)
    return its[0] if its else None


def run(shift_id, verbose=True, redo=False, say=None):
    """say: 진행 문장을 받을 콜백. 화면(jobs.py)이 넘긴다. 없으면 stdout."""
    _cb = say

    def say(msg):
        if _cb is not None:
            _cb(msg)
            return
        if verbose:
            print(f"  {msg}")

    result = {"shift_id": shift_id, "engine": ports.engine_source()}

    with db.connect() as conn:
        # 확정된 일지는 기록이다. 다시 검출하면 그 기록의 근거가 사라진다.
        if db.load_handover(conn, shift_id) is not None:
            if not redo:
                raise ValueError(
                    f"{shift_id} 는 이미 확정된 근무입니다. 확정 일지를 지우고 다시 만들려면 "
                    f"--redo 를 붙이세요."
                )
            db.clear_outputs(conn, shift_id, include_handover=True)
            say("확정 일지를 지우고 다시 만듭니다 (--redo)")

        series = db.load_series(conn, shift_id)
        if not series:
            raise ValueError(
                f"{shift_id} 구간에 적재된 데이터가 없습니다. 먼저 ingest 를 실행하세요."
            )
        result["tags"] = len(series)
        result["samples"] = sum(len(v) for v in series.values())
        say(f"시계열 {result['tags']}개 태그 · {result['samples']:,}점")

        # 1) 요약 — 원본이 회전으로 사라져도 이건 남는다
        summaries = ports.summarize(series)
        db.save_summaries(conn, shift_id, summaries)
        say(f"요약 {len(summaries)}건 저장")

        # 2) 기준선 — 이번 근무는 빼고 만든다.
        #    이상이 있는 근무로 그 근무의 기준선을 만들면 이상이 정상으로 보인다.
        past = db.load_summaries(conn, exclude_shift=shift_id)
        provisional = False
        if not past:
            past = db.load_summaries(conn)
            provisional = True
            say("지난 근무가 없어 이번 구간으로 임시 기준선을 만듭니다 (잠정)")
        # 원본이 깨졌던 (근무, 태그) 의 요약은 기준선 재료로 쓰지 않는다 — 결측을 모른 채 낸 통계가 '평소' 를 왜곡한다 (홀리스틱 Codex)
        bad = db.quality_bad_pairs(conn)
        if bad:
            dropped = 0
            for tag in list(past):
                keep = [s for s in past[tag] if (s["shift_id"], tag) not in bad]
                dropped += len(past[tag]) - len(keep)
                if keep:
                    past[tag] = keep
                else:
                    del past[tag]
            if dropped:
                say(f"기준선 재료에서 품질 나쁜 요약 {dropped}건 제외")
        baselines = ports.build_baseline(past)
        db.save_baselines(conn, baselines)
        result["baseline_tags"] = len(baselines)
        result["baseline_provisional"] = provisional
        say(f"기준선 {len(baselines)}개 태그 갱신")

        # 3) 검출
        events = ports.detect(series, db.load_baselines(conn))
        # 원본은 3일 뒤 폐기되지만 이벤트는 영구 보존이라, 감지 구간 파형을 지금 떠 둔다.
        # 기획서 4-3 "이벤트(감지 구간의 지표 및 해당 구간 파형)" 가 이것이다.
        for e in events:
            e["waveform"] = _waveform(series.get(e["tag"]) or [], e.get("start_ts"), e.get("end_ts"))
        db.save_events(conn, shift_id, events, ports.engine_source())
        result["events"] = len(events)
        say(f"이벤트 {len(events)}건 검출 ({ports.engine_source()})")

        # 4) 초안 — 저장된 이벤트를 다시 읽어 id 를 달아 넘긴다
        stored = db.load_events(conn, shift_id)

        def find_precedents(tag, query):
            return db.search_precedents(conn, tag, query)

        shift = dict(conn.execute(
            "SELECT * FROM shift WHERE id = ?", (shift_id,)
        ).fetchone())
        items = ports.compose(shift, stored, find_precedents)
        quality = db.load_quality(conn, shift_id)
        qis = quality_items(quality)
        if qis:
            # 원본이 깨진 근무는 그 사실 자체가 인수인계 대상이다 — 그 태그의 검출은 결측을 모른 채 나온 것이라 신뢰도가 낮다.
            items[0:0] = qis
            say(f"원본 품질 항목 {len(qis)}개 추가 — " + "; ".join(i["title"] for i in qis[:3]))
        shift["quality"] = quality

        # 4-1) AI 서술 — 문장만 다시 쓴다. 근거·숫자는 이벤트 metrics 로 넘기고 출력에서는 뺀다.
        # 실패하면 여기서 멈춘다. AI 없이 만든 초안을 저장하지 않는다 (llm.py 원칙 1).
        # 대표 이벤트의 시각도 얹는다 — 근거 문장엔 시각이 없어서, 연속성 판정(llm 2단계)이 근무 전체의 시간대를 대조할 재료가 없었다.
        by_event = {e["id"]: e for e in stored}
        for it in items:
            e = by_event.get(it.get("event_id"))
            it["metrics"] = dict(e.get("metrics") or {}, start_ts=e.get("start_ts"), end_ts=e.get("end_ts")) if e else {}
        conn.commit()   # 여기까지의 쓰기를 확정하고 잠금을 놓는다 — AI 가 수 분 걸리는 동안 승인 화면이 "database is locked" 로 막혔다 (경모님 QA 2026-08-27)
        items, ai_status = llm.rewrite(shift, items, say=say)
        for it in items:
            it.pop("metrics", None)             # 저장 스키마엔 없는 임시 필드
        result["ai"] = ai_status
        say(f"서술 작성 — {ai_status}")

        # 4-2) 지난 근무에서 근무자가 제외한 것과 같은 (태그, 종류) 에 표시를 단다.
        # AI 호출 **뒤**에 붙인다 — 프롬프트에 넣으면 AI 가 "전에 제외됐으니 괜찮다" 로
        # 판정을 접는다(침묵 사고와 같은 경로). 화면과 조립만의 관심사다.
        # 항목을 지우거나 빼지 않는다. 초안 최하단으로 자리만 옮긴다 (#30).
        prev_ex = db.recent_exclusions(conn, shift_id)
        kind_of = {e["id"]: e.get("kind") for e in stored}
        marked = 0
        for it in items:
            info = prev_ex.get((it.get("tag"), kind_of.get(it.get("event_id"))))
            if info:
                it["prev_excluded"] = info
                marked += 1
        result["prev_excluded"] = marked
        if marked:
            say(f"지난 근무에서 제외했던 항목 {marked}개 — 최하단으로 내립니다 (숨기지 않음)")

        db.save_draft(conn, shift_id, items, ports.engine_source(), model=(llm.MODEL if llm.mode() != "off" else None))
        result["items"] = len(items)
        say(f"초안 {len(items)}개 항목 생성 — 승인 대기")

        # 5) 보관 기간이 지난 원본 정리
        removed = db.rotate_raw(conn)
        if removed:
            say(f"보관 기간 지난 원본 {removed:,}점 정리")
        result["rotated"] = removed

    return result
