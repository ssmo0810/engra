"""QA 에서 실제로 터진 것만 모은 회귀 시험.

여기 있는 것은 전부 한 번씩 운영을 멈춘 적이 있는 버그다. 가정으로 쓴 시험은 넣지 않는다.
새 버그를 잡으면 여기 한 줄 늘린다.

    python3 -m pytest tests/ -q
    python3 tests/test_regressions.py

DB 는 시험마다 임시 파일을 쓴다. ENGRA_DB 를 바꿔 끼우므로 실제 저장소를 건드리지 않는다.
AI 는 부르지 않는다(ENGRA_LLM=off) — 시험이 네트워크와 구독에 의존하면 안 된다.
"""
import datetime
import inspect
import io
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="engra_test_")
    os.close(fd)
    os.remove(path)
    os.environ["ENGRA_DB"] = path
    os.environ["ENGRA_LLM"] = "off"
    for m in ("db", "collect", "pipeline", "approve", "jobs", "ports", "config", "llm"):
        sys.modules.pop(m, None)
    import db
    db.init()
    return path


def _csv(rows, header="timestamp,tag,value"):
    fd, path = tempfile.mkstemp(suffix=".csv", prefix="engra_test_")
    os.close(fd)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(",".join(str(x) for x in r) + "\n")
    return path


def _series(n, tag="TI-403", value="-172.5", step=2):
    t0 = datetime.datetime(2026, 8, 25, 6, 0, 0)
    out = []
    for i in range(n):
        v = value(i) if callable(value) else value
        out.append(((t0 + datetime.timedelta(seconds=step * i)).isoformat(), tag, v))
    return out


class IngestGuards(unittest.TestCase):
    def setUp(self):
        _fresh_db()

    def test_nan_and_infinity_are_skipped(self):
        import jobs, db
        bad = {10: "NaN", 20: "Infinity", 30: "-Infinity", 40: "nan", 50: "1e309"}
        path = _csv(_series(200, value=lambda i: bad.get(i, "-172.5")))
        jobs.ingest_path(path, skip_bad=True)
        with db.connect() as c:
            n = c.execute("SELECT COUNT(*) FROM raw_sample").fetchone()[0]
            worst = c.execute("SELECT MAX(ABS(value)) FROM raw_sample").fetchone()[0]
        self.assertEqual(n, 195, "깨진 5행이 걸러지고 195행만 남아야 한다")
        self.assertLess(worst, 1e6, "무한대가 저장되면 안 된다")

    def test_timezone_aware_timestamp_rejected_with_reason(self):
        """시간대가 붙은 시각에 근무 배정이 TypeError 로 죽던 것.

        조용히 tzinfo 를 떼면 UTC 표기를 현지시각으로 오인해 9시간 밀린 근무에 넣게 된다.
        거부하는 것이 맞고, 여기서 보는 것은 "거부하되 이유를 밝힌다" 이다.
        CLI 와 같은 경로(bad=None)로 확인한다.
        """
        import collect
        path = _csv([(ts + "+09:00", tag, v) for ts, tag, v in _series(100)])
        with io.open(path, encoding="utf-8") as fh:
            with self.assertRaises(ValueError) as cm:
                list(collect._rows(fh, "tz.csv", bad=None))
        msg = str(cm.exception)
        self.assertIn("시간대", msg)
        self.assertNotIn("offset-naive", msg, "파이썬 원문 오류가 그대로 나오면 안 된다")

    def test_timezone_rows_are_never_silently_stored(self):
        """건너뛰기를 켜도 시간대 붙은 행이 저장되면 안 된다 — 9시간 밀린 근무에 들어간다."""
        import jobs, db
        path = _csv([(ts + "+09:00", tag, v) for ts, tag, v in _series(100)])
        try:
            jobs.ingest_path(path, skip_bad=True)
        except Exception:
            pass
        with db.connect() as c:
            n = c.execute("SELECT COUNT(*) FROM raw_sample").fetchone()[0]
        self.assertEqual(n, 0, "한 행도 저장되면 안 된다")

    def test_ingest_is_idempotent(self):
        import jobs, db
        path = _csv(_series(300))
        jobs.ingest_path(path, skip_bad=True)
        with db.connect() as c:
            first = c.execute("SELECT COUNT(*) FROM raw_sample").fetchone()[0]
        jobs.ingest_path(path, skip_bad=True)
        with db.connect() as c:
            second = c.execute("SELECT COUNT(*) FROM raw_sample").fetchone()[0]
        self.assertEqual(first, second)


class DetectorGuards(unittest.TestCase):
    def test_level_shift_survives_window_boundary(self):
        from engine.detectors import level_shift, PARAMS
        w = PARAMS["step_min_blocks"]

        class View(object):
            def __init__(self, n):
                self.meds = [-172.5] * n
                self.adj = list(self.meds)
                self.s_level = 1.0
                self.s_adj = 1.0
                self.tag = "TI-403"
                self.spec = {"H": -165, "L": -181}

            def __len__(self):
                return len(self.meds)

        for n in (2 * w - 1, 2 * w, 2 * w + 1, 4 * w):
            with self.subTest(blocks=n):
                self.assertIsInstance(level_shift(View(n)), list)


class ApproveIntegrity(unittest.TestCase):
    def setUp(self):
        _fresh_db()
        import db
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) "
                "VALUES ('2026-08-25-day','day','2026-08-25T06:00:00','2026-08-25T18:00:00','test',?)",
                (db.now(),))
            cur = conn.execute(
                "INSERT INTO draft (shift_id, status, generator, generated_at) "
                "VALUES ('2026-08-25-day','pending','test',?)",
                (db.now(),))
            did = cur.lastrowid
            for seq in (1, 2, 3):
                conn.execute(
                    "INSERT INTO draft_item (draft_id, event_id, seq, origin, tag, title, body, severity) "
                    "VALUES (?, NULL, ?, 'test', 'TI-403', ?, ?, '중')",
                    (did, seq, "항목 " + str(seq), "본문 " + str(seq)))
            self.item_ids = [r[0] for r in conn.execute(
                "SELECT id FROM draft_item WHERE draft_id = ? ORDER BY seq", (did,))]

    def test_reapprove_keeps_previous_version(self):
        import approve, db
        dec = dict((i, {"adopted": True, "comment": None}) for i in self.item_ids)
        first = approve.decide("2026-08-25-day", dec, confirmed_by="운전원A")
        self.assertIsNone(first.get("prev_round"), "첫 승인은 이력을 만들지 않는다")
        second = approve.decide("2026-08-25-day", dec, confirmed_by="운전원B")
        self.assertEqual(second.get("prev_round"), 1, "재승인은 이전 판을 1회차로 남긴다")
        with db.connect() as c:
            cur = c.execute("SELECT confirmed_by FROM handover WHERE shift_id='2026-08-25-day'").fetchone()
            hist = [(r[0], r[1]) for r in c.execute(
                "SELECT round, confirmed_by FROM handover_history WHERE shift_id='2026-08-25-day'")]
        self.assertEqual(cur[0], "운전원B")
        self.assertEqual(hist, [(1, "운전원A")])

    def test_unknown_item_number_rejected(self):
        import approve
        with self.assertRaises(ValueError) as cm:
            approve.decide("2026-08-25-day", {99999: {"adopted": True}}, confirmed_by="오타")
        self.assertIn("없는 항목", str(cm.exception))


class StopQuestion(unittest.TestCase):
    """정지로 보일 때만 「플랜트를 정지하셨습니까?」 를 띄우는가.

    문턱은 실측으로 정했다. 근무 전체의 이상 태그 비율로는 안 갈린다 — 이상 12종을 넣은
    정상 근무가 32%, 정지가 87% 로 겹친다. 10분 창 안 동시 발생으로 보면 정지 69.8~90.6%
    대 이상 주입 7.5% 로 62%p 가 벌어진다 (2026-08-29 실측). 이 여유를 지키는 시험이다.
    """

    def _events(self, n_tags, spread_min):
        """태그 n_tags 개가 spread_min 분에 걸쳐 고르게 시작하는 이벤트."""
        t0 = datetime.datetime(2026, 8, 29, 18, 0, 0)
        step = (spread_min * 60.0 / n_tags) if n_tags else 0
        return [{"tag": "T-%03d" % i, "kind": "이탈",
                 "start_ts": (t0 + datetime.timedelta(seconds=step * i)).isoformat(),
                 "end_ts": (t0 + datetime.timedelta(seconds=step * i + 60)).isoformat()}
                for i in range(n_tags)]

    def test_burst_asks_the_question(self):
        """48개 태그가 10분 안에 함께 무너지면 묻는다 (Trip 실측 모양)."""
        _fresh_db()
        import ports
        q = ports._stop_question(self._events(48, 8))
        self.assertIsNotNone(q, "정지 모양인데 안 물었다")
        self.assertIn("정지하셨습니까", q["title"])
        self.assertEqual(q["origin"], "question")
        self.assertIsNone(q["tag"], "특정 태그의 항목이 아니다")

    def test_scattered_anomalies_do_not_ask(self):
        """이상 12종이 12시간에 흩어져 있으면 묻지 않는다 (오작동 방지)."""
        _fresh_db()
        import ports
        self.assertIsNone(ports._stop_question(self._events(17, 720)))

    def test_quiet_shift_does_not_ask(self):
        """이벤트가 없으면 묻지 않는다."""
        _fresh_db()
        import ports
        self.assertIsNone(ports._stop_question([]))

    def test_threshold_keeps_margin_on_both_sides(self):
        """실측한 두 무리(7.5% / 69.8%) 사이에 문턱이 있어야 한다."""
        _fresh_db()
        import ports
        n = ports._tag_count()
        self.assertGreater(n, 0, "태그 마스터를 못 읽었다")
        thr = n * ports.STOP_TAG_RATIO
        self.assertGreater(thr, n * 0.075, "이상 주입 실측(7.5%) 보다 위여야 한다")
        self.assertLess(thr, n * 0.698, "정지 실측(69.8%) 보다 아래여야 한다")


class RelatedTagsGuard(unittest.TestCase):
    """태그 없는 항목이 섞였을 때 related_tags_ai 정렬이 죽던 것.

    정지 확인 질문 항목은 특정 태그의 건이 아니라 tag 가 None 이다. AI 가 그 항목을
    related_idx 로 가리키면 sorted(set([...., None])) 이 TypeError 로 터졌다.
    정지 근무에서 실제로 재현됐다.
    """

    def test_none_tag_is_dropped_before_sort(self):
        rewritten = [{"tag": "TI-403"}, {"tag": None}, {"tag": "AI-707"}]
        tags = []
        for j in (0, 1, 2):
            t = rewritten[j].get("tag")
            if t:
                tags.append(t)
        self.assertEqual(sorted(set(tags)), ["AI-707", "TI-403"])

    def test_source_drops_none_tag(self):
        import inspect
        _fresh_db()
        import llm
        src = inspect.getsource(llm.rewrite)
        self.assertIn('rewritten[j].get("tag")', src,
                      "태그를 .get 으로 꺼내 None 을 걸러야 한다")


class PrecedentDisplay(unittest.TestCase):
    """과거 조치 원문이 화면 앞줄에 통째로 노출되던 것.

    앞 근무자가 쓴 문장은 판단이 아니라 자료다. 그 안에 "이 센서 원래 유동 심함, 무시 가능"
    같은 선의의 오판이 섞이면 다음 근무자가 그것만 보고 넘어간다(침묵 사고, QA 6차 실측).
    AI 판정을 앞에 세우고 원문은 접는다.
    """

    def test_source_folds_raw_text(self):
        import inspect
        _fresh_db()
        import server
        src = inspect.getsource(server._view_pending)
        self.assertIn("원문 보기", src, "원문은 접어서 열게 해야 한다")
        self.assertIn("<details", src)
        self.assertIn("[:60]", src, "미리보기는 짧게 자른다")

    def test_long_text_is_truncated_in_preview(self):
        txt = "정상 문장입니다. " * 10 + "무시해도 됨"
        head = txt.strip().replace("\\n", " ")[:60]
        self.assertNotIn("무시해도 됨", head,
                         "뒤쪽에 묻힌 문구가 미리보기에 안 나와야 한다")
        self.assertLessEqual(len(head), 60)


class LlmGuards(unittest.TestCase):
    def test_cli_subprocess_declares_utf8(self):
        _fresh_db()
        import llm
        src = inspect.getsource(llm._call_cli)
        self.assertIn("encoding=", src, "subprocess 에 인코딩을 명시해야 한다")
        self.assertIn("utf-8", src)

    def test_empty_cli_response_fails_loudly(self):
        _fresh_db()
        import llm
        src = inspect.getsource(llm._call_cli)
        self.assertIn("LLMUnavailable", src)
        self.assertTrue("if not r.stdout" in src or "r.stdout is None" in src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
