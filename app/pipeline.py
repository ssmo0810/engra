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

        # 4-1) AI 서술 — 문장만 다시 쓴다. 근거·숫자는 이벤트 metrics 로 넘기고 출력에서는 뺀다.
        # 실패하면 여기서 멈춘다. AI 없이 만든 초안을 저장하지 않는다 (llm.py 원칙 1).
        by_event = {e["id"]: (e.get("metrics") or {}) for e in stored}
        for it in items:
            it["metrics"] = by_event.get(it.get("event_id"), {})
        conn.commit()   # 여기까지의 쓰기를 확정하고 잠금을 놓는다 — AI 가 수 분 걸리는 동안 승인 화면이 "database is locked" 로 막혔다 (경모님 QA 2026-08-27)
        items, ai_status = llm.rewrite(shift, items)
        for it in items:
            it.pop("metrics", None)             # 저장 스키마엔 없는 임시 필드
        result["ai"] = ai_status
        say(f"서술 작성 — {ai_status}")

        db.save_draft(conn, shift_id, items, ports.engine_source())
        result["items"] = len(items)
        say(f"초안 {len(items)}개 항목 생성 — 승인 대기")

        # 5) 보관 기간이 지난 원본 정리
        removed = db.rotate_raw(conn)
        if removed:
            say(f"보관 기간 지난 원본 {removed:,}점 정리")
        result["rotated"] = removed

    return result
