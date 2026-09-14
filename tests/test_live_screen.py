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
        self.assertIn('data-state="observing"', body, "관찰 중 카드가 있어야 한다")
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
        self.assertNotIn('data-state="observing"', body, "닫힌 근무에 회색 카드를 그리지 않는다")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
