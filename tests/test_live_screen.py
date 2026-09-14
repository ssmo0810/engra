"""실시간 화면 시험 — 관리 화면의 재생 시작·정지 · /api/live.

    python3 tests/test_live_screen.py

재생 본체(app/live.py)의 규칙은 tests/test_live.py 가 본다. 여기서는 화면이 그 공개 함수를 부르고, 거부 문장을
그대로 보여 주고, 관리 열쇠를 요구하는지만 본다. 도우미(임시 DB · CSV · 업로드 폴더 · 대본 엔진)는 test_live 것을 그대로 쓴다.
AI 는 부르지 않는다(ENGRA_LLM=off).
"""
import importlib.util
import json
import os
import pathlib
import sys
import unittest
from urllib.parse import urlencode

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("test_live_helpers", os.path.join(ROOT, "tests", "test_live.py"))
H = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(H)


def _handler(path, cookie=""):
    """소켓 없이 핸들러 본체만 부른다 — 응답은 sent 에 (코드, 본문) 으로 쌓인다."""
    import server

    class Fake(server.Handler):
        def __init__(self):
            self.path = path
            self.sent = []
            self.headers = {"Cookie": cookie} if cookie else {}

        def _send(self, code, body, ctype=""):
            self.sent.append((code, body))

        def send_response(self, code):
            self.sent.append((code, ""))

        def send_header(self, *a):
            pass

        def end_headers(self):
            pass

    return Fake()


def _post(path, pairs, cookie=""):
    h = _handler(path, cookie)
    h._handle_form(urlencode(pairs).encode())
    return h.sent[-1]


def _get(path):
    h = _handler(path)
    h.do_GET()
    return h.sent[-1]


class ReplayControls(unittest.TestCase):
    """관리 화면에서 재생을 시작하고 멈춘다 — 실제로 도는 재생을 화면 버튼이 켜고 끈다."""

    def setUp(self):
        H._fresh_db()
        os.environ.pop("ENGRA_ADMIN_KEY", None)

    def tearDown(self):
        os.environ.pop("ENGRA_ADMIN_KEY", None)
        live = sys.modules.get("live")
        if live is not None and live.status()["phase"] in ("preparing", "running"):
            live.stop()

    def test_admin_page_offers_start_and_stop_with_uploaded_csvs(self):
        _, (name,) = H._uploads(H._csv(minutes=10))
        import server
        body = server.view_admin()
        self.assertIn('action="/live/start"', body)
        self.assertIn('action="/live/stop"', body)
        self.assertIn(f'<option value="{name}"', body, "올린 근무 CSV 를 골라 재생할 수 있어야 한다")

    def test_start_and_stop_from_the_admin_forms(self):
        _, (name,) = H._uploads(H._csv())
        import jobs
        import live
        import ports
        s = H.Script(lambda t: [])
        ports.detect, ports.compose = s.detect, s.compose
        live._clock = lambda: 0.0                  # 시계를 멈춰 둔다 — 첫 1분 묶음 앞에서 기다린다
        code, _ = _post("/live/start", [("csv", name)])
        self.assertEqual(code, 303)
        self.assertTrue(H._wait(lambda: live.status()["phase"] == "running"), live.status())
        self.assertEqual(json.loads(_get("/api/live")[1])["status"]["phase"], "running")
        code, _ = _post("/live/stop", [])
        self.assertEqual(code, 303)
        self.assertEqual(live.status()["phase"], "stopped")
        hold = jobs.hold()
        self.assertIsNotNone(hold, "정지하면 작업 락을 놓는다 — 일괄 실행·리셋이 다시 된다")
        hold.release()

    def test_refusals_are_shown_as_is(self):
        H._uploads(H._csv(minutes=10))
        code, body = _post("/live/start", [("csv", "../밖.csv")])
        self.assertEqual(code, 400)
        self.assertIn("업로드 폴더의 파일 이름만 받습니다", body)
        code, body = _post("/live/stop", [])
        self.assertEqual(code, 400)
        self.assertIn("재생 중이 아닙니다", body)

    def test_replay_controls_need_the_admin_key(self):
        _, (name,) = H._uploads(H._csv(minutes=10))
        os.environ["ENGRA_ADMIN_KEY"] = "열쇠"
        for path, pairs in (("/live/start", [("csv", name)]), ("/live/stop", [])):
            code, _ = _post(path, pairs)
            self.assertEqual(code, 403, path)
        import live
        self.assertEqual(live.status()["phase"], "idle", "열쇠 없이 재생이 시작되면 안 된다")

    def test_api_live_is_status_json(self):
        code, body = _get("/api/live")
        self.assertEqual(code, 200)
        st = json.loads(body)["status"]
        self.assertEqual((st["phase"], st["chain"]), ("idle", []))


def _card(body, key):
    """그 표식이 붙은 항목 카드 한 장만 잘라 낸다 — <div> 짝을 세어 카드 끝에서 멈춘다.
    다음 카드까지로 자르면 마지막 카드일 때 화면 바닥(승인 바·스크립트)까지 딸려 와 검사가 헛돈다."""
    i = body.index(f'data-key="{key}"')
    start = body.rindex('<div class="item', 0, i)
    depth, j = 0, start
    while True:
        opened, closed = body.find("<div", j), body.find("</div>", j)
        if closed < 0:
            return body[start:]
        if 0 <= opened < closed:
            depth, j = depth + 1, opened + 4
        else:
            depth, j = depth - 1, closed + 6
            if depth == 0:
                return body[start:j]


class LiveDraftScreen(unittest.TestCase):
    """쌓이는 중인 근무 화면 — 관찰 중은 회색으로 못 건드리고, AI 가 다 쓴 항목은 고를 수 있고, 승인은 잠긴다."""

    LOCK = "18:00 마감과 AI 서술이 끝나면 승인할 수 있습니다"

    def setUp(self):
        H._fresh_db()
        import db
        with db.connect() as conn:
            H._shift_row(conn, H.SID, H.WS, H.T(720))
            did = db.open_live_draft(conn, H.SID, "live")
            self.item_id = db.add_live_item(
                conn, did,
                {"origin": "detected", "tag": "TI-101", "title": "TI-101 드리프트", "body": "문장",
                 "evidence": "TI-101 드리프트 근거", "severity": "중", "live": {"key": "p1"}},
                [H.ev("TI-101", "드리프트", 0, 30, 1)], "engine")
            self.event_id = conn.execute("SELECT event_id FROM draft_item WHERE id = ?", (self.item_id,)).fetchone()[0]

    def _fake_live(self, lock=LOCK):
        """live.items·approve_lock 을 계약 모양으로 바꿔 끼운다 — 화면이 그 값을 어떻게 그리는지만 본다.
        실제 추적기에서 나오는 값은 tests/test_live.py 와 실제 재생이 본다."""
        import live
        ready = {"key": "p1", "state": "ready", "origin": "detected", "confirm": "ended", "ongoing": False,
                 "not_in_close": False, "recurrence_of": None, "tag": "TI-101", "kind": "드리프트",
                 "title": "TI-101 드리프트", "severity": "중", "evidence": "TI-101 드리프트 근거", "score": 1.0,
                 "score_max": 1.0, "start": H.iso(H.T(0)), "end": H.iso(H.T(30)), "members": [["TI-101", "드리프트"]],
                 "related_tags": [], "recurrences": [], "stop": None, "first_seen": H.iso(H.T(5)),
                 "confirmed": H.iso(H.T(35)), "ai_sec": 3.0, "draft_item_id": self.item_id, "error": None}
        obs = dict(ready, key="p2", state="observing", tag="PI-201", kind="헌팅", title="PI-201 헌팅",
                   evidence="PI-201 헌팅 근거 — 지금 크기", confirm=None, confirmed=None, ai_sec=None,
                   draft_item_id=None, first_seen=H.iso(H.T(40)))
        live.items = lambda shift_id=None: [ready, obs]
        live.approve_lock = lambda shift_id: lock
        return ready, obs

    def test_observing_is_gray_and_cannot_be_chosen(self):
        self._fake_live()
        import server
        body = server.view_shift(H.SID)
        self.assertIn('<div class="item obs off" data-state="observing"', body, "관찰 중 카드가 있어야 한다")
        card = _card(body, "p2")
        self.assertIn('data-tag="PI-201"', card)
        self.assertIn("지금 크기", card, "무엇이 얼마나 움직이는지 보여야 한다")
        self.assertNotIn("<input", card, "관찰 중은 고르지 못한다")
        self.assertNotIn("<textarea", card)

    def test_written_item_is_selectable_and_carries_ids(self):
        self._fake_live()
        import server
        body = server.view_shift(H.SID)
        card = _card(body, "p1")
        self.assertIn('data-state="ready"', card)
        self.assertIn(f'data-item="{self.item_id}"', card)
        self.assertIn('data-tag="TI-101"', card)
        self.assertIn(f'data-event="{self.event_id}"', card, "녹화 스크립트가 태그·이벤트로 고른다")
        self.assertIn(f'name="item" value="{self.item_id}"', card)
        self.assertIn(f'name="status_{self.item_id}"', card)

    def test_approve_is_locked_while_the_shift_is_still_filling(self):
        self._fake_live()
        import server
        body = server.view_shift(H.SID)
        self.assertIn(self.LOCK, body, "왜 못 누르는지 문구로 보인다")
        self.assertRegex(body, r'<button[^>]*id="approve"[^>]*disabled')

    def test_after_the_shift_closes_the_button_opens(self):
        import db
        import live
        with db.connect() as conn:
            conn.execute("UPDATE draft SET status = 'pending' WHERE shift_id = ?", (H.SID,))
        live.approve_lock = lambda shift_id: None
        live.items = lambda shift_id=None: []
        import server
        body = server.view_shift(H.SID)
        self.assertNotIn(self.LOCK, body)
        self.assertNotIn('<div class="item obs', body, "닫힌 근무에 회색 카드를 그리지 않는다")
        self.assertRegex(body, r'<button[^>]*id="approve"(?![^>]*disabled)')

    def test_api_live_cards_feeds_the_polling(self):
        self._fake_live()
        code, body = _get(f"/api/live/cards?shift={H.SID}")
        self.assertEqual(code, 200)
        j = json.loads(body)
        self.assertEqual(j["draft"], "live")
        self.assertEqual(j["lock"], self.LOCK)
        got = {c["key"]: c for c in j["cards"]}
        self.assertEqual(sorted(got), ["p1", "p2"])
        self.assertEqual((got["p1"]["state"], got["p2"]["state"]), ("ready", "observing"))
        self.assertIn(f'name="status_{self.item_id}"', got["p1"]["html"])
        self.assertIn('data-state="observing"', got["p2"]["html"])


class BatchCardMarkers(unittest.TestCase):
    """일괄 초안 카드에도 같은 표식 — 녹화 스크립트가 위치가 아니라 태그로 고른다(반증 지적)."""

    def setUp(self):
        H._fresh_db()

    def test_batch_draft_cards_carry_tag_and_item_ids(self):
        import db
        with db.connect() as conn:
            H._shift_row(conn, H.SID, H.WS, H.T(720))
            (iid,) = H._pending_draft(conn, H.SID)
        import server
        body = server.view_shift(H.SID)
        self.assertIn(f'data-item="{iid}"', body)
        self.assertIn('data-tag="TI-101"', body)


class DcsLiveValues(unittest.TestCase):
    """DCS 흐름도 숫자 — 재생 중이면 근무 시각의 값으로 바뀐다. 원본 파일(docs/asu_dcs_overview.html)은 고치지 않는다."""

    def setUp(self):
        H._fresh_db()

    def test_values_api_uses_the_number_rule(self):
        import live
        live.values = lambda: {"clock": "2026-08-25T18:07:00",
                               "values": {"FI-602": ["2026-08-25T18:06:58", 40131.4],
                                          "XI-001": ["2026-08-25T18:06:58", 3.2e-05]}}
        code, body = _get("/api/live/values")
        self.assertEqual(code, 200)
        j = json.loads(body)
        self.assertEqual(j["clock"], "2026-08-25T18:07:00")
        self.assertEqual(j["values"]["FI-602"], "40,131", "앱 숫자 규칙으로 찍는다")
        self.assertEqual(j["values"]["XI-001"], "0.000032")
        self.assertNotRegex(body, r"\d[eE][+-]?\d", "지수 표기가 나가면 안 된다")

    def test_dcs_page_carries_one_updater_for_the_tag_boxes(self):
        import server
        body = server.view_dcs()
        self.assertEqual(body.count("/api/live/values"), 1, "덧붙이는 스크립트는 하나")
        self.assertIn('g[data-tag=', body, "태그 상자만 건드린다")
        self.assertIn("text.tv", body, "값 글자만 바꾼다")

    def test_the_original_dcs_file_has_the_structure_the_updater_needs(self):
        """원본이 바뀌어 구조가 어긋나면 조용히 안 바뀌는 대신 여기서 걸린다."""
        src = (pathlib.Path(ROOT) / "docs" / "asu_dcs_overview.html").read_text(encoding="utf-8")
        self.assertIn("'data-tag': tag[0]", src, "태그마다 g[data-tag] 가 있어야 한다")
        self.assertIn("txt('tv'", src, "그 안에 값 글자(text.tv)가 있어야 한다")


class ChainAndSpeed(unittest.TestCase):
    """이어서 재생 — 여러 근무를 시각순으로 골라 한 번에 틀고, 배속은 폼에서 받는다(경모님: start 하면 stop 할 때까지 연속)."""

    def setUp(self):
        H._fresh_db()
        os.environ.pop("ENGRA_ADMIN_KEY", None)

    def test_form_takes_several_shifts_and_a_speed(self):
        _, names = H._uploads(H._csv(minutes=10), H._csv(minutes=10, start=H.WS + __import__("datetime").timedelta(hours=12)))
        import server
        body = server.view_admin()
        self.assertRegex(body, r'<select name="csv"[^>]*multiple', "여러 근무를 고를 수 있어야 한다")
        self.assertRegex(body, r'<input[^>]*name="speed"[^>]*value="100"', "배속 칸 기본 100")
        self.assertRegex(body, r'<input[^>]*name="speed"[^>]*max="300"', "상한 300")
        for n in names:
            self.assertIn(f'value="{n}"', body)

    def test_start_passes_the_chain_in_order_and_the_speed(self):
        _, names = H._uploads(H._csv(minutes=10), H._csv(minutes=10, start=H.WS + __import__("datetime").timedelta(hours=12)))
        import live
        got = {}
        live.start = lambda csvs, speed=100, prev_csv_name=None, replace_unconfirmed=False: (
            got.update(csvs=list(csvs), speed=speed, prev=prev_csv_name), live.status())[1]
        code, _ = _post("/live/start", [("csv", names[0]), ("csv", names[1]), ("speed", "200")])
        self.assertEqual(code, 303)
        self.assertEqual(got["csvs"], [names[0], names[1]], "고른 순서대로 넘긴다")
        self.assertEqual(got["speed"], 200.0)

    def test_speed_over_the_cap_is_refused_with_the_reason(self):
        _, (name,) = H._uploads(H._csv(minutes=10))
        code, body = _post("/live/start", [("csv", name), ("speed", "600")])
        self.assertEqual(code, 400)
        self.assertIn("300", body, "왜 막았는지 상한을 적는다")

    def test_status_line_says_it_is_catching_up_when_ticks_fall_behind(self):
        import server
        st = dict(server.live.status(), phase="running", shift_id="2026-08-25-day", clock="2026-08-25T09:00:00",
                  counts={"observing": 1, "writing": 0, "ready": 2}, tick={"t": "x", "n": 5, "of": 144, "events": 1,
                                                                          "items": 1, "sec": 1.2, "late_sec": 22.0})
        self.assertIn("따라잡는 중", server._live_line(st))
        self.assertNotIn("따라잡는 중", server._live_line(dict(st, tick=dict(st["tick"], late_sec=0.4))))


class LiveSummaryLine(unittest.TestCase):
    """쌓이는 중 화면의 오른쪽 요약 — 「감지 0건」은 회색 카드가 보이는데 앞뒤가 안 맞는다(지휘자 실측)."""

    def setUp(self):
        H._fresh_db()
        import db
        with db.connect() as conn:
            H._shift_row(conn, H.SID, H.WS, H.T(720))
            db.open_live_draft(conn, H.SID, "live")

    def test_live_summary_counts_cards_not_draft_rows(self):
        import live
        import server
        view = {"key": "p2", "state": "observing", "origin": "detected", "confirm": None, "ongoing": None,
                "not_in_close": False, "recurrence_of": None, "tag": "PI-201", "kind": "헌팅", "title": "PI-201 헌팅",
                "severity": "중", "evidence": "지금 크기", "score": 1.0, "score_max": 1.0, "start": H.iso(H.T(0)),
                "end": H.iso(H.T(10)), "members": [], "related_tags": [], "recurrences": [], "stop": None,
                "first_seen": H.iso(H.T(5)), "confirmed": None, "ai_sec": None, "draft_item_id": None, "error": None}
        live.items = lambda shift_id=None: [view, dict(view, key="p3", tag="TI-301", title="TI-301 드리프트")]
        live.approve_lock = lambda shift_id: "06:00 마감 후 승인"
        body = server.view_shift(H.SID)
        self.assertRegex(body, r"관찰 중 <b[^>]*>2</b>", "지금 보이는 회색 카드 수를 그대로 센다")
        self.assertNotRegex(body, r"감지\s*<b[^>]*>0</b>건", "초안 표의 0 을 「감지 0건」으로 내보이지 않는다")
        self.assertNotIn("전체 표시 (걸러내지 않음)", body, "쌓이는 중에는 뜻이 없는 줄")

    def test_closed_draft_still_shows_the_detection_count(self):
        import db
        import live
        with db.connect() as conn:
            conn.execute("UPDATE draft SET status = 'pending' WHERE shift_id = ?", (H.SID,))
        live.items = lambda shift_id=None: []
        live.approve_lock = lambda shift_id: None
        import server
        body = server.view_shift(H.SID)
        self.assertRegex(body, r"감지 <b[^>]*>\d+</b>건", "건수는 남고")
        self.assertNotIn("전체 표시 (걸러내지 않음)", body, "설명은 뺀다(경모님 지시 2026-09-15)")


class StaleLiveDraftScreen(unittest.TestCase):
    """재생이 이 근무를 떠난 뒤(체인이 다음 근무로 갔거나 멈춘 재생) 남은 'live' 초안 — 폴링이 같은 카드를 또 넣으면 안 된다."""

    def setUp(self):
        H._fresh_db()
        import db
        with db.connect() as conn:
            H._shift_row(conn, H.SID, H.WS, H.T(720))
            did = db.open_live_draft(conn, H.SID, "live")
            self.item_id = db.add_live_item(
                conn, did,
                {"origin": "detected", "tag": "TI-101", "title": "TI-101 드리프트", "body": "문장",
                 "evidence": "근거", "severity": "중", "live": {"key": "p1"}},
                [H.ev("TI-101", "드리프트", 0, 30, 1)], "engine")

    def test_cards_keep_their_key_even_when_the_replay_left_the_shift(self):
        import live
        import server
        live.items = lambda shift_id=None: []          # 이 재생은 다른 근무를 쌓고 있다(또는 멈췄다)
        live.approve_lock = lambda shift_id: "재생이 중단된 구간 — 다시 재생하거나 일괄 실행한 뒤 승인"
        body = server.view_shift(H.SID)
        self.assertRegex(body, r'data-state="ready"[^>]*data-key="i%d"' % self.item_id,
                         "화면 카드에도 표식이 있어야 폴링이 같은 카드를 또 넣지 않는다")
        code, raw = _get(f"/api/live/cards?shift={H.SID}&have=i{self.item_id}")
        self.assertEqual(code, 200)
        self.assertEqual([c["key"] for c in json.loads(raw)["cards"]], [], "이미 그린 카드는 다시 내려보내지 않는다")

    def test_counts_belong_to_the_shift_that_is_being_replayed(self):
        import live
        import server
        live.status = lambda: dict(server.live._IDLE, phase="running", shift_id="2026-08-26-day",
                                   clock="2026-08-26T07:00:00", counts={"observing": 9, "writing": 0, "ready": 3})
        live.items = lambda shift_id=None: []
        live.approve_lock = lambda shift_id: None
        j = json.loads(_get(f"/api/live/cards?shift={H.SID}")[1])
        self.assertEqual((j["counts"], j["clock"]), ({}, None), "다른 근무의 개수를 이 줄에 넣지 않는다")


class AiFailureRetry(unittest.TestCase):
    """AI 서술이 끝내 실패한 항목 — 조용히 넘기지 않는다. 화면에 남고 승인이 잠기며, 관리에서 다시 시도한다."""

    def setUp(self):
        H._fresh_db()
        os.environ.pop("ENGRA_ADMIN_KEY", None)

    def _failed(self):
        import live
        v = {"key": "p4", "state": "ai_failed", "origin": "detected", "confirm": "ended", "ongoing": False,
             "not_in_close": False, "recurrence_of": None, "tag": "MI-804", "kind": "이탈", "title": "MI-804 이탈",
             "severity": "상", "evidence": "근거", "score": 1.0, "score_max": 1.0, "start": H.iso(H.T(0)),
             "end": H.iso(H.T(20)), "members": [], "related_tags": [], "recurrences": [], "stop": None,
             "first_seen": H.iso(H.T(5)), "confirmed": H.iso(H.T(25)), "ai_sec": None, "draft_item_id": None,
             "error": "AI 호출 실패 — 시험용"}
        live.items = lambda shift_id=None: [v]
        live.approve_lock = lambda shift_id: "AI 서술 실패 1건 — 다시 시도한 뒤 승인"
        return v

    def test_admin_lists_failed_items_with_a_retry_button(self):
        self._failed()
        import server
        body = server.view_admin()
        self.assertIn('action="/live/retry"', body, "관리에서 다시 시도할 수 있어야 한다")
        self.assertIn('value="p4"', body, "어느 항목인지 표식으로 보낸다")
        self.assertIn("MI-804", body)
        self.assertIn("AI 호출 실패 — 시험용", body, "왜 실패했는지 보인다")

    def test_retry_calls_live_and_refusals_are_shown(self):
        self._failed()
        import live
        got = []
        live.retry = lambda key: (got.append(key), live.status())[1]
        code, _ = _post("/live/retry", [("key", "p4")])
        self.assertEqual(code, 303)
        self.assertEqual(got, ["p4"])

        def refuse(key):
            raise ValueError(f"항목 {key} 가 없습니다.")
        live.retry = refuse
        code, body = _post("/live/retry", [("key", "없는키")])
        self.assertEqual(code, 400)
        self.assertIn("없는키", body)

    def test_retry_needs_the_admin_key(self):
        self._failed()
        os.environ["ENGRA_ADMIN_KEY"] = "열쇠"
        code, _ = _post("/live/retry", [("key", "p4")])
        self.assertEqual(code, 403)

    def test_failed_card_is_visible_on_the_draft_screen(self):
        import db
        with db.connect() as conn:
            H._shift_row(conn, H.SID, H.WS, H.T(720))
            db.open_live_draft(conn, H.SID, "live")
        self._failed()
        import server
        body = server.view_shift(H.SID)
        self.assertIn('<div class="item obs off" data-state="ai_failed"', body, "실패한 항목이 화면에서 사라지면 안 된다")
        self.assertIn("AI 호출 실패 — 시험용", body)
        self.assertRegex(body, r'<button[^>]*id="approve"[^>]*disabled', "실패가 남아 있으면 승인은 잠긴다")


class PlainScreens(unittest.TestCase):
    """경모님 지시 — 화면에서 기능을 설명하는 문장을 전부 뺀다. 상태 이름과 잠금 사유 한 줄만 남긴다.
    「수정」은 확정 일지 오른쪽 위에 작은 글자 버튼으로."""

    GONE = ("감지가 지금도 쌓이고 있습니다", "회색은 관찰 중이라", "감지된 항목을 <b", "최종 판단은 근무자가 합니다",
            "전체 표시 (걸러내지 않음)", "근무를 누르면 초안 검토", "눌러서 바로 고칩니다",
            "감지되지 않았지만 넘겨야 할 것이 있으면", "제외 항목도 기록으로 남습니다")

    def setUp(self):
        H._fresh_db()
        import db
        with db.connect() as conn:
            H._shift_row(conn, H.SID, H.WS, H.T(720))
            (self.iid,) = H._pending_draft(conn, H.SID)

    def test_draft_and_list_have_no_explaining_sentences(self):
        import server
        body = server.view_shift(H.SID)
        for phrase in self.GONE:
            self.assertNotIn(phrase, body, f"설명 문장이 남았다: {phrase}")
        self.assertIn("승인하고 확정", body, "버튼과 상태 이름은 남는다")

    def test_confirmed_log_has_a_small_edit_link_on_the_right(self):
        import approve
        import server
        approve.decide(H.SID, {self.iid: {"adopted": True, "status": "완료"}})
        body = server.view_shift(H.SID)
        for phrase in self.GONE:
            self.assertNotIn(phrase, body, f"설명 문장이 남았다: {phrase}")
        self.assertRegex(body, r'class="edit"[^>]*>\s*수정\s*<', "오른쪽 위 작은 글자 버튼")
        self.assertIn('action="/reopen"', body)

    def test_live_screen_keeps_only_the_lock_reason(self):
        import db
        import live
        import server
        with db.connect() as conn:
            conn.execute("DELETE FROM draft_item WHERE draft_id IN (SELECT id FROM draft WHERE shift_id = ?)", (H.SID,))
            conn.execute("DELETE FROM draft WHERE shift_id = ?", (H.SID,))
            db.open_live_draft(conn, H.SID, "live")
        live.items = lambda shift_id=None: []
        live.approve_lock = lambda shift_id: "06:00 마감 후 승인"
        body = server.view_shift(H.SID)
        for phrase in self.GONE:
            self.assertNotIn(phrase, body, f"설명 문장이 남았다: {phrase}")
        self.assertIn("06:00 마감 후 승인", body, "왜 못 누르는지는 남긴다")


class LiveCardOrderAndRefresh(unittest.TestCase):
    """경모님 지시 — 관찰 중을 맨 위에, 그 안에서 가장 최근에 잡힌 것이 위로. 관찰 카드는 폴링마다 다시 그려 값이 움직여야 한다."""

    def setUp(self):
        H._fresh_db()
        import db
        with db.connect() as conn:
            H._shift_row(conn, H.SID, H.WS, H.T(720))
            did = db.open_live_draft(conn, H.SID, "live")
            self.item_id = db.add_live_item(
                conn, did,
                {"origin": "detected", "tag": "TI-101", "title": "TI-101 드리프트", "body": "문장",
                 "evidence": "근거", "severity": "중", "live": {"key": "p1"}},
                [H.ev("TI-101", "드리프트", 0, 30, 1)], "engine")

    def _views(self, evidence="지금 크기 10.0"):
        base = {"key": "p1", "state": "ready", "origin": "detected", "confirm": "ended", "ongoing": False,
                "not_in_close": False, "recurrence_of": None, "tag": "TI-101", "kind": "드리프트",
                "title": "TI-101 드리프트", "severity": "중", "evidence": "근거", "score": 1.0, "score_max": 1.0,
                "start": H.iso(H.T(0)), "end": H.iso(H.T(30)), "members": [], "related_tags": [], "recurrences": [],
                "stop": None, "first_seen": H.iso(H.T(5)), "confirmed": H.iso(H.T(35)), "ai_sec": 1.0,
                "draft_item_id": self.item_id, "error": None}
        old = dict(base, key="p2", state="observing", tag="PI-201", title="PI-201 헌팅", evidence=evidence,
                   confirm=None, confirmed=None, draft_item_id=None, first_seen=H.iso(H.T(10)))
        new = dict(old, key="p3", tag="MI-804", title="MI-804 이탈", first_seen=H.iso(H.T(200)))
        return base, old, new

    def test_observing_cards_come_first_newest_on_top(self):
        import live
        import server
        ready, old, new = self._views()
        live.items = lambda shift_id=None: [ready, old, new]
        live.approve_lock = lambda shift_id: "06:00 마감 후 승인"
        body = server.view_shift(H.SID)
        order = [k for k in ("p3", "p2", "p1") if f'data-key="{k}"' in body]
        pos = {k: body.index(f'data-key="{k}"') for k in order}
        self.assertEqual(sorted(pos, key=pos.get), ["p3", "p2", "p1"],
                         "관찰 중이 위(최근 것이 맨 위) · 선택 가능은 그 아래")
        j = json.loads(_get(f"/api/live/cards?shift={H.SID}")[1])
        self.assertEqual(j["order"], ["p3", "p2", "p1"], "폴링도 같은 차례를 준다")

    def test_observing_cards_are_sent_again_even_when_the_screen_has_them(self):
        import live
        ready, old, new = self._views()
        live.items = lambda shift_id=None: [ready, old, new]
        live.approve_lock = lambda shift_id: None
        j = json.loads(_get(f"/api/live/cards?shift={H.SID}&have=p1,p2,p3")[1])
        got = {c["key"] for c in j["cards"]}
        self.assertEqual(got, {"p2", "p3"}, "관찰 카드는 매번 다시 보내 값이 움직이게 한다(선택 가능 카드는 그대로 둔다)")

    def test_observing_card_shows_a_small_trend(self):
        import db
        import live
        import server
        with db.connect() as conn:      # 원본이 쌓이면 관찰 카드에도 추이가 붙는다
            conn.executemany("INSERT INTO raw_sample (tag, ts, value) VALUES (?,?,?)",
                             [("PI-201", H.iso(H.T(i)), 10.0 + i * 0.5) for i in range(0, 60, 2)])
        ready, old, new = self._views()
        live.items = lambda shift_id=None: [old]
        live.approve_lock = lambda shift_id: None
        card = _card(server.view_shift(H.SID), "p2")
        self.assertIn("<svg", card, "관찰 중에도 추이가 보여야 한다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
