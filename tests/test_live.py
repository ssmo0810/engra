"""실시간 누적(app/live.py) 시험 — 틱 실험이 정한 추적기 규칙 · 창 · 이어진 재생 · 마감 동기화 · 정지 · AI 실패 · 승인 순서.

    python3 tests/test_live.py

AI 는 부르지 않는다(ENGRA_LLM=off). 재생 시험은 ports.detect·compose 를 대본으로 바꿔 끼워 창 경계·귀속·추적·저장만 본다 —
실제 엔진으로 돌린 마감 초안이 일괄 실행과 같은지는 실제 재생으로 따로 확인한다.
규칙 값(M 30분 · K 3틱)과 근거는 app/live.py _Tracks 에 있다.
"""
import datetime as dt
import io
import itertools
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)

WS = dt.datetime(2026, 8, 25, 6, 0, 0)          # 2026-08-25-day 구간 시작
SID, NIGHT_SID, NEXT_SID, PREV_SID = "2026-08-25-day", "2026-08-25-night", "2026-08-26-day", "2026-08-24-night"
TAGS = ("TI-101", "PI-201")


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="engra_live_test_")
    os.close(fd)
    os.remove(path)
    os.environ["ENGRA_DB"] = path
    os.environ["ENGRA_LLM"] = "off"
    for m in ("db", "collect", "pipeline", "approve", "jobs", "ports", "config", "llm", "server", "live"):
        sys.modules.pop(m, None)
    import db
    db.init()
    return path


def T(minutes):
    return WS + dt.timedelta(minutes=minutes)


def iso(t):
    return t.isoformat(timespec="seconds")


def ev(tag, kind, start, end, group, score=1.0, sev="중"):
    """구간 시작 기준 분(또는 시각) 으로 만든 이벤트 한 건."""
    return {"tag": tag, "kind": kind, "start_ts": iso(start if isinstance(start, dt.datetime) else T(start)),
            "end_ts": iso(end if isinstance(end, dt.datetime) else T(end)), "severity": sev, "score": score,
            "metrics": {"group": group}, "evidence": f"{tag} {kind} 근거"}


def _csv(minutes=720, tags=TAGS, start=WS, extra=()):
    """1분에 한 점씩(시험을 가볍게). extra = 뒤에 붙일 (ts, tag, value)."""
    d = tempfile.mkdtemp(prefix="engra_live_csv_")
    path = os.path.join(d, f"shift_{start:%Y%m%d_%H}.csv")
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write("timestamp,tag,value\n")
        for i in range(minutes):
            for tag in tags:
                f.write(f"{iso(start + dt.timedelta(minutes=i))},{tag},{10.0 + i * 0.01}\n")
        for ts, tag, value in extra:
            f.write(f"{ts},{tag},{value}\n")
    return path


def _uploads(*paths):
    """jobs.UPLOAD_DIR 을 시험용 폴더로 바꿔 끼우고 CSV 를 그 안으로 옮긴다 → (폴더, 이름 목록)."""
    import jobs
    up = tempfile.mkdtemp(prefix="engra_live_up_")
    jobs.UPLOAD_DIR = Path(up)
    names = []
    for p in paths:
        shutil.move(p, os.path.join(up, os.path.basename(p)))
        names.append(os.path.basename(p))
    return up, names


def _shift_row(conn, sid, start, end):
    import db
    conn.execute("INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) VALUES (?,?,?,?,?,?)",
                 (sid, sid.rsplit("-", 1)[1], iso(start), iso(end), "test", db.now()))


def _pending_draft(conn, sid, titles=("항목",)):
    import db
    did = conn.execute("INSERT INTO draft (shift_id, status, generator, generated_at) VALUES (?,'pending','test',?)",
                       (sid, db.now())).lastrowid
    for seq, title in enumerate(titles, 1):
        conn.execute("INSERT INTO draft_item (draft_id, seq, origin, tag, title, body, severity) VALUES (?,?,'detected','TI-101',?,'본문','중')",
                     (did, seq, title))
    return [r[0] for r in conn.execute("SELECT id FROM draft_item WHERE draft_id = ? ORDER BY seq", (did,))]


def _prev_raw(minutes=720):
    """앞 근무 12시간 원본을 DB 에 둔다 (1분에 한 점)."""
    import db
    with db.connect() as conn:
        _shift_row(conn, PREV_SID, WS - dt.timedelta(hours=12), WS)
        conn.executemany("INSERT INTO raw_sample (tag, ts, value) VALUES (?,?,?)",
                         [(tag, iso(WS - dt.timedelta(hours=12) + dt.timedelta(minutes=i)), 5.0)
                          for i in range(minutes) for tag in TAGS])


class Script:
    """ports.detect·compose 대역. 틱 시각 t 를 원본의 마지막 점(1분 간격)에서 알아내 events(t) 를 돌려준다."""

    def __init__(self, events, question=False):
        self.events, self.question = events, question
        self.detects, self.composed = [], []

    def detect(self, series, baselines):
        lo = min(pts[0][0] for pts in series.values())
        hi = max(pts[-1][0] for pts in series.values())
        t = dt.datetime.fromisoformat(hi) + dt.timedelta(minutes=1)
        self.detects.append((lo, t))
        return [dict(e, metrics=dict(e["metrics"])) for e in self.events(t)]

    def compose(self, shift, events, find_precedents):
        """실제 compose 처럼 묶음(metrics.group)마다 항목 하나 — 대표는 그 묶음에서 먼저 온 이벤트."""
        self.composed.append([(e["tag"], e["kind"]) for e in events])
        rank, groups = {"상": 0, "중": 1, "하": 2}, {}
        for e in events:
            groups.setdefault(e["metrics"]["group"], []).append(e)
        # 실제 engine.compose 처럼 대표는 중요도(같으면 점수)가 가장 큰 멤버다
        heads = [sorted(mem, key=lambda x: (rank.get(x["severity"], 3), -(x["score"] or 0)))[0] for mem in groups.values()]
        items = [{"event_id": e["id"], "origin": "detected", "tag": e["tag"], "severity": e["severity"],
                  "title": f"{e['tag']} — {e['kind']}", "body": "본문", "evidence": e["evidence"], "precedents": []}
                 for e in heads]
        if self.question:
            items.insert(0, {"event_id": None, "origin": "question", "tag": None, "severity": "상",
                             "title": "플랜트를 정지하셨습니까?", "body": "본문", "evidence": "동시 이탈", "precedents": []})
        return items


def _clock():
    """부를 때마다 크게 뛰는 시계 — 재생이 기다리지 않고 끝까지 달린다."""
    c = itertools.count(0, 10 ** 6)
    return lambda: next(c)


def _replay(csvs, script, prev=True, replace=False):
    """재생 스레드 본체를 이 스레드에서 끝까지 돌린다(이름 검사·작업 락은 start 시험이 따로 본다)."""
    import live
    import ports
    if prev:
        _prev_raw()
    ports.detect, ports.compose = script.detect, script.compose
    paths = [Path(c) for c in (csvs if isinstance(csvs, (list, tuple)) else [csvs])]
    r = live._Replay(live._plan(paths), 100, clock=_clock(), replace_unconfirmed=replace)
    live._replay = r
    r.run()
    return r


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class TrackerRules(unittest.TestCase):
    """추적기 판정 — tracker_analyze.run(detach=False) 규칙 그대로인가."""

    def setUp(self):
        _fresh_db()
        import live
        self.live = live
        self.tracks = live._Tracks()

    def P(self, *members):
        return self.live._Problem({"tag": members[0]["tag"], "title": "t", "origin": "detected"}, list(members))

    def step(self, minute, probs, closing=False):
        conf, rep, _gone = self.tracks.step(minute // 5, T(minute), probs, closing)
        return conf, rep

    def test_ended_waits_until_every_member_is_M_minutes_old(self):
        a = lambda: self.P(ev("TI-101", "드리프트", -60, 5, 1))
        for m in range(5, 35, 5):
            conf, _ = self.step(m, [a()])
            self.assertEqual(conf, [], f"{m}분: 끝 시각 06:05 가 M분 지나기 전에 확정됐다")
        conf, _ = self.step(35, [a()])
        self.assertEqual([tr.state for tr in conf], ["ended"], "06:35 = 끝 시각 + M 에서 끝남 확정")
        self.assertFalse(conf[0].ongoing)

    def test_vanishes_after_K_missing_ticks_and_returns_as_new(self):
        a = lambda m: self.P(ev("TI-101", "드리프트", 0, m, 1))
        self.step(5, [a(5)])
        self.step(10, [])
        self.step(15, [])
        self.assertEqual(self.tracks.all[0].state, "open", "K-1 틱 빠진 것은 아직 관찰 중")
        self.step(20, [])
        self.assertEqual(self.tracks.all[0].state, "vanished", "K 틱 연속 빠지면 사라짐")
        self.step(25, [a(25)])
        self.assertEqual(len(self.tracks.all), 2, "사라진 동일성이 다시 뜨면 새 추적기")
        self.assertEqual(self.tracks.stats["after_vanish"], 1)

    def test_closing_tick_closes_open_and_new_and_drops_missing(self):
        a = lambda m: self.P(ev("TI-101", "드리프트", 0, m, 1))
        b = lambda m: self.P(ev("PI-201", "레벨시프트", 0, m, 2))
        c = lambda m: self.P(ev("FI-301", "헌팅", 700, m, 3))
        d = lambda m: self.P(ev("LI-401", "이탈", 0, m, 4))
        self.step(690, [a(690), b(690), d(690)])
        self.step(695, [a(695), b(695)])
        self.step(700, [a(700), b(700)])
        self.step(705, [a(705), b(705)])            # d 는 3틱 빠져 사라짐
        self.assertEqual([tr.state for tr in self.tracks.all], ["open", "open", "vanished"])
        conf, rep = self.step(720, [a(720), c(720), d(720)], closing=True)
        by_tag = {}
        for tr in self.tracks.all:
            by_tag.setdefault(tr.prob.item["tag"], []).append(tr.state)
        self.assertEqual(by_tag["TI-101"], ["closed"], "마감 틱에 있는 관찰 중 = 마감 확정")
        self.assertEqual(by_tag["FI-301"], ["closed"], "마감 틱에 처음 뜬 문제 = 마감 확정")
        self.assertEqual(by_tag["PI-201"], ["vanished"], "마감 틱에 없는 관찰 중 = 사라짐")
        self.assertEqual(by_tag["LI-401"], ["vanished", "closed"], "사라짐 뒤 마감 틱에 있으면 새로 마감 확정")
        self.assertTrue(conf[0].ongoing, "마감 확정 때 끝 시각이 최근 M분 안이면 진행 중")
        self.assertTrue(all(x is not None for x in rep), "마감 틱의 모든 문제에 대표 추적기가 있어야 한다")

    def test_identity_follows_member_overlap_when_representative_changes(self):
        self.step(5, [self.P(ev("TI-101", "드리프트", 0, 5, 1))])
        self.step(10, [self.P(ev("PI-201", "레벨시프트", 6, 10, 1), ev("TI-101", "드리프트", 0, 10, 1))])
        self.step(15, [self.P(ev("PI-201", "레벨시프트", 6, 15, 1))])
        self.assertEqual(len(self.tracks.all), 1, "대표가 바뀌어도 멤버 tag·kind 가 겹치면 같은 문제")
        self.assertEqual(self.tracks.all[0].keys, frozenset({("PI-201", "레벨시프트")}))

    def test_ended_problem_is_remembered_and_not_reconfirmed(self):
        a = lambda: self.P(ev("PDI-103", "드리프트", -300, 5, 1))
        self.step(5, [a()])
        conf, _ = self.step(35, [a()])
        self.assertEqual(len(conf), 1)
        for m in range(40, 105, 5):
            self.step(m, [])                          # K 틱보다 오래 안 보인다
        for m in (105, 110):
            conf, _ = self.step(m, [a()])             # 앞 근무에서 끝난 드리프트가 다시 뜬다
            self.assertEqual(conf, [], "기억한 확정 문제를 다시 확정하면 안 된다")
        self.assertEqual(len(self.tracks.all), 1, "놓아 주면 새 추적기가 생겨 곧장 재확정된다(실측 PDI-103 6회)")

    def test_recurrence_only_when_moving_within_M(self):
        self.step(5, [self.P(ev("TI-101", "드리프트", 0, 5, 1))])
        self.step(35, [self.P(ev("TI-101", "드리프트", 0, 5, 1))])
        self.step(40, [self.P(ev("TI-101", "드리프트", 0, 38, 1))])
        self.assertEqual(len(self.tracks.all), 2, "최근 M분 안에 다시 움직이면 재발 = 새 추적기")
        new = self.tracks.all[1]
        self.assertEqual((new.state, new.recurrence_of), ("open", "p1"))
        self.assertFalse(self.tracks.all[0].attached, "앞 문제는 새 추적기에 동일성을 넘긴다")
        self.assertEqual(self.tracks.stats["recurrence"], 1)

    def test_end_time_jitter_does_not_recreate(self):
        self.step(5, [self.P(ev("TI-101", "드리프트", 0, 5, 1))])
        self.step(35, [self.P(ev("TI-101", "드리프트", 0, 5, 1))])
        for m, end in ((40, 9), (45, 14), (50, 19)):     # 확정 때(06:05)보다 늦어지지만 늘 지금 − M 보다 앞
            conf, _ = self.step(m, [self.P(ev("TI-101", "드리프트", 0, end, 1))])
            self.assertEqual(conf, [])
        self.assertEqual(len(self.tracks.all), 1, "끝 시각 흔들림으로 재생성하면 127~359건이 쏟아진다(실측)")

    def test_vanished_recurrence_restores_the_remembered_confirmation(self):
        a = lambda end: self.P(ev("TI-101", "드리프트", 0, end, 1))
        self.step(5, [a(5)])
        self.step(35, [a(5)])                       # 끝남 확정
        self.step(40, [a(38)])                      # 최근 M분 안 움직임 → 재발 자식
        self.assertEqual(len(self.tracks.all), 2)
        for m in (45, 50, 55):
            self.step(m, [])                        # 자식이 확정 전에 K틱 빠져 사라진다
        self.assertEqual(self.tracks.all[1].state, "vanished")
        self.assertTrue(self.tracks.all[0].attached, "자식이 사라지면 부모의 확정 기억을 되살린다")
        conf, _ = self.step(60, [a(5)])             # 같은 문제가 옛 끝 시각으로 다시 뜬다
        self.assertEqual((len(self.tracks.all), conf), (2, []), "기억이 살아 있어야 새 항목·새 AI 가 안 생긴다")

    def test_confirmed_at_birth_when_found_late_in_window(self):
        conf, _ = self.step(50, [self.P(ev("TI-101", "드리프트", -120, 10, 1))])
        self.assertEqual([tr.state for tr in conf], ["ended"])
        self.assertEqual(self.tracks.stats["ended_at_birth"], 1)
        self.assertEqual(self.tracks.max_open, 0, "회색 카드 없이 곧장 AI 로")

    def test_one_to_one_by_largest_overlap(self):
        self.step(5, [self.P(ev("TI-101", "드리프트", 0, 5, 1)),
                      self.P(ev("PI-201", "레벨시프트", 0, 5, 2), ev("FI-301", "헌팅", 0, 5, 2))])
        self.step(10, [self.P(ev("PI-201", "레벨시프트", 0, 10, 1), ev("FI-301", "헌팅", 0, 10, 1),
                              ev("TI-101", "드리프트", 0, 10, 1))])
        x, y = self.tracks.all
        self.assertEqual((y.miss, x.miss), (0, 1), "겹치는 tag·kind 가 많은 추적기가 잇는다")
        self.assertEqual(len(self.tracks.all), 2)


class ReplayWindow(unittest.TestCase):
    """재생 — 12시간 창 · 귀속 · (가)로 떨어지기 · 마감 동기화 · 저장."""

    def setUp(self):
        _fresh_db()

    def test_rolling_12h_window_and_attribution_by_end(self):
        def events(t):
            return [ev("TI-101", "드리프트", -200, -30, 1),       # 앞 근무 구간에서 끝남 — 이 근무 것이 아니다
                    ev("PI-201", "레벨시프트", 0, t, 2)]          # 계속 움직임
        s = Script(events)
        import jobs
        qa = []
        jobs.mark_qa_active = lambda: qa.append(1)
        r = _replay(_csv(), s)
        sec = r.sections[0]
        self.assertEqual((r.phase, sec.mode, sec.note), ("ended", "12h", None), r.error)
        self.assertGreaterEqual(len(qa), 144, "틱마다 정시 자동 비움 보호를 켠다(재생 도중 DB 를 비우지 않게)")
        self.assertEqual(s.detects[0], (iso(T(5) - dt.timedelta(hours=12)), T(5)), "첫 틱 창 = [t−12h, t)")
        self.assertEqual(s.detects[-1], (iso(WS), T(720)), "마감 틱 창 = [구간 시작, 끝) = 배치")
        self.assertEqual(len(s.detects), 144)
        self.assertTrue(all(("TI-101", "드리프트") not in c for c in s.composed), "끝 시각 < 구간 시작 이벤트는 조립에 안 들어간다")
        import db
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
            n_cur = conn.execute("SELECT COUNT(*) FROM raw_sample WHERE ts >= ?", (iso(WS),)).fetchone()[0]
            n_prev = conn.execute("SELECT COUNT(*) FROM raw_sample WHERE ts < ?", (iso(WS),)).fetchone()[0]
        self.assertEqual(d["status"], "pending", "마감 동기화와 AI 가 끝나면 승인 대기")
        self.assertEqual([it["title"] for it in d["items"]], ["PI-201 — 레벨시프트"])
        self.assertEqual(d["items"][0]["live"]["confirm"], "closed")
        self.assertEqual((n_cur, n_prev), (720 * 2, 720 * 2), "1분 묶음 덧붙이기가 앞 근무 원본을 지우지 않는다")

    def test_falls_back_to_shift_start_window_with_note_and_late_stop_question(self):
        import live
        s = Script(lambda t: [ev("PI-201", "레벨시프트", t - dt.timedelta(minutes=30), t, 1)], question=True)
        r = _replay(_csv(), s, prev=False)
        sec = r.sections[0]
        self.assertEqual(sec.mode, "shift_start", r.error)
        self.assertEqual(sec.note, "앞 근무 데이터 없음 — 초반 결과가 흔들릴 수 있음")
        self.assertEqual(sec.stop_q_from, T(65), "경과 60분 틱까지 막는다")
        self.assertEqual(s.detects[0], (iso(WS), T(5)), "(가) 창 = [구간 시작, t)")
        self.assertEqual([q.first_seen for q in sec.questions], [T(65)], "경과 60분 안에는 정지 질문을 띄우지 않는다")
        self.assertEqual(sec.questions[0].state, "closed")
        (q,) = [it for it in live.items() if it["origin"] == "question"]
        self.assertEqual(q["stop"], {"at": iso(T(690)), "tags": 1, "first": ["PI-201"]},
                         "정지 질문 카드는 매 틱 엔진의 현재 추정으로 바뀐다(마감 틱 추정)")
        import db
        with db.connect() as conn:
            titles = [it["title"] for it in db.load_draft(conn, SID)["items"]]
        self.assertEqual(titles[0], "플랜트를 정지하셨습니까?", "마감 틱 순서(질문 먼저)대로 정렬")

    def test_close_sync_keeps_ended_item_not_in_close_and_orders_like_closing_tick(self):
        def events(t):
            out = []
            if t <= T(180):
                out.append(ev("TI-101", "드리프트", 0, min(t, T(60)), 1))      # 07:00 에 끝남 → 07:30 끝남 확정 → 뒤에 사라짐
            if t >= T(720):
                out.append(ev("PI-201", "레벨시프트", 700, t, 2))               # 마감 틱에만 있음
            return out
        r = _replay(_csv(), Script(events))
        sec = r.sections[0]
        self.assertEqual(r.phase, "ended", r.error)
        ended = [tr for tr in sec.tracks.all if tr.state == "ended"]
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0].confirmed, T(90))
        self.assertTrue(ended[0].not_in_close)
        self.assertEqual(sec.false_confirm, 1)
        import db
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
            evs = [(e["tag"], e["kind"]) for e in db.load_events(conn, SID)]
        self.assertEqual([it["title"] for it in d["items"]], ["PI-201 — 레벨시프트", "TI-101 — 드리프트"],
                         "마감 틱 순서 먼저, 마감 틱이 대표하지 않는 끝남 확정 항목은 뒤에")
        self.assertTrue(d["items"][1]["live"]["not_in_close"], "내리지 않고 「마감 검출에 없음」으로 남긴다")
        self.assertEqual(sorted(evs), [("PI-201", "레벨시프트"), ("TI-101", "드리프트")])
        self.assertIsNotNone(d["items"][1]["event_id"], "확정 때 근거 이벤트를 가리킨다")

    def test_rows_out_of_time_order_fail_loud(self):
        path = _csv(minutes=30, extra=[(iso(T(10)), "TI-101", 1.0)])
        r = _replay(path, Script(lambda t: []))
        self.assertEqual(r.phase, "failed")
        self.assertIn("시각순이 아닙니다", r.error)


class ChainReplay(unittest.TestCase):
    """이어진 근무 재생 — 한 근무를 마감하고 같은 시계로 다음 근무를 곧바로 쌓는다. 마지막 근무 마감에서 멈춘다."""

    def setUp(self):
        _fresh_db()

    def test_chain_closes_each_shift_and_keeps_accumulating_on_the_same_clock(self):
        import db
        import live
        day, night = _csv(start=WS), _csv(start=T(720))

        def events(t):
            if t <= T(720):
                return [ev("TI-101", "드리프트", 0, t, 1)]            # 주간 내내 움직임 → 주간 마감 확정
            return [ev("PI-201", "레벨시프트", 725, t, 2)]            # 야간에 뜬 문제
        s = Script(events)
        r = _replay([day, night], s, prev=False)
        self.assertEqual(r.phase, "ended", r.error)
        self.assertIn("재생 데이터 끝", r.phase_note)
        d, n = r.sections
        self.assertEqual((d.mode, n.mode), ("shift_start", "12h"), "뒤 근무는 앞 근무를 재생한 원본으로 12시간 창")
        self.assertEqual(len(s.detects), 288)
        self.assertEqual(s.detects[144], (iso(T(5)), T(725)), "주간 마감 다음 틱이 같은 시계로 야간 첫 틱 [t−12h, t)")
        with db.connect() as conn:
            dd, nd = db.load_draft(conn, SID), db.load_draft(conn, NIGHT_SID)
        self.assertEqual((dd["status"], nd["status"]), ("pending", "pending"))
        self.assertEqual([it["title"] for it in dd["items"]], ["TI-101 — 드리프트"])
        self.assertEqual([it["title"] for it in nd["items"]], ["PI-201 — 레벨시프트"], "경계 뒤 처음 감지된 문제는 뒤 근무 초안")
        st = live.status()
        self.assertEqual([c["shift_id"] for c in st["closed"]], [SID, NIGHT_SID])
        self.assertEqual({c["draft"] for c in st["closed"]}, {"pending"})
        self.assertEqual((st["shift_id"], st["clock"], st["chain"]), (NIGHT_SID, iso(T(1440)), [SID, NIGHT_SID]))
        self.assertEqual([it["tag"] for it in live.items(SID)], ["TI-101"])
        self.assertEqual([it["tag"] for it in live.items()], ["PI-201"], "기본은 지금(마지막) 근무")
        self.assertEqual(live.items("2026-01-01-day"), [])
        self.assertEqual(len({it["key"] for it in live.items(SID) + live.items()}), 2, "키는 재생 안에서 유일")

    def test_chain_must_be_contiguous_and_prev_must_be_the_shift_right_before(self):
        import live
        day, next_day = Path(_csv(minutes=5, start=WS)), Path(_csv(minutes=5, start=T(1440)))
        with self.assertRaises(ValueError) as cm:
            live._plan([day, next_day])
        self.assertIn("이어지지 않습니다", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            live._plan([next_day], prev_path=day)
        self.assertIn(NIGHT_SID, str(cm.exception), "앞 근무 CSV 는 바로 앞 근무(08-25 야간)여야 한다")


class StartStopAndLocks(unittest.TestCase):
    """start 의 이름 검사·거부 · stop 이 곧바로 잠금을 놓고 늦은 AI 결과를 버리는지 · 교체 옵션."""

    def setUp(self):
        _fresh_db()

    def test_start_accepts_upload_names_only_and_refuses_confirmed_pending_busy(self):
        import db
        import jobs
        import live
        up, (name,) = _uploads(_csv(minutes=10))
        os.symlink(_csv(minutes=10), os.path.join(up, "link.csv"))           # 폴더 밖 실제 파일을 가리키는 이름
        for bad in ("../" + name, os.path.join(up, name), "a/b.csv", "..", "없는.csv", "x.txt", "link.csv", "", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                live.start([bad])
        for speed in (0, -1, 1001, "빠르게", float("nan")):
            with self.assertRaises(ValueError):
                live.start([name], speed=speed)
        with db.connect() as conn:
            _shift_row(conn, SID, WS, T(720))
            _pending_draft(conn, SID)
        with self.assertRaises(ValueError) as cm:
            live.start([name])
        self.assertIn("승인 대기 초안", str(cm.exception))
        hold = jobs.hold()
        try:
            with self.assertRaises(ValueError) as cm:
                live.start([name], replace_unconfirmed=True)      # 이름·교체 검사를 통과하고 작업 락에서 멈춘다
            self.assertIn("다른 작업", str(cm.exception))
        finally:
            hold.release()
        with db.connect() as conn:
            conn.execute("UPDATE draft SET status = 'confirmed' WHERE shift_id = ?", (SID,))
        with self.assertRaises(ValueError) as cm:
            live.start([name], replace_unconfirmed=True)
        self.assertIn("확정된 일지", str(cm.exception), "확정 초안은 교체 옵션으로도 지우지 않는다")

    def test_replace_unconfirmed_rebuilds_pending_draft(self):
        import db
        with db.connect() as conn:
            _shift_row(conn, SID, WS, T(720))
            _pending_draft(conn, SID, ("옛 항목",))
        r = _replay(_csv(), Script(lambda t: [ev("PI-201", "레벨시프트", 0, t, 1)]), replace=True)
        self.assertEqual(r.phase, "ended", r.error)
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual((d["status"], [it["title"] for it in d["items"]]), ("pending", ["PI-201 — 레벨시프트"]))

    def test_confirmed_draft_is_never_replaced_even_if_confirmed_after_start(self):
        import db
        with db.connect() as conn:
            _shift_row(conn, SID, WS, T(720))
            _pending_draft(conn, SID, ("확정 항목",))
            conn.execute("UPDATE draft SET status = 'confirmed' WHERE shift_id = ?", (SID,))
        r = _replay(_csv(), Script(lambda t: []), replace=True)
        self.assertEqual(r.phase, "failed")
        self.assertIn("확정된 일지", r.error)
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual((d["status"], [it["title"] for it in d["items"]]), ("confirmed", ["확정 항목"]))

    def test_stop_returns_at_once_releases_lock_and_drops_late_ai_result(self):
        import db
        import jobs
        import live
        import ports
        _, names = _uploads(_csv())
        _prev_raw()
        qa = []
        jobs.mark_qa_active = lambda: qa.append(1)
        s = Script(lambda t: [ev("PI-201", "레벨시프트", 0, T(60), 1)] if t >= T(30) else [])   # 07:30 끝남 확정 → AI 작업
        ports.detect, ports.compose = s.detect, s.compose
        entered, release = threading.Event(), threading.Event()

        def slow_ai(shift, item):
            entered.set()
            release.wait(10)
            return dict(item, title="정지 뒤 돌아온 AI 문장"), "시험"
        live._write_ai = slow_ai
        live._clock = _clock()
        released, real_release = [], jobs.release_hold
        jobs.release_hold = lambda lock: (released.append(threading.current_thread().name), real_release(lock))
        try:
            live.start(names)
            self.assertTrue(entered.wait(10), live.status())
            self.assertIsNone(jobs.hold(), "재생 동안은 작업 락이 잡혀 일괄 실행·리셋·적재가 기존 검사에서 막힌다")
            t0 = time.monotonic()
            st = live.stop()
            took = time.monotonic() - t0
            self.assertLess(took, 1.0, "돌고 있는 AI 호출을 기다리지 않고 돌아와야 한다")
            self.assertEqual(st["phase"], "stopped")
            self.assertEqual(released[:1], [threading.current_thread().name],
                             "작업 락은 stop() 이 돌아오기 전에 그 자리에서 놓는다 — 재생 스레드가 나중에 놓으면 리셋이 그만큼 막힌다")
            hold = jobs.hold()
            self.assertIsNotNone(hold)
            hold.release()
        finally:
            release.set()
        r = live._replay
        self.assertTrue(r.done.wait(10))
        (tr,) = r.sections[0].tracked()
        self.assertTrue(_wait(lambda: tr.error == live._STOP_NOTE and tr.ai == "failed"), tr.error)
        time.sleep(0.3)             # 늦게 돌아온 호출이 쓰기를 시도할 틈
        with db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM draft_item").fetchone()[0]
            status = conn.execute("SELECT status FROM draft WHERE shift_id = ?", (SID,)).fetchone()[0]
        self.assertEqual((n, status), (0, "live"), "정지 뒤 돌아온 AI 결과는 DB 에 쓰지 않는다")
        self.assertEqual(live.status()["interrupted"], [SID])
        self.assertIn("멈춘 구간", live.approve_lock(SID))
        self.assertGreaterEqual(len(qa), 2, "start 와 틱마다 정시 자동 비움 보호를 켠다")
        with self.assertRaises(ValueError):
            live.retry(tr.key)

    def test_stop_while_waiting_for_the_clock_writes_nothing_more(self):
        import jobs
        import live
        import ports
        _, names = _uploads(_csv())
        s = Script(lambda t: [])
        ports.detect, ports.compose = s.detect, s.compose
        live._clock = lambda: 0.0                      # 시계가 흐르지 않는다 — 첫 1분 묶음 전
        live.start(names)
        self.assertTrue(_wait(lambda: live.status()["phase"] == "running"), live.status())
        self.assertEqual(live.stop()["phase"], "stopped")
        self.assertTrue(live._replay.done.wait(5), "재생 스레드가 곧 끝나야 한다")
        hold = jobs.hold()
        self.assertIsNotNone(hold)
        hold.release()
        self.assertEqual(live.status()["rows"]["appended"], 0)
        with self.assertRaises(ValueError):
            live.stop()


class ApprovalRules(unittest.TestCase):
    """승인 잠금(live) · 승인 순서(앞 근무 대기면 뒤 근무 거부) — 명령줄과 화면 둘 다 approve.decide 에서 걸린다."""

    def setUp(self):
        _fresh_db()

    def _cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "app", "cli.py"), *args],
                              capture_output=True, text=True, env=dict(os.environ), timeout=120)

    def _post(self, pairs):
        """소켓 없이 /approve 핸들러 본체만 부른다 → 마지막 응답 (코드, 본문)."""
        import server
        from urllib.parse import urlencode

        class Fake(server.Handler):
            def __init__(self):
                self.path = "/approve"; self.sent = []; self.headers = {}
            def _send(self, code, body, ctype=""): self.sent.append((code, body))
            def send_response(self, code): self.sent.append((code, ""))
            def send_header(self, *a): pass
            def end_headers(self): pass

        h = Fake()
        h._handle_form(urlencode(pairs).encode())
        return h.sent[-1]

    def test_later_shift_is_rejected_on_cli_and_screen_until_earlier_is_confirmed(self):
        import approve
        import db
        import live
        with db.connect() as conn:
            ids = {}
            for sid, a, b in ((SID, WS, T(720)), (NIGHT_SID, T(720), T(1440)), (NEXT_SID, T(1440), T(2160))):
                _shift_row(conn, sid, a, b)
                ids[sid] = _pending_draft(conn, sid)[0]
        approve.decide(SID, {ids[SID]: {"adopted": False}})
        form = [("shift_id", NEXT_SID), ("item", ids[NEXT_SID]), (f"status_{ids[NEXT_SID]}", "완료")]
        cli = self._cli("approve", NEXT_SID, "--all", "--status", "완료")
        self.assertEqual(cli.returncode, 1, cli.stdout + cli.stderr)
        self.assertIn(f"앞 근무 {NIGHT_SID}", cli.stdout)
        code, body = self._post(form)
        self.assertEqual(code, 400, body[:300])
        self.assertIn(f"앞 근무 {NIGHT_SID}", body)
        self.assertIn(NIGHT_SID, live.approve_lock(NEXT_SID))
        with db.connect() as conn:
            self.assertIsNone(db.load_handover(conn, NEXT_SID), "거부된 승인은 아무것도 남기지 않는다")
        approve.decide(NIGHT_SID, {ids[NIGHT_SID]: {"adopted": False}})
        self.assertIsNone(live.approve_lock(NEXT_SID))
        code, body = self._post(form)
        self.assertEqual(code, 303, body[:300])
        cli = self._cli("approve", NEXT_SID, "--all", "--status", "완료")    # 재승인도 순서 규칙을 통과한다
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)

    def test_previous_live_draft_points_at_the_replay_not_at_approval(self):
        import approve
        import db
        import live
        with db.connect() as conn:
            _shift_row(conn, SID, WS, T(720))
            _shift_row(conn, NIGHT_SID, T(720), T(1440))
            db.open_live_draft(conn, SID, "test")        # 중단된 재생이 남긴 초안
            nid = _pending_draft(conn, NIGHT_SID)[0]
        with self.assertRaises(ValueError) as cm:
            approve.decide(NIGHT_SID, {nid: {"adopted": False}})
        self.assertIn(SID, str(cm.exception))
        self.assertIn("실시간 초안", str(cm.exception), "「먼저 확정하세요」만 말하면 그 근무로 가도 또 거부돼 맴돈다")
        self.assertIn("일괄 실행", str(cm.exception), "무엇을 해야 풀리는지 짚어야 한다")
        self.assertIn("실시간 초안", live.approve_lock(NIGHT_SID))

    def test_ai_failure_is_visible_locks_approval_and_retry_recovers(self):
        import live
        import llm
        calls = []
        real = live._write_ai

        def flaky(shift, item):
            calls.append(item["title"])
            if len(calls) == 1:
                raise llm.LLMUnavailable("시험용 실패")
            return real(shift, item)

        live._write_ai = flaky
        r = _replay(_csv(), Script(lambda t: [ev("PI-201", "레벨시프트", 700, t, 1)] if t >= T(720) else []))
        self.assertEqual(r.phase, "ended", r.error)
        st = live.status()
        self.assertEqual(st["counts"]["ai_failed"], 1)
        self.assertIn("AI 서술 실패 1건", st["approve_lock"])
        (view,) = live.items()
        self.assertEqual((view["state"], view["draft_item_id"]), ("ai_failed", None))
        self.assertIn("시험용 실패", view["error"])
        import approve
        import db
        with db.connect() as conn:
            self.assertEqual(db.load_draft(conn, SID)["status"], "live", "실패가 남으면 승인 대기로 넘기지 않는다")
        with self.assertRaises(ValueError) as cm:
            approve.decide(SID, {})
        self.assertIn("실시간", str(cm.exception))
        code, body = self._post([("shift_id", SID)])
        self.assertEqual(code, 400, "화면도 같은 이유로 거부한다")
        cli = self._cli("approve", SID, "--all", "--status", "완료")
        self.assertEqual(cli.returncode, 1, cli.stdout + cli.stderr)
        self.assertIn("실시간", cli.stdout, "명령줄도 같은 이유로 거부한다")
        live.retry(view["key"])
        self.assertTrue(_wait(lambda: live.items()[0]["state"] == "ready"), live.items())
        self.assertTrue(_wait(lambda: live.status()["approve_lock"] is None), live.status())
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual((d["status"], len(d["items"])), ("pending", 1))
        with self.assertRaises(ValueError):
            live.retry(view["key"])


class LiveDraftEverywhere(unittest.TestCase):
    """draft.status 'live' 를 읽는 곳 — 일괄 실행은 깨끗이 대체하고, 이월·제외 기록과 순서 규칙은 확정 전 초안으로 본다."""

    def setUp(self):
        _fresh_db()

    def _live_item(self, conn, did, title="재생 중 항목", adopted=None, status=None):
        import db
        iid = db.add_live_item(conn, did, {"origin": "detected", "tag": "TI-101", "title": title, "body": "본문", "severity": "중",
                                           "live": {"key": "p1"}}, [ev("TI-101", "드리프트", 0, 30, 1)], "engine")
        conn.execute("UPDATE draft_item SET adopted = ?, status = ? WHERE id = ?", (adopted, status, iid))
        return iid

    def test_batch_run_cleanly_replaces_a_live_draft_with_and_without_redo(self):
        import collect
        import db
        import pipeline
        collect.ingest(collect.source_for(_csv(tags=("TI-101", "TI-403"))))
        for redo in (False, True):
            with db.connect() as conn:
                did = db.open_live_draft(conn, SID, "test", replace_unconfirmed=True)    # 멈춘 재생이 남긴 초안
                self._live_item(conn, did)
            result = pipeline.run(SID, verbose=False, redo=redo)
            with db.connect() as conn:
                d = db.load_draft(conn, SID)
                n_ev = conn.execute("SELECT COUNT(*) FROM event WHERE shift_id = ?", (SID,)).fetchone()[0]
            self.assertEqual(d["status"], "pending", f"redo={redo}")
            self.assertEqual([it for it in d["items"] if it["live"] or it["title"] == "재생 중 항목"], [],
                             "재생이 남긴 항목이 섞이면 안 된다")
            self.assertEqual(n_ev, result["events"], "재생이 넣은 근거 이벤트가 남으면 안 된다")
        with db.connect() as conn:
            did = db.open_live_draft(conn, SID, "test", replace_unconfirmed=True)
            self._live_item(conn, did)
        cli = subprocess.run([sys.executable, os.path.join(ROOT, "app", "cli.py"), "run", SID],
                             capture_output=True, text=True, env=dict(os.environ), timeout=300)
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual((d["status"], [it for it in d["items"] if it["live"]]), ("pending", []),
                         "명령줄 일괄 실행(cli run)도 'live' 초안을 깨끗이 대체한다")

    def test_live_drafts_are_not_carry_or_exclusion_records_but_block_later_approval(self):
        import db
        with db.connect() as conn:
            _shift_row(conn, SID, WS, T(720))
            _shift_row(conn, NIGHT_SID, T(720), T(1440))
            did = db.open_live_draft(conn, SID, "test")
            self._live_item(conn, did, "진행중 흉내", adopted=1, status="진행중")
            self._live_item(conn, did, "제외 흉내", adopted=0)
            self.assertEqual(db.open_items(conn, NIGHT_SID), [], "확정되지 않은 초안은 이월 재료가 아니다")
            self.assertEqual(db.recent_exclusions(conn, NIGHT_SID), {}, "확정되지 않은 초안의 제외는 자리 옮김 재료가 아니다")
            self.assertEqual({r["id"]: r["draft_status"] for r in db.list_shifts(conn)}[SID], "live")
            self.assertEqual(db.unconfirmed_before(conn, NIGHT_SID), (SID, "live"))
            self.assertEqual(db.live_drafts(conn), [SID])


    def test_reset_leaves_no_interrupted_live_draft(self):
        import db
        import live
        with db.connect() as conn:
            _shift_row(conn, SID, WS, T(720))
            db.open_live_draft(conn, SID, "test")
        live._replay = None
        self.assertEqual(live.status()["interrupted"], [SID])
        db.reset(empty=True)
        self.assertEqual(live.status()["interrupted"], [], "빈 상태 리셋 뒤에는 멈춘 재생의 초안이 남지 않는다")


class FoldingAndEvents(unittest.TestCase):
    """재발은 원 항목에 접고(새 항목·새 AI 없음), 마감 뒤 이벤트 표는 마감 집합에 맞춘다."""

    def setUp(self):
        _fresh_db()

    def _count_ai(self):
        import live
        calls, real = [], live._write_ai
        live._write_ai = lambda shift, item: (calls.append(item.get("title")), real(shift, item))[1]
        return calls

    def test_recurrence_folds_into_the_original_item_without_new_ai(self):
        import db
        import live
        calls = self._count_ai()

        def events(t):
            if t < T(120):
                return [ev("TI-101", "드리프트", 0, 30, 1)]                     # 06:30 에 끝난 문제가 결과에 남아 있다
            if t <= T(180):
                return [ev("TI-101", "드리프트", 0, min(t, T(150)), 1)]          # 08:00 에 다시 움직인다 → 재발
            return []
        r = _replay(_csv(), Script(events))
        sec = r.sections[0]
        self.assertEqual(r.phase, "ended", r.error)
        self.assertEqual((sec.tracks.stats["recurrence"], sec.tracks.stats["folded"]), (1, 1))
        self.assertEqual(len(calls), 1, "재발에는 AI 를 다시 부르지 않는다")
        self.assertEqual(len(live.items()), 1, "접힌 재발은 카드로 남지 않는다")
        st = live.status()          # 접힌 카드가 세기·상태에 섞여 status() 가 터졌었다(6근무 재측정에서 KeyError)
        self.assertEqual((st["counts"]["ready"], st["counts"]["observing"], st["counts"]["vanished"]), (1, 0, 0))
        self.assertEqual(st["stats"]["folded"], 1)
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual(len(d["items"]), 1, "재발이 새 초안 항목을 만들면 안 된다")
        it = d["items"][0]
        self.assertIn("재발 1회 — 08:00", it["evidence"], "감지 근거에 재발 시각을 남긴다")
        self.assertEqual(it["live"]["recurrences"], [iso(T(120))])
        self.assertEqual(it["live"]["end"], iso(T(150)), "끝 시각을 마지막 재발까지 늘린다")
        self.assertEqual(it["title"], "TI-101 — 드리프트", "문장은 확정 때 것 그대로")

    def test_recurrence_alive_at_close_keeps_one_item_and_the_closing_event(self):
        import db
        import live

        def events(t):
            return [ev("TI-101", "드리프트", 0, 30 if t < T(600) else t, 1)]     # 16:00 부터 다시 움직여 마감까지
        r = _replay(_csv(), Script(events))
        sec = r.sections[0]
        self.assertEqual(r.phase, "ended", r.error)
        self.assertEqual(sec.tracks.stats["folded"], 1)
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
            evs = db.load_events(conn, SID)
        self.assertEqual(len(d["items"]), 1)
        it = d["items"][0]
        self.assertFalse(it["live"]["not_in_close"], "접힌 재발이 마감에 있으면 원 항목은 「마감 검출에 없음」이 아니다")
        self.assertIn("재발 1회 — 16:00", it["evidence"])
        self.assertEqual((d["status"], len(live.items())), ("pending", 1))
        self.assertEqual(live.status()["counts"]["ready"], 1, "접힌 카드가 세기에 섞이면 안 된다")
        self.assertEqual(len(evs), 1, "이벤트 표는 마감 집합과 같아야 한다(확정 때 근거 행은 치운다)")
        self.assertEqual((evs[0]["end_ts"], it["event_id"]), (iso(T(720)), evs[0]["id"]),
                         "확정 항목의 대표 이벤트가 마감 행을 가리킨다")

    def _same_problem_script(self):
        """같은 tag·kind 가 더 크게 다시 나는 대본 — 08:00 재발, 08:30 에 끝나 09:00 에 확정."""
        def events(t):
            if t < T(120):
                return [ev("TI-101", "드리프트", 0, 30, 1, score=1.0, sev="중")]
            if t <= T(180):
                return [ev("TI-101", "드리프트", 0, min(t, T(150)), 1, score=9.0, sev="상")]
            return []
        return Script(events)

    def test_recurrence_sync_writes_the_newest_snapshot_last(self):
        """겹친 두 _sync_recurrences 의 쓰기 순서가 스냅샷 순서와 뒤집히지 않는다(codex 반증).

        A 가 쓰기 잠금을 잡기 직전에 멈춘 사이 재발이 하나 더 접히고 B 가 먼저 쓰는 순서를 강제한다.
        스냅샷을 잠금 밖에서 뜨던 코드는 A 의 「재발 1회」가 마지막에 써져 「재발 2회」를 덮었다."""
        from contextlib import contextmanager
        import db
        import live
        r = _replay(_csv(), self._same_problem_script())
        sec = r.sections[0]
        root = next(tr for tr in sec.tracks.all if tr.recurrences)
        self.assertEqual((r.phase, len(root.recurrences), bool(root.draft_item_id)), ("ended", 1, True), r.error)

        real_writing, a_waiting, a_go = r.writing, threading.Event(), threading.Event()
        self.addCleanup(a_go.set)                   # 단언이 먼저 깨져도 A 가 매달려 남지 않게

        @contextmanager
        def writing():
            if threading.current_thread().name == "A":
                a_waiting.set()
                a_go.wait()                         # 시간 제한을 두면 A 가 스스로 풀려 순서를 강제하지 못한다(codex)
            with real_writing():
                yield
        r.writing = writing
        with live._lock:
            root.saved_recur = 0                    # 두 호출 모두 위쪽 빠른 가드를 지나게 한다
        a = threading.Thread(target=r._sync_recurrences, args=(sec, root), name="A", daemon=True)
        a.start()
        self.assertTrue(a_waiting.wait(5), "A 가 쓰기 잠금 앞에 서야 순서를 강제할 수 있다")
        with live._lock:
            root.recurrences.append(root.recurrences[0])    # 그 사이 재발이 하나 더 접힌다
        b = threading.Thread(target=r._sync_recurrences, args=(sec, root), name="B", daemon=True)
        b.start()
        b.join(5)
        self.assertFalse(b.is_alive(), "B 가 다 쓴 뒤에만 A 를 푼다 — 남아 있으면 교착이다")
        a_go.set()
        a.join(5)
        self.assertFalse(a.is_alive(), "A 도 끝나야 한다 — 남아 있으면 교착이다")
        with db.connect() as conn:
            evidence = conn.execute("SELECT evidence FROM draft_item WHERE id = ?", (root.draft_item_id,)).fetchone()[0]
        self.assertIn("재발 2회", evidence, "마지막에 써진 근거 줄이 최신 재발 수여야 한다")
        self.assertEqual(root.saved_recur, 2)

    def test_same_problem_recurrence_folds_keeps_ai_severity_and_merges_score(self):
        import db
        import live
        calls = self._count_ai()
        r = _replay(_csv(), self._same_problem_script())
        sec = r.sections[0]
        self.assertEqual((r.phase, sec.tracks.stats["folded"]), ("ended", 1), r.error)
        self.assertEqual(len(calls), 1, "접힌 재발에는 AI 를 다시 부르지 않는다")
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual(len(d["items"]), 1)
        it = d["items"][0]
        self.assertEqual(it["severity"], "중", "항목 중요도는 AI 판정 그대로 — 더 큰 재발이 접혀도 덮지 않는다")
        self.assertIn("재발 1회 — 08:00", it["evidence"], "마감 뒤에도 근거 줄이 최종본이어야 한다")
        self.assertIn("재발 중 가장 큰 것은 중요도 상", it["evidence"],
                      "덮지 않는 대신 더 큰 재발이 있었다는 사실은 근거 줄에 남는다")
        self.assertEqual(it["live"]["end"], iso(T(150)))
        self.assertEqual(live.items()[0]["score_max"], 9.0)
        self.assertEqual(live.items()[0]["recur_severity"], "상", "카드에는 재발 최댓값이 따로 뜬다")
        self.assertEqual((sec.false_confirm, sec.false_confirm_tracks), (1, 2),
                         "화면 숫자는 카드 기준(접힌 재발 뺌), 틱 실험 대조값은 추적기 기준")

    def test_recurrence_with_new_tags_becomes_its_own_item(self):
        import db
        import live
        calls = self._count_ai()

        def events(t):
            if t < T(120):
                return [ev("TI-101", "드리프트", 0, 30, 1)]
            if t <= T(180):     # 같은 묶음에 새 태그 둘이 붙어 더 크고 다른 문제로 돈다
                end = min(t, T(150))
                return [ev("PI-201", "레벨시프트", 100, end, 1, score=9.0, sev="상"),
                        ev("FI-301", "이탈", 100, end, 1, score=5.0, sev="상"),
                        ev("TI-101", "드리프트", 0, 30, 1)]
            return []
        r = _replay(_csv(), Script(events))
        sec = r.sections[0]
        self.assertEqual((r.phase, sec.tracks.stats["folded"]), ("ended", 0), "새 tag·kind 를 데려온 재발은 접지 않는다")
        self.assertEqual(len(calls), 2, "별개 문제라 제 AI 호출을 받는다")
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual(sorted(it["title"] for it in d["items"]), ["PI-201 — 레벨시프트", "TI-101 — 드리프트"],
                         "새 태그를 데려온 문제가 옛 항목에 삼켜지면 안 된다")
        big = next(it for it in d["items"] if it["title"].startswith("PI-201"))
        self.assertEqual((big["severity"], sorted(m[0] for m in big["live"]["members"])),
                         ("상", ["FI-301", "PI-201", "TI-101"]))
        self.assertEqual(len(live.items()), 2)

    def test_fold_into_a_failed_item_appears_after_retry_and_blocks_forget(self):
        import db
        import live
        import llm
        real, seen = live._write_ai, []

        def flaky(shift, item):
            seen.append(item.get("title"))
            if len(seen) == 1:
                raise llm.LLMUnavailable("시험용 실패")
            return real(shift, item)
        live._write_ai = flaky
        r = _replay(_csv(), self._same_problem_script())
        sec = r.sections[0]
        root = next(tr for tr in sec.tracks.all if not tr.folded)
        folded = next(tr for tr in sec.tracks.all if tr.folded)
        self.assertEqual((root.ai, live.status()["counts"]["ai_failed"]), ("failed", 1))
        with self.assertRaises(ValueError) as cm:
            live.forget()
        self.assertIn("AI 서술 실패", str(cm.exception), "실패가 남은 재생을 지우면 retry 할 곳이 사라진다")
        with self.assertRaises(ValueError):
            live.retry(folded.key)          # 접힌 카드는 다시 쓸 대상이 아니다
        live.retry(root.key)
        self.assertTrue(_wait(lambda: live.items()[0]["state"] == "ready"), live.items())
        self.assertTrue(_wait(lambda: live.status()["approve_lock"] is None), live.status())
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual((d["status"], len(d["items"])), ("pending", 1))
        self.assertIn("재발 1회 — 08:00", d["items"][0]["evidence"], "실패 뒤 다시 쓴 항목에도 접힌 재발이 들어간다")
        self.assertEqual(d["items"][0]["severity"], "중", "다시 쓴 뒤에도 항목 값은 AI 판정 그대로다")
        self.assertIn("재발 중 가장 큰 것은 중요도 상", d["items"][0]["evidence"])
        live.forget()

    def test_recurrence_that_vanishes_before_confirming_still_folds(self):
        import db
        import live

        def events(t):
            if t < T(710):
                return [ev("TI-101", "드리프트", 0, 30, 1)]      # 06:30 에 끝난 문제(07:00 확정)가 결과에 남아 있다
            if t < T(715):
                return [ev("TI-101", "드리프트", 0, 712, 1)]     # 17:50 에 다시 움직였다가
            return []                                            # 확정 전에 사라지고 마감 틱에도 없다
        r = _replay(_csv(), Script(events))
        sec = r.sections[0]
        self.assertEqual((r.phase, sec.tracks.stats["folded"]), ("ended", 1),
                         "확정 전에 사라진 재발도 실제로 일어난 움직임이라 원 항목에 접는다")
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual(len(d["items"]), 1)
        it = d["items"][0]
        self.assertIn("재발 1회 — 17:50", it["evidence"], "「06:30 에 끝남」인 채로 승인되면 안 된다")
        self.assertEqual(it["live"]["end"], iso(T(712)), "끝 시각이 마지막 재발까지 늘어난다")
        self.assertEqual(len(live.items()), 1)

    def test_forget_clears_the_last_replay_but_refuses_while_running(self):
        import live
        r = _replay(_csv(), Script(lambda t: []))
        self.assertEqual(live.status()["phase"], "ended")
        r.phase = "running"
        with self.assertRaises(ValueError):
            live.forget()
        r.phase = "ended"
        self.assertEqual(live.forget()["phase"], "idle")
        self.assertEqual((live.items(), live.values()["clock"]), ([], None))


class WindowAndRawJudgement(unittest.TestCase):
    """창 판정·원본 온전 판정 — 양끝 표본 두 개가 아니라 시간 칸이 얼마나 찼는지로 본다(반증 A 1·6)."""

    def setUp(self):
        _fresh_db()

    def _prev_rows(self, ranges):
        """앞 근무 원본을 분 구간 목록으로 넣는다 — [(시작분, 끝분)], 앞 근무 시작 기준."""
        import db
        base = WS - dt.timedelta(hours=12)
        with db.connect() as conn:
            _shift_row(conn, PREV_SID, base, WS)
            conn.executemany("INSERT INTO raw_sample (tag, ts, value) VALUES (?,?,?)",
                             [(tag, iso(base + dt.timedelta(minutes=i)), 5.0)
                              for a, b in ranges for i in range(a, b) for tag in TAGS])

    def _fill(self):
        import db
        with db.connect() as conn:
            return db.raw_window_complete(conn, iso(WS - dt.timedelta(hours=12)), iso(WS))

    def test_edges_only_is_not_complete_but_two_minutes_short_is(self):
        self._prev_rows([(0, 2), (718, 720)])           # 양끝 2분씩만 있고 가운데는 비었다
        ok, fill = self._fill()
        self.assertFalse(ok, "양끝 표본만 보면 가운데 구멍을 못 본다")
        self.assertEqual((fill["filled"], fill["buckets"], fill["samples"]), (2, 12, 8))
        _fresh_db()
        self._prev_rows([(2, 719)])                      # 앞뒤 2분만 모자란 99.7%
        ok, fill = self._fill()
        self.assertTrue(ok, "앞뒤 2분 모자란 것을 통째로 버리면 안 된다")
        self.assertEqual((fill["filled"], fill["head_gap_min"], fill["tail_gap_min"]), (12, 2.0, 2.0))

    def test_replay_note_says_how_much_of_the_previous_shift_is_there(self):
        import live
        self._prev_rows([(0, 2), (718, 720)])
        r = _replay(_csv(), Script(lambda t: []), prev=False)
        sec = r.sections[0]
        self.assertEqual(sec.mode, "shift_start")
        self.assertIn("12칸 중 2칸만 참", sec.note, "몇 점·몇 칸인지 드러나야 오판이 보인다")
        self.assertEqual(live.status()["prev"]["filled"], 2)

    def test_half_filled_shift_is_not_complete_and_partial_raw_is_cleared(self):
        import db
        import live

        def boom(t):
            if t >= T(100):
                raise RuntimeError("검출 폭발")
            return []
        r = _replay(_csv(), Script(boom), prev=False)
        self.assertEqual(r.phase, "failed")
        with db.connect() as conn:
            left = conn.execute("SELECT COUNT(*) FROM raw_sample WHERE ts >= ?", (iso(WS),)).fetchone()[0]
            self.assertEqual(left, 0, "쌓다 만 원본은 비운다 — 남으면 일괄 실행이 반쪽 데이터로 초안을 만든다")
            self.assertFalse(db.raw_complete(conn, SID),
                             "비웠으니 표본이 0 이고, 배치는 파일에서 다시 적재한다 (반증 A6 은 여기서 닫힌다)")
            conn.executemany("INSERT INTO raw_sample (tag, ts, value) VALUES (?,?,?)",
                             [(tag, iso(T(i)), 1.0) for i in range(100) for tag in TAGS])
            ok, fill = db.raw_window_complete(conn, iso(WS), iso(WS + dt.timedelta(hours=12)))
            self.assertFalse(ok, "12시간 중 100분치는 실시간 창으로 쓸 수 없다")
            self.assertEqual((fill["filled"], fill["buckets"]), (2, 12))
        self.assertIn("멈춘 구간", live.approve_lock(SID))

    def test_a_long_outage_keeps_the_raw_complete_for_the_batch(self):
        """전 태그가 91분 멎어도 원본은 멀쩡하다 — 배치가 「원본 없음」이라 거부하면 안 된다(3라운드 ①).

        두 물음은 다르다: raw_complete 는 「회전이 창을 잘랐나」, raw_window_complete 는 「창이 실제로 찼나」.
        한 함수로 합쳤더니 데이터가 다 있는 근무에 /pipeline/run 이 사실과 다른 400 을 냈다."""
        import db
        with db.connect() as conn:
            _shift_row(conn, SID, WS, WS + dt.timedelta(hours=12))
            conn.executemany("INSERT INTO raw_sample (tag, ts, value) VALUES (?,?,?)",
                             [(tag, iso(T(i)), 1.0) for i in range(720) if not 59 <= i < 150 for tag in TAGS])
            self.assertTrue(db.raw_complete(conn, SID),
                            "앞을 자른 것이 아니므로 배치는 그대로 돌아야 한다 — 구멍은 「계측 결측 구간」으로 초안에 적힌다")
            self.assertTrue(db.find_gaps(conn, SID), "같은 구멍을 find_gaps 는 결측 구간으로 정상 취급한다")
            ok, fill = db.raw_window_complete(conn, iso(WS), iso(WS + dt.timedelta(hours=12)))
            self.assertFalse(ok, "같은 원본이라도 실시간 창 판정은 한 칸이 통째로 빈 것을 덜 찼다고 본다")
            self.assertEqual((fill["filled"], fill["buckets"]), (11, 12))

    def test_tick_interval_must_divide_the_shift(self):
        import live
        path = Path(_csv(minutes=5))
        orig = live._TICK
        live._TICK = dt.timedelta(minutes=7)
        try:
            with self.assertRaises(ValueError) as cm:
                live._plan([path])
            self.assertIn("마감 틱", str(cm.exception), "마감 틱이 안 도는 간격은 시작 전에 막는다")
        finally:
            live._TICK = orig

    def test_rows_after_the_window_are_visible_on_screen(self):
        import live
        r = _replay(_csv(minutes=720, extra=[(iso(T(725)), "TI-101", 1.0)]), Script(lambda t: []))
        self.assertTrue(r.sections[0].rows_after)
        self.assertTrue(live.status()["rows"]["after_window"], "버린 행이 있으면 화면에서도 알아야 한다")

    def test_event_without_end_time_is_kept_not_dropped(self):
        import db

        def events(t):
            e = ev("TI-101", "드리프트", 0, 30, 1)
            e["end_ts"] = None              # 규약상 허용 — 일괄 실행은 그대로 저장한다
            return [e]
        r = _replay(_csv(), Script(events))
        self.assertEqual(r.phase, "ended", r.error)
        with db.connect() as conn:
            d = db.load_draft(conn, SID)
        self.assertEqual(len(d["items"]), 1, "끝 시각이 없다고 실시간에서만 사라지면 안 된다")
        self.assertEqual(d["items"][0]["live"]["end"], iso(T(720)), "끝 시각이 없으면 지금도 이어지는 것으로 본다")

    def test_a_live_draft_keeps_the_batch_from_trusting_the_raw(self):
        """재생 중 서버가 강제 종료된 자리 — _drop_partial 이 못 돌아 반쪽 원본과 'live' 초안이 함께 남는다.

        첫 표본이 창 시작이라 회전 규칙만으로는 온전으로 보여, 화면이 권하는 복구 경로(일괄 실행)가 반쪽 데이터로
        초안을 만들었다. 'live' 초안이 있는 동안은 믿지 않고, 마감 동기화가 끝나 pending 이 되면 원래 규칙으로 돌아온다."""
        import db
        with db.connect() as conn:
            _shift_row(conn, SID, WS, WS + dt.timedelta(hours=12))
            conn.executemany("INSERT INTO raw_sample (tag, ts, value) VALUES (?,?,?)",
                             [(tag, iso(T(i)), 1.0) for i in range(100) for tag in TAGS])     # 앞 100분 — 재생이 죽은 자리
            did = conn.execute("INSERT INTO draft (shift_id, status, generator, generated_at) VALUES (?,'live','live',?)",
                               (SID, db.now())).lastrowid
            self.assertFalse(db.raw_complete(conn, SID), "'live' 초안이 남은 근무의 원본은 일괄 실행이 믿지 않는다")
            conn.execute("UPDATE draft SET status = 'pending' WHERE id = ?", (did,))
            self.assertTrue(db.raw_complete(conn, SID), "pending 이 되면 원래 규칙(회전이 앞을 잘랐나)으로 판정한다")

    def test_closing_with_no_detection_still_cleans_the_event_table(self):
        """마감 검출이 0건인 조용한 근무도 정리가 돌아야 한다(3라운드 ③).

        keep_ids 가 비었을 때 `id NOT IN (SELECT NULL)` 로 흉내내면 SQL 에서 참이 아니라 한 행도 안 지워진다 —
        그 근무만 조용히 「채점이 일괄과 같아진다」가 깨졌다."""
        import db
        with db.connect() as conn:
            _shift_row(conn, SID, WS, WS + dt.timedelta(hours=12))
            db.add_events(conn, SID, [ev("TI-101", "드리프트", 0, 30, i) for i in range(1, 4)], "test")
            ids = [r[0] for r in conn.execute("SELECT id FROM event WHERE shift_id = ? ORDER BY id", (SID,))]
            item_id = _pending_draft(conn, SID)[0]
            conn.execute("UPDATE draft_item SET event_id = ? WHERE id = ?", (ids[0], item_id))
            dropped = db.drop_unreferenced_events(conn, SID, [])
            left = [r[0] for r in conn.execute("SELECT id FROM event WHERE shift_id = ? ORDER BY id", (SID,))]
        self.assertEqual((dropped, left), (2, [ids[0]]), "초안이 가리키는 한 건만 남고 나머지는 지워진다")


class ClockAndContract(unittest.TestCase):
    def setUp(self):
        _fresh_db()

    def test_clock_is_start_plus_elapsed_times_speed_and_rebases(self):
        import live
        now = [100.0]
        r = live._Replay(live._plan([Path(_csv(minutes=1))]), 100, clock=lambda: now[0])
        r.base_data, r.base_mono, r.phase = WS, 100.0, "running"
        live._replay = r
        now[0] = 103.0
        self.assertEqual(r._now(), T(5), "3초 × 100배 = 5분")
        live.set_speed(10)
        now[0] = 106.0
        self.assertEqual(r._now(), T(5) + dt.timedelta(seconds=30), "배속을 바꿔도 근무 시각이 끊기지 않는다")
        now[0] = 1e9
        self.assertEqual(r._now(), r.sections[-1].we, "마지막 근무 끝을 넘지 않는다")

    def test_status_items_values_follow_contract(self):
        import live
        _replay(_csv(), Script(lambda t: [ev("PI-201", "레벨시프트", 0, t, 1)]))
        st = live.status()
        for key in ("phase", "phase_note", "chain", "files", "shift_id", "window", "file", "clock", "speed", "window_mode",
                    "window_note", "stop_question_from", "prev", "tick", "counts", "stats", "rows", "approve_lock", "closed",
                    "interrupted", "error"):
            self.assertIn(key, st)
        self.assertEqual((st["phase"], st["clock"], st["rows"]["appended"]), ("ended", iso(T(720)), 1440))
        self.assertEqual(st["prev"]["complete"], True)
        self.assertEqual(st["interrupted"], [])
        (it,) = live.items()
        for key in ("key", "state", "origin", "confirm", "ongoing", "not_in_close", "recurrence_of", "tag", "kind", "title",
                    "severity", "evidence", "score", "score_max", "start", "end", "members", "related_tags", "stop", "first_seen",
                    "confirmed", "ai_sec", "draft_item_id", "error"):
            self.assertIn(key, it)
        self.assertEqual((it["state"], it["confirm"], it["first_seen"]), ("ready", "closed", iso(T(5))))
        vals = live.values()
        self.assertEqual(vals["clock"], iso(T(720)))
        self.assertEqual(sorted(vals["values"]), sorted(TAGS))
        self.assertEqual(vals["values"]["TI-101"][0], iso(T(719)), "근무 시각 전까지 흘러 들어온 마지막 값")
        idle = (_fresh_db(), __import__("live").status())[1]
        self.assertEqual((idle["phase"], idle["closed"], idle["interrupted"]), ("idle", [], []))

    def test_interrupted_live_draft_is_reported_and_stays_locked(self):
        import db
        import live
        with db.connect() as conn:
            _shift_row(conn, SID, WS, T(720))
            db.open_live_draft(conn, SID, "test")
        live._replay = None                       # 서버를 다시 켠 것과 같다 — 메모리 상태 없음
        self.assertEqual(live.status()["interrupted"], [SID])
        self.assertIn("중단된 구간", live.approve_lock(SID))

    def test_collect_append_adds_without_replacing_but_ingest_replaces(self):
        import collect
        import db
        path = _csv(minutes=3, tags=("TI-101",))
        collect.ingest(collect.source_for(path))
        with db.connect() as conn:
            collect.append(conn, [(T(3), "TI-101", 1.0), (T(4), "TI-101", 2.0)])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_sample").fetchone()[0], 5)
        collect.ingest(collect.source_for(path))
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM raw_sample").fetchone()[0], 3, "ingest 는 근무 원본을 교체한다")


if __name__ == "__main__":
    unittest.main()
