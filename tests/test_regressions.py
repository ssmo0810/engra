"""QA 에서 실제로 터진 것만 모은 회귀 시험.

여기 있는 것은 전부 한 번씩 운영을 멈춘 적이 있는 버그다. 가정으로 쓴 시험은 넣지 않는다.
새 버그를 잡으면 여기 한 줄 늘린다.

    python3 -m pytest tests/ -q
    python3 tests/test_regressions.py

DB 는 시험마다 임시 파일을 쓴다. ENGRA_DB 를 바꿔 끼우므로 실제 저장소를 건드리지 않는다.
AI 는 부르지 않는다(ENGRA_LLM=off) — 시험이 네트워크와 구독에 의존하면 안 된다.

**시험을 추가하면 일부러 되돌려 보고 실패하는지 확인한다.** 통과만 보고 넣으면 아무것도
안 지키는 시험이 쌓인다. 실제로 CUSUM 시험이 처음에 함수만 불러서, `drift()` 안의 호출
한 줄을 지워도 통과했다 — 호출부까지 보도록 고쳤다.
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


class ShiftBoundary(unittest.TestCase):
    """근무 배정이 시각 경계에서 흔들리지 않는가.

    자정·월말·연말·윤일에서 근무가 쪼개지거나 창이 12시간이 아니게 되면 그 근무의 채점이
    통째로 어긋난다. 5차에서 12건을 손으로 확인했는데, 손으로 한 것은 다음에 또 해야 한다.
    """

    CASES = [
        ("2026-08-21T05:59:59", "2026-08-20-night", "주간 시작 직전"),
        ("2026-08-21T06:00:00", "2026-08-21-day", "주간 시작"),
        ("2026-08-21T17:59:59", "2026-08-21-day", "주간 끝"),
        ("2026-08-21T18:00:00", "2026-08-21-night", "야간 시작"),
        ("2026-08-21T23:59:59", "2026-08-21-night", "자정 직전"),
        ("2026-08-22T00:00:00", "2026-08-21-night", "자정 — 같은 근무"),
        ("2026-08-31T18:00:00", "2026-08-31-night", "월말 야간"),
        ("2026-09-01T05:59:59", "2026-08-31-night", "월 넘김 — 같은 근무"),
        ("2026-12-31T18:00:00", "2026-12-31-night", "연말 야간"),
        ("2027-01-01T05:00:00", "2026-12-31-night", "해 넘김 — 같은 근무"),
        ("2026-02-28T18:00:00", "2026-02-28-night", "2월 말"),
        ("2028-02-29T06:00:00", "2028-02-29-day", "윤일"),
    ]

    def test_every_boundary(self):
        _fresh_db()
        import collect
        for ts, want, label in self.CASES:
            with self.subTest(label=label):
                t = datetime.datetime.fromisoformat(ts)
                sid, kind, a, b = collect.shift_id_for(t)
                self.assertEqual(sid, want, label)
                self.assertTrue(a <= t < b, f"{label}: 시각이 창 밖")
                self.assertAlmostEqual((b - a).total_seconds(), 12 * 3600, places=3,
                                       msg=f"{label}: 창이 12시간이 아니다")


class ScoringRules(unittest.TestCase):
    """채점 규칙 — 여기가 틀리면 모든 수치가 틀린다.

    4차에서 두 가지를 고쳤다. 앞 근무에서 넘어온 짧은 조각을 미탐지로 세던 것과,
    주입이 끝나고 값이 돌아오는 구간을 그냥 오탐으로만 세던 것이다.
    """

    def _score(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sc", os.path.join(ROOT, "tools", "score.py"))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def test_min_detectable_matches_engine_guard(self):
        """분모에서 빼는 기준이 엔진의 최소 판정 길이와 맞아야 한다."""
        _fresh_db()
        sc = self._score()
        from engine.core import BLOCK_SEC
        from engine.detectors import PARAMS
        need = 20 * BLOCK_SEC          # 검출기 n < 20 가드
        self.assertEqual(sc.MIN_DETECTABLE_SEC, need,
                         "엔진이 20블록을 요구하면 채점도 그 길이를 기준으로 해야 한다")
        self.assertLess(38, sc.MIN_DETECTABLE_SEC,
                        "실측으로 나온 38초 꼬리는 이 기준 아래여야 한다")

    def test_recovery_window_is_reasonable(self):
        """복귀 판정 창 — 실측 간격(0~8분)을 담되 지나치게 넓으면 안 된다."""
        _fresh_db()
        sc = self._score()
        self.assertGreaterEqual(sc.RECOVERY_WINDOW_SEC, 8 * 60,
                                "실측 최대 간격 8분을 담아야 한다")
        self.assertLessEqual(sc.RECOVERY_WINDOW_SEC, 15 * 60,
                             "너무 넓으면 진짜 오탐까지 복귀로 덮는다")

    def test_only_drift_is_trend_kind(self):
        """추세형으로 분류되는 것은 드리프트뿐이다.

        드리프트만 관찰 창을 적는다(실측 평균 247분·최대 720분, 나머지는 최대 120분).
        여기에 다른 종류를 넣으면 IoU 를 유리하게 나누는 것이 된다.
        """
        _fresh_db()
        sc = self._score()
        self.assertEqual(tuple(sc.TREND_KINDS), ("드리프트",))


class DriftWindowNarrowing(unittest.TestCase):
    """드리프트 보고 구간을 CUSUM 으로 좁히는 것.

    검출 여부는 안 바꾸고 보고 구간만 좁힌다. 좁히지 못하면 원래 구간을 그대로 돌려준다 —
    부가 기능이지 판정이 아니기 때문이다.
    """

    def test_narrows_when_change_starts_late(self):
        """앞은 평평하고 뒤에서 오르면 오른 지점부터 보고한다."""
        from engine.detectors import _cusum_narrow
        n = 240
        src = [0.0] * 160 + [(k + 1) * 0.5 for k in range(n - 160)]
        i, j = _cusum_narrow(src, 0, n, s_adj=1.0)
        self.assertGreater(i, 100, "변화가 시작된 뒤로 시작점이 밀려야 한다")
        self.assertEqual(j, n, "끝은 그대로 둔다")

    def test_keeps_window_when_flat(self):
        """움직임이 없으면 원래 구간을 그대로 돌려준다."""
        from engine.detectors import _cusum_narrow
        src = [0.0] * 200
        self.assertEqual(_cusum_narrow(src, 0, 200, s_adj=1.0), (0, 200))

    def test_keeps_window_when_too_short(self):
        """블록이 너무 적으면 손대지 않는다."""
        from engine.detectors import _cusum_narrow
        self.assertEqual(_cusum_narrow([0.0] * 10, 0, 10, s_adj=1.0), (0, 10))

    def test_drift_actually_uses_the_narrowed_window(self):
        """호출부가 살아 있는가.

        함수만 시험하면 `drift()` 안의 호출 한 줄을 지워도 통과한다 — 실제로 그렇게
        되돌려 보고 안 잡히는 것을 확인했다. 좁힌 결과가 이벤트 구간으로 나가는지까지 본다.
        """
        import inspect
        from engine import detectors
        src = inspect.getsource(detectors.drift)
        self.assertIn("_cusum_narrow(src, i, j", src, "drift 가 좁히기를 불러야 한다")
        self.assertIn("view, ri, rj", src,
                      "좁힌 구간(ri, rj)이 이벤트로 나가야 한다 — i, j 를 그대로 쓰면 의미가 없다")


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


class PrevExclusionParking(unittest.TestCase):
    """제외한 항목이 다음 근무에 그대로 다시 올라오던 것 (#30).

    같은 것을 5근무 연속 제외해도 6번째에 또 떴다. 경모님 결정(2026-08-30)은
    **최하단 노출만** — 승격 조건도 만료 시간도 넣지 않는다. 숨기는 것이 아니라
    자리를 옮기는 것이라서, 항목이 사라지지 않는지까지 확인한다.
    """

    def _shift(self, conn, sid, start, end):
        import db
        conn.execute(
            "INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) "
            "VALUES (?,?,?,?,'test',?)", (sid, sid.rsplit("-", 1)[1], start, end, db.now()))

    def _excluded(self, conn, sid, tag="TI-403", kind="드리프트", confirm=True, comment=None):
        """그 근무에서 태그·종류 하나를 제외한 채 확정한다."""
        import db
        cur = conn.execute(
            "INSERT INTO event (shift_id, tag, kind, severity, detector, created_at) "
            "VALUES (?,?,?,'중','test',?)", (sid, tag, kind, db.now()))
        eid = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO draft (shift_id, status, generator, generated_at) VALUES (?,?,'test',?)",
            (sid, "confirmed" if confirm else "pending", db.now()))
        did = cur.lastrowid
        conn.execute(
            "INSERT INTO draft_item (draft_id, event_id, seq, origin, tag, title, body, "
            "severity, adopted, comment) VALUES (?,?,1,'detected',?,?,'본문','중',0,?)",
            (did, eid, tag, tag + " 드리프트", comment))
        if confirm:
            conn.execute(
                "INSERT INTO handover (shift_id, draft_id, confirmed_by, confirmed_at, "
                "adopted_count, excluded_count, body) VALUES (?,?,?,?,0,1,'')",
                (sid, did, "운전원A", "2026-08-25T18:05:00"))

    def test_confirmed_exclusion_is_found(self):
        _fresh_db()
        import db
        with db.connect() as conn:
            self._shift(conn, "2026-08-25-day", "2026-08-25T06:00:00", "2026-08-25T18:00:00")
            self._shift(conn, "2026-08-25-night", "2026-08-25T18:00:00", "2026-08-26T06:00:00")
            self._excluded(conn, "2026-08-25-day")
            got = db.recent_exclusions(conn, "2026-08-25-night")
        self.assertIn(("TI-403", "드리프트"), got, "지난 근무의 제외를 찾아야 한다")
        self.assertEqual(got[("TI-403", "드리프트")]["by"], "운전원A")

    def test_unconfirmed_draft_is_not_counted(self):
        """확정되지 않은 초안의 제외는 결정이 아니다 — 아직 아무도 승인하지 않았다."""
        _fresh_db()
        import db
        with db.connect() as conn:
            self._shift(conn, "2026-08-25-day", "2026-08-25T06:00:00", "2026-08-25T18:00:00")
            self._shift(conn, "2026-08-25-night", "2026-08-25T18:00:00", "2026-08-26T06:00:00")
            self._excluded(conn, "2026-08-25-day", confirm=False)
            got = db.recent_exclusions(conn, "2026-08-25-night")
        self.assertEqual(got, {}, "확정 전 초안의 제외는 세면 안 된다")

    def test_later_shift_does_not_leak_backwards(self):
        """뒤 근무의 판단이 앞 근무의 초안에 영향을 주면 안 된다."""
        _fresh_db()
        import db
        with db.connect() as conn:
            self._shift(conn, "2026-08-25-day", "2026-08-25T06:00:00", "2026-08-25T18:00:00")
            self._shift(conn, "2026-08-25-night", "2026-08-25T18:00:00", "2026-08-26T06:00:00")
            self._excluded(conn, "2026-08-25-night")
            got = db.recent_exclusions(conn, "2026-08-25-day")
        self.assertEqual(got, {})

    def test_repeat_count(self):
        _fresh_db()
        import db
        with db.connect() as conn:
            self._shift(conn, "2026-08-24-day", "2026-08-24T06:00:00", "2026-08-24T18:00:00")
            self._shift(conn, "2026-08-24-night", "2026-08-24T18:00:00", "2026-08-25T06:00:00")
            self._shift(conn, "2026-08-25-day", "2026-08-25T06:00:00", "2026-08-25T18:00:00")
            self._excluded(conn, "2026-08-24-day")
            self._excluded(conn, "2026-08-24-night")
            got = db.recent_exclusions(conn, "2026-08-25-day")
        self.assertEqual(got[("TI-403", "드리프트")]["times"], 2, "연속 제외 횟수를 센다")
        self.assertEqual(got[("TI-403", "드리프트")]["shift_id"], "2026-08-24-night",
                         "가장 최근 제외를 보여야 한다")

    # --- 화면 ---------------------------------------------------------

    def _pending(self, prev_excluded_json, adopted="NULL", sev="중"):
        """항목 두 개짜리 초안. 두 번째만 이전 제외 표시가 붙는다."""
        _fresh_db()
        import db
        sid = "2026-08-26-day"
        with db.connect() as conn:
            self._shift(conn, sid, "2026-08-26T06:00:00", "2026-08-26T18:00:00")
            cur = conn.execute(
                "INSERT INTO draft (shift_id, status, generator, generated_at) "
                "VALUES (?, 'pending','test',?)", (sid, db.now()))
            did = cur.lastrowid
            rows = [("PI-604", "보통 항목", None), ("TI-403", "내려갈 항목", prev_excluded_json)]
            for seq, (tag, title, pj) in enumerate(rows, 1):
                conn.execute(
                    "INSERT INTO draft_item (draft_id, event_id, seq, origin, tag, title, body, "
                    "evidence, severity, prev_excluded_json, adopted) "
                    "VALUES (?, NULL, ?, 'detected', ?, ?, '본문', '근거', ?, ?, " + adopted + ")",
                    (did, seq, tag, title, sev if pj else "중", pj))
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM draft_item WHERE draft_id = ? ORDER BY seq", (did,))]
            draft = db.load_draft(conn, sid)
        import server
        return server._view_pending(sid, draft), ids

    def test_screen_parks_at_bottom_and_does_not_hide(self):
        import json
        pj = json.dumps({"shift_id": "2026-08-25-day", "by": "운전원A",
                         "at": "2026-08-25T18:05:00", "times": 3, "comment": None},
                        ensure_ascii=False)
        page, ids = self._pending(pj)
        box = "이전에 제외한 것 —"   # 최하단 칸 제목. 머리글의 안내 문구와 구별한다
        self.assertIn(box, page, "최하단 칸이 있어야 한다")
        self.assertIn("내려갈 항목", page, "숨기면 안 된다 — 항목은 그대로 남는다")
        self.assertLess(page.index("보통 항목"), page.index(box),
                        "보통 항목이 먼저, 제외했던 것이 최하단")
        self.assertLess(page.index(box), page.index("내려갈 항목"),
                        "내려간 항목은 최하단 칸 안에 들어가야 한다")
        self.assertIn("3근무 연속", page, "반복 제외 횟수를 보인다")
        self.assertIn("운전원A", page, "누가 제외했는지 밝힌다")

    def test_parked_item_defaults_unchecked(self):
        import json
        pj = json.dumps({"shift_id": "2026-08-25-day", "by": "운전원A", "times": 1},
                        ensure_ascii=False)
        page, ids = self._pending(pj)
        self.assertIn('value="%d" onchange=' % ids[1], page,
                      "미결정 + 이전 제외면 앞 근무자의 결정을 이어받아 꺼져 있어야 한다")
        self.assertIn('value="%d" checked onchange=' % ids[0], page,
                      "보통 항목은 그대로 켜져 있어야 한다")
        self.assertIn('채택 <b id="n">1</b> / <span>2</span>건', page,
                      "채택 카운터가 켜진 개수와 맞아야 한다")

    def test_worker_decision_beats_previous_shift(self):
        """근무자가 이번에 채택했으면 그 값이 우선한다 — 지난 결정이 덮으면 안 된다."""
        import json
        pj = json.dumps({"shift_id": "2026-08-25-day", "by": "운전원A", "times": 1},
                        ensure_ascii=False)
        page, ids = self._pending(pj, adopted="1")
        self.assertIn('value="%d" checked onchange=' % ids[1], page)

    def test_high_severity_is_named_in_the_folded_summary(self):
        """접힌 채로도 무거운 것이 들었는지는 보여야 한다.

        실측: 근무 A 에서 제외한 MI-804 HH 초과(중요도 상)가 근무 B 에서 최하단으로
        내려갔다. 「중요도 상이면 본문으로 올린다」 는 판정 규칙이라 넣지 않기로 했으므로
        (#30 결정), 자리는 그대로 두고 제목에 밝히는 것으로 대신한다.
        """
        import json
        pj = json.dumps({"shift_id": "2026-08-25-day", "by": "운전원A", "times": 1},
                        ensure_ascii=False)
        page, ids = self._pending(pj, sev="상")
        self.assertIn("중요도 상 1건 포함", page, "접힌 제목에 중요도를 밝혀야 한다")
        # 그래도 자리는 최하단이다 — 승격 규칙을 넣은 것이 아니다
        self.assertLess(page.index("이전에 제외한 것 —"), page.index("내려갈 항목"))

    def test_ai_is_not_told_about_previous_exclusion(self):
        """AI 가 이전 제외를 알면 "전에 제외됐으니 괜찮다" 로 판정을 접는다(침묵 사고).

        표시는 반드시 llm.rewrite 뒤에 붙어야 한다.
        """
        _fresh_db()
        import pipeline
        src = inspect.getsource(pipeline)
        self.assertIn("recent_exclusions", src, "표시 단계가 있어야 한다")
        self.assertLess(src.index("llm.rewrite("), src.index("db.recent_exclusions("),
                        "이전 제외 표시는 AI 호출 뒤에 와야 한다")
        self.assertLess(src.index("db.recent_exclusions("), src.index("db.save_draft("),
                        "표시한 뒤에 저장해야 한다")
        import llm
        self.assertNotIn("prev_excluded", inspect.getsource(llm),
                         "프롬프트·스키마에 이전 제외가 들어가면 안 된다")


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
