"""실시간 화면 시험 — 관리 화면의 재생 시작·정지 · /api/live.

    python3 tests/test_live_screen.py

재생 본체(app/live.py)의 규칙은 tests/test_live.py 가 본다. 여기서는 화면이 그 공개 함수를 부르고, 거부 문장을
그대로 보여 주고, 관리 열쇠를 요구하는지만 본다. 도우미(임시 DB · CSV · 업로드 폴더 · 대본 엔진)는 test_live 것을 그대로 쓴다.
AI 는 부르지 않는다(ENGRA_LLM=off).
"""
import importlib.util
import json
import os
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
