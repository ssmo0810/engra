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
    # server 도 갈아야 한다 — 남겨 두면 server.db 가 이전 시험의 DB 파일을 계속 가리켜 화면 시험이 빈 DB 를 본다 (상태 스위치 시험에서 실측)
    for m in ("db", "collect", "pipeline", "approve", "jobs", "ports", "config", "llm", "server"):
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
        dec = dict((i, {"adopted": True, "comment": None, "status": "완료"}) for i in self.item_ids)
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
        # 묶음 합치기는 2026-09-14 항목별 병렬화로 rewrite → _merge 로 옮겼다. 걸러내는 자리는 그대로다.
        src = inspect.getsource(llm._merge)
        self.assertIn('whole[j].get("tag")', src,
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

    def test_old_database_gets_the_new_column(self):
        """라이브 seed.db 는 새 컬럼이 없는 202MB 짜리다. 재시작만으로 붙어야 한다.

        붙지 않으면 초안 저장이 "no such column" 으로 죽는다 — 배포가 통째로 멈춘다.
        """
        _fresh_db()
        import db
        with db.connect() as conn:
            conn.execute("ALTER TABLE draft_item DROP COLUMN prev_excluded_json")
            self.assertNotIn("prev_excluded_json",
                             {r[1] for r in conn.execute("PRAGMA table_info(draft_item)")})
        db.init()                      # 서버 기동·cli 가 하는 것과 같은 호출
        with db.connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(draft_item)")}
        self.assertIn("prev_excluded_json", cols, "재시작 한 번으로 컬럼이 붙어야 한다")

    def test_scheduled_run_migrates_by_itself(self):
        """교대 타이머(`cli.py run --latest`)는 무인으로 돈다. 혼자 서야 한다."""
        import cli
        src = inspect.getsource(cli.cmd_run)
        self.assertIn("db.init()", src,
                      "배포와 서버 재시작 사이에 타이머가 먼저 돌면 새 컬럼 없이 저장한다")

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


class StatusSwitchAndCarry(unittest.TestCase):
    """채택 항목의 완료/진행중 강제와 「진행중」 의 이월 (본선 심사평 08).

    확인·조치·미완료가 자유 코멘트에 묻혀 다음 근무로 이어지지 않는다는 지적. 채택하면 둘 중
    하나를 골라야 하고(기본값 없음), 진행중은 완료로 닫힐 때까지 다음 근무 초안 맨 위에 남는다.
    """

    DAY = ("2026-08-25-day", "2026-08-25T06:00:00", "2026-08-25T18:00:00")
    NIGHT = ("2026-08-25-night", "2026-08-25T18:00:00", "2026-08-26T06:00:00")
    NEXT = ("2026-08-26-day", "2026-08-26T06:00:00", "2026-08-26T18:00:00")

    def _shift(self, conn, sid, start, end):
        import db
        conn.execute(
            "INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) "
            "VALUES (?,?,?,?,'test',?)", (sid, sid.rsplit("-", 1)[1], start, end, db.now()))

    def _draft(self, conn, sid, titles=("항목 1",)):
        """항목 n 개짜리 대기 초안. 항목 id 목록을 돌려준다."""
        import db
        cur = conn.execute(
            "INSERT INTO draft (shift_id, status, generator, generated_at) VALUES (?,'pending','test',?)",
            (sid, db.now()))
        did = cur.lastrowid
        for seq, t in enumerate(titles, 1):
            conn.execute(
                "INSERT INTO draft_item (draft_id, event_id, seq, origin, tag, title, body, severity) "
                "VALUES (?, NULL, ?, 'detected', 'TI-403', ?, '본문', '중')", (did, seq, t))
        return [r[0] for r in conn.execute("SELECT id FROM draft_item WHERE draft_id = ? ORDER BY seq", (did,))]

    def _three_shifts(self):
        _fresh_db()
        import db
        with db.connect() as conn:
            for s in (self.DAY, self.NIGHT, self.NEXT):
                self._shift(conn, *s)
            ids = self._draft(conn, self.DAY[0], ("헌팅 확인", "밸브 점검"))
        return ids

    def test_old_database_gets_status_columns(self):
        """라이브 DB 에 status·carried_from 이 없다. 재시작(db.init) 한 번으로 붙고 기존 행은 그대로여야 한다."""
        _fresh_db()
        import db
        with db.connect() as conn:
            self._shift(conn, *self.DAY)
            ids = self._draft(conn, self.DAY[0], ("옛 항목",))
            conn.execute("ALTER TABLE draft_item DROP COLUMN status")
            conn.execute("ALTER TABLE draft_item DROP COLUMN carried_from")
            self.assertNotIn("status", {r[1] for r in conn.execute("PRAGMA table_info(draft_item)")})
        db.init()
        with db.connect() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(draft_item)")}
            row = conn.execute("SELECT title, status, carried_from FROM draft_item WHERE id = ?", (ids[0],)).fetchone()
        self.assertIn("status", cols)
        self.assertIn("carried_from", cols)
        self.assertEqual(row["title"], "옛 항목", "기존 행이 보존돼야 한다")
        self.assertIsNone(row["status"])

    def test_adopted_without_status_is_rejected_and_nothing_saved(self):
        """상태 없는 채택은 승인되지 않는다 — 어느 항목인지 짚고, 아무것도 저장되지 않아야 한다."""
        ids = self._three_shifts()
        import approve, db
        dec = {ids[0]: {"adopted": True, "status": "완료"},
               ids[1]: {"adopted": True}}                       # 두 번째만 상태가 빈다
        with self.assertRaises(approve.StatusMissing) as cm:
            approve.decide(self.DAY[0], dec, confirmed_by="운전원A")
        self.assertEqual([i for i, _ in cm.exception.items], [ids[1]], "빈 항목만 짚어야 한다")
        self.assertIn("밸브 점검", str(cm.exception))
        with db.connect() as conn:
            self.assertIsNone(db.load_handover(conn, self.DAY[0]), "확정되면 안 된다")
            st = conn.execute("SELECT adopted, status FROM draft_item WHERE id = ?", (ids[0],)).fetchone()
        self.assertIsNone(st["adopted"], "실패한 승인의 UPDATE 는 되돌아가야 한다")

    def test_unknown_status_value_is_rejected(self):
        ids = self._three_shifts()
        import approve
        with self.assertRaises(ValueError):
            approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "대충"}})

    def test_excluded_item_needs_no_status(self):
        ids = self._three_shifts()
        import approve, db
        r = approve.decide(self.DAY[0], {ids[0]: {"adopted": False}, ids[1]: {"adopted": False}})
        self.assertEqual(r["adopted"], 0)
        with db.connect() as conn:
            self.assertIsNotNone(db.load_handover(conn, self.DAY[0]))

    def test_in_progress_shows_up_next_shift_until_closed(self):
        """진행중 → 다음 근무 open_items 에 뜬다. 다음 근무가 완료로 닫으면 그 뒤 근무에는 없다."""
        ids = self._three_shifts()
        import approve, db
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중", "comment": "야간에 확인 요망"},
                                     ids[1]: {"adopted": True, "status": "완료"}}, confirmed_by="운전원A")
        with db.connect() as conn:
            opened = db.open_items(conn, self.NIGHT[0])
        self.assertEqual([o["id"] for o in opened], [ids[0]], "진행중 항목만 열려 있어야 한다")
        o = opened[0]
        self.assertEqual(o["shift_id"], self.DAY[0])
        self.assertEqual(o["shifts_ago"], 1)
        self.assertEqual(o["comment"], "야간에 확인 요망")
        self.assertEqual(o["confirmed_by"], "운전원A")

        # 야간 근무: 계속 진행중으로 넘긴다 → 그 다음 근무에도 열려 있고, 코멘트는 야간 것이 최신
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}},
                       carried={ids[0]: {"status": "진행중", "comment": "밸브 교체 대기"}}, confirmed_by="운전원B")
        with db.connect() as conn:
            opened = db.open_items(conn, self.NEXT[0])
            body = db.load_handover(conn, self.NIGHT[0])["body"]
        self.assertEqual([o["id"] for o in opened], [ids[0]])
        self.assertEqual(opened[0]["shifts_ago"], 2)
        self.assertEqual(opened[0]["comment"], "밸브 교체 대기")
        self.assertEqual(opened[0]["last_shift_id"], self.NIGHT[0])
        self.assertIn("이월 항목 1건", body)
        self.assertIn("계속 진행중", body)

        # 다음 주간: 완료로 닫는다 → 그 뒤엔 없다
        with db.connect() as conn:
            self._shift(conn, "2026-08-26-night", "2026-08-26T18:00:00", "2026-08-27T06:00:00")
            xids = self._draft(conn, self.NEXT[0], ("주간 항목",))
        approve.decide(self.NEXT[0], {xids[0]: {"adopted": False}},
                       carried={ids[0]: {"status": "완료"}}, confirmed_by="운전원C")
        with db.connect() as conn:
            self.assertEqual(db.open_items(conn, "2026-08-26-night"), [], "완료로 닫혔으면 사라져야 한다")
            # 닫은 근무 자신에게는 여전히 「그 근무 앞에 열려 있던 것」 으로 보인다(확정 화면이 판단을 보이는 자리)
            self.assertEqual([o["id"] for o in db.open_items(conn, self.NEXT[0])], [ids[0]])

    def test_unconfirmed_close_does_not_close(self):
        """확정되지 않은 초안의 완료 표시는 닫힘이 아니다 — 재검토로 되돌리면 다시 열린다."""
        ids = self._three_shifts()
        import approve, db
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "완료"}})
        with db.connect() as conn:
            self.assertEqual(db.open_items(conn, self.NEXT[0]), [])
            db.reopen_handover(conn, self.NIGHT[0], "실수")
            self.assertEqual([o["id"] for o in db.open_items(conn, self.NEXT[0])], [ids[0]],
                             "닫은 근무가 재검토로 돌아가면 닫힘도 풀려야 한다")

    def test_reapprove_does_not_duplicate_carried_rows(self):
        """재검토 → 재승인이 같은 원 항목을 두 번 가리키면 안 된다."""
        ids = self._three_shifts()
        import approve, db
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
        for _ in (1, 2):
            approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "진행중"}})
        with db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM draft_item WHERE origin='carried' AND carried_from = ?", (ids[0],)).fetchone()[0]
        self.assertEqual(n, 1)

    def test_carried_choice_must_point_at_an_open_item(self):
        ids = self._three_shifts()
        import approve, db
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
        with self.assertRaises(ValueError) as cm:
            approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "완료"}})
        self.assertIn("열려 있는 이월 항목이 아닙니다", str(cm.exception))

    # --- 화면 ---------------------------------------------------------

    def test_pending_screen_has_radios_without_default_and_carry_box(self):
        ids = self._three_shifts()
        import approve, db, server
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
            draft = db.load_draft(conn, self.NIGHT[0])
        page = server._view_pending(self.NIGHT[0], draft)
        self.assertIn(f'name="status_{nids[0]}" value="완료"', page)
        self.assertIn(f'name="status_{nids[0]}" value="진행중"', page)
        self.assertNotIn(f'name="status_{nids[0]}" value="완료" checked', page, "기본 선택이 있으면 안 된다")
        self.assertNotIn(f'name="status_{nids[0]}" value="진행중" checked', page, "기본 선택이 있으면 안 된다")
        self.assertIn("이월 항목 1건", page)
        self.assertIn(f'name="carry_{ids[0]}"', page)
        self.assertIn("헌팅 확인", page)
        self.assertLess(page.index("이월 항목 1건"), page.index("야간 항목"), "이월 묶음은 본문 항목 위, 따로")
        self.assertIn("<details", page[:page.index("이월 항목 1건")][-400:], "접힌 묶음이어야 한다")
        self.assertIn('onsubmit="return chk(this)"', page)

    def test_pending_screen_without_open_items_draws_no_box(self):
        ids = self._three_shifts()
        import db, server
        with db.connect() as conn:
            draft = db.load_draft(conn, self.DAY[0])
        page = server._view_pending(self.DAY[0], draft)
        self.assertNotIn('class="card carry"', page, "열린 것이 없으면 묶음을 그리지 않는다")
        self.assertNotIn("이월 항목 0건", page)

    def test_confirmed_screen_shows_status_and_carry_decision(self):
        ids = self._three_shifts()
        import approve, db, server
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": True, "status": "완료"}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": True, "status": "완료"}}, carried={ids[0]: {"status": "완료", "comment": "교체 끝"}})
        with db.connect() as conn:
            draft = db.load_draft(conn, self.NIGHT[0])
            h = db.load_handover(conn, self.NIGHT[0])
        page = server._view_confirmed(self.NIGHT[0], draft, h)
        self.assertIn("이월 항목 1건", page)
        self.assertIn("완료로 닫음", page)
        self.assertIn("교체 끝", page)
        self.assertIn('class="pill done">완료', page)
        self.assertIn("감지 1건 중", page, "이월 판단 행은 감지 건수에 섞이지 않는다")

    def test_server_rejects_form_without_status(self):
        """폼을 우회해도(라디오 없이 POST) 서버가 400 으로 막고 어느 항목인지 적는다."""
        ids = self._three_shifts()
        import server, db
        from urllib.parse import urlencode

        class Fake(server.Handler):
            def __init__(self):        # 소켓 없이 핸들러 본체만 쓴다
                self.path = "/approve"; self.sent = []
                self.headers = {}
            def _send(self, code, body, ctype="text/html; charset=utf-8"):
                self.sent.append((code, body))
            def send_response(self, code): self.sent.append((code, ""))
            def send_header(self, *a): pass
            def end_headers(self): pass

        h = Fake()
        raw = urlencode([("shift_id", self.DAY[0]), ("item", ids[0]), ("item", ids[1]),
                         (f"status_{ids[0]}", "완료")]).encode()
        h._handle_form(raw)
        code, body = h.sent[-1]
        self.assertEqual(code, 400)
        self.assertIn("밸브 점검", body, "빈 항목을 이름으로 짚어야 한다")
        with db.connect() as conn:
            self.assertIsNone(db.load_handover(conn, self.DAY[0]))
        # 위조된 상태 값도 400 — 라디오가 안 주는 값을 보낸 요청 잘못이다
        h = Fake()
        h._handle_form(urlencode([("shift_id", self.DAY[0]), ("item", ids[0]), (f"status_{ids[0]}", "대충")]).encode())
        self.assertEqual(h.sent[-1][0], 400)
        self.assertIn("완료/진행중", h.sent[-1][1])

    def test_server_accepts_form_with_status_and_carry(self):
        ids = self._three_shifts()
        import server, db, approve
        from urllib.parse import urlencode
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))

        class Fake(server.Handler):
            def __init__(self):
                self.path = "/approve"; self.sent = []; self.headers = {}
            def _send(self, code, body, ctype=""): self.sent.append((code, body))
            def send_response(self, code): self.sent.append((code, ""))
            def send_header(self, *a): pass
            def end_headers(self): pass

        h = Fake()
        raw = urlencode([("shift_id", self.NIGHT[0]), ("item", nids[0]), (f"status_{nids[0]}", "완료"),
                         (f"carry_{ids[0]}", "완료"), (f"carry_comment_{ids[0]}", "끝"),
                         ("manual_title", "직접 건"), ("manual_body", "내용"), ("manual_status", "진행중")]).encode()
        h._handle_form(raw)
        self.assertEqual(h.sent[-1][0], 303, h.sent[-1][1][:300])
        with db.connect() as conn:
            self.assertIsNotNone(db.load_handover(conn, self.NIGHT[0]))
            opened = db.open_items(conn, self.NEXT[0])
            man = conn.execute("SELECT id, status FROM draft_item WHERE origin='manual' AND title='직접 건'").fetchone()
        self.assertEqual(man["status"], "진행중")
        self.assertNotIn(ids[0], [o["id"] for o in opened], "폼으로 닫은 것도 닫혀야 한다")
        self.assertEqual([o["id"] for o in opened], [man["id"]], "진행중으로 직접 추가한 것은 다음 근무에 이월된다")

    def test_server_rejects_manual_without_status_before_saving(self):
        ids = self._three_shifts()
        import server, db
        from urllib.parse import urlencode

        class Fake(server.Handler):
            def __init__(self):
                self.path = "/approve"; self.sent = []; self.headers = {}
            def _send(self, code, body, ctype=""): self.sent.append((code, body))
            def send_response(self, code): self.sent.append((code, ""))
            def send_header(self, *a): pass
            def end_headers(self): pass

        h = Fake()
        raw = urlencode([("shift_id", self.DAY[0]), ("manual_title", "직접 건"), ("manual_body", ""), ("manual_status", "")]).encode()
        h._handle_form(raw)
        self.assertEqual(h.sent[-1][0], 400)
        with db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM draft_item WHERE origin='manual'").fetchone()[0]
        self.assertEqual(n, 0, "거부됐으면 수동 항목이 저장돼 남으면 안 된다")

    def _post(self, pairs):
        """소켓 없이 /approve 핸들러 본체만 부른다. 마지막 응답 (코드, 본문)."""
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

    def test_rejected_approval_leaves_no_manual_row(self):
        """직접 추가에 상태가 있어도 다른 채택 항목의 상태가 비어 승인이 거부되면 아무것도 남으면 안 된다.
        직접 추가를 승인 전에 따로 커밋해 상태 없는 수동 행이 초안에 남았다(실측)."""
        ids = self._three_shifts()
        import db
        code, body = self._post([("shift_id", self.DAY[0]), ("item", ids[0]),
                                 ("manual_title", "직접 건"), ("manual_body", "내용"), ("manual_status", "진행중")])
        self.assertEqual(code, 400)
        self.assertIn("헌팅 확인", body, "상태가 빈 감지 항목을 짚어야 한다")
        with db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM draft_item WHERE origin='manual'").fetchone()[0]
            self.assertIsNone(db.load_handover(conn, self.DAY[0]))
        self.assertEqual(n, 0, "거부된 승인의 직접 추가 항목이 남으면 안 된다")

    def test_decide_lists_every_missing_status_and_rolls_back_manual(self):
        ids = self._three_shifts()
        import approve, db
        with self.assertRaises(approve.StatusMissing) as cm:
            approve.decide(self.DAY[0], {ids[0]: {"adopted": True}},
                           manual=[{"title": "직접 건", "body": "", "status": None}])
        self.assertEqual(sorted(t for _, t in cm.exception.items), ["직접 건", "헌팅 확인"], "빈 항목을 한 번에 다 짚는다")
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM draft_item WHERE origin='manual'").fetchone()[0], 0)

    def test_manual_fields_stay_paired_when_a_body_is_blank(self):
        """parse_qs 기본값은 빈 값을 버린다. 앞 건의 내용이 비면 뒤 건의 내용이 앞 건으로 당겨졌다."""
        ids = self._three_shifts()
        import db
        code, body = self._post([("shift_id", self.DAY[0]),
                                 ("manual_title", "앞 건"), ("manual_status", "완료"), ("manual_body", ""),
                                 ("manual_title", "뒤 건"), ("manual_status", "진행중"), ("manual_body", "뒤 내용")])
        self.assertEqual(code, 303, body[:300])
        with db.connect() as conn:
            rows = {r["title"]: (r["body"], r["status"]) for r in conn.execute(
                "SELECT title, body, status FROM draft_item WHERE origin='manual'")}
        self.assertEqual(rows["앞 건"], ("", "완료"))
        self.assertEqual(rows["뒤 건"], ("뒤 내용", "진행중"))

    def test_forged_carry_key_is_400_not_500(self):
        ids = self._three_shifts()
        code, _ = self._post([("shift_id", self.DAY[0]), ("item", ids[0]), (f"status_{ids[0]}", "완료"),
                              ("carry_abc", "완료")])
        self.assertEqual(code, 400, "숫자가 아닌 이월 항목 번호는 요청 잘못이다")

    # --- 이월 계보가 뒤 근무 기록과 어긋나지 않게 (codex 반증 2라운드) ---------------

    def test_closing_under_a_later_confirmed_decision_is_refused(self):
        """순서를 거슬러 승인: 뒤 근무가 먼저 「계속 진행중」 으로 확정한 항목을 앞 근무가 뒤늦게 완료로 닫으면
        뒤 근무 기록은 「다음 근무로」 인데 그 다음 근무에는 안 뜬다. 조용히 어긋나게 두지 않고 거부한다."""
        ids = self._three_shifts()
        import approve, db
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
            xids = self._draft(conn, self.NEXT[0], ("주간 항목",))
        approve.decide(self.NEXT[0], {xids[0]: {"adopted": False}}, carried={ids[0]: {"status": "진행중"}})   # 뒤 근무를 먼저
        with self.assertRaises(ValueError) as cm:
            approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "완료"}})
        self.assertIn(self.NEXT[0], str(cm.exception), "어느 뒤 근무 때문인지 짚어야 한다")
        with db.connect() as conn:
            self.assertIsNone(db.load_handover(conn, self.NIGHT[0]), "거부됐으면 확정되면 안 된다")
        # 이어 가는 판단(진행중)은 뒤 근무 기록과 어긋나지 않는다
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "진행중"}})

    def test_reapproving_root_shift_cannot_drop_a_carried_item(self):
        """원 근무를 재검토 뒤 재승인하며 뒤 근무가 이어받은 항목을 완료·제외로 바꾸면 뒤 근무의 판단이
        확정 화면에서 사라지고 본문·색인에만 남는다. 뒤 근무를 먼저 되돌리라고 거부한다."""
        ids = self._three_shifts()
        import approve, db
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "진행중"}})
        with db.connect() as conn:
            db.reopen_handover(conn, self.DAY[0], "고침")
        for dec in ({"adopted": True, "status": "완료"}, {"adopted": False}):
            with self.assertRaises(ValueError) as cm:
                approve.decide(self.DAY[0], {ids[0]: dec, ids[1]: {"adopted": False}})
            self.assertIn(self.NIGHT[0], str(cm.exception))
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            self.assertEqual([o["id"] for o in db.open_items(conn, self.NEXT[0])], [ids[0]])

    def test_regenerating_items_a_later_shift_carried_is_refused(self):
        """원 근무 초안을 다시 만들면 항목 id 가 바뀌어 뒤 근무의 판단이 가리킬 곳을 잃고, 닫힌 항목이 다시 열렸다."""
        ids = self._three_shifts()
        import approve, db
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "완료"}})
        with db.connect() as conn:
            with self.assertRaises(ValueError) as cm:
                db.clear_outputs(conn, self.DAY[0], include_handover=True)
            self.assertIn(self.NIGHT[0], str(cm.exception))
            with self.assertRaises(ValueError):
                db.save_draft(conn, self.DAY[0], [], "test")
            self.assertIsNotNone(db.load_handover(conn, self.DAY[0]), "거부됐으면 지우면 안 된다")
            db.reopen_handover(conn, self.NIGHT[0], "다시 만들기 전")
            db.clear_outputs(conn, self.DAY[0], include_handover=True)     # 뒤 근무를 되돌리면 다시 만들 수 있다

    def test_carry_rows_do_not_crowd_precedent_search(self):
        """같은 원 항목을 여러 근무 「계속 진행중」 으로 넘기면 그 복제본이 태그 검색 상위(limit 3)를 독점해
        다른 과거 사례가 밀려났다. 색인에는 닫은 판단(완료)만 — 조치가 끝난 기록이 과거 조치가 된다."""
        ids = self._three_shifts()
        import approve, db
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
            xids = self._draft(conn, self.NEXT[0], ("주간 항목",))
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "진행중", "comment": "교체 대기"}})
        approve.decide(self.NEXT[0], {xids[0]: {"adopted": False}}, carried={ids[0]: {"status": "완료", "comment": "밸브 교체 끝"}})
        with db.connect() as conn:
            night = conn.execute("SELECT COUNT(*) FROM handover_fts WHERE shift_id = ?", (self.NIGHT[0],)).fetchone()[0]
            nxt = [r[0] for r in conn.execute("SELECT text FROM handover_fts WHERE shift_id = ?", (self.NEXT[0],))]
        self.assertEqual(night, 0, "계속 진행중 이월 행은 색인에 넣지 않는다")
        self.assertEqual(len(nxt), 1)
        self.assertIn("밸브 교체 끝", nxt[0], "닫은 판단의 코멘트가 과거 조치로 검색돼야 한다")

    # --- 명령줄·도구 — smoke·seed·snapshot 이 approve --all 로 승인한다 ---------------------

    def _cli(self, *args):
        import subprocess
        return subprocess.run([sys.executable, os.path.join(ROOT, "app", "cli.py"), *args],
                              capture_output=True, text=True, env=dict(os.environ))

    def test_cli_approve_needs_status_for_adopted_items(self):
        """approve --all 이 상태를 안 넣어 이 브랜치에서 항상 실패했다 — smoke·seed·snapshot 이 깨졌다(실측).
        --status 는 기본값이 없다. 빠지면 한 줄로 알리고 0 이 아닌 코드로 끝나야 한다."""
        ids = self._three_shifts()
        import db
        r = self._cli("approve", self.DAY[0], "--all")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--status", r.stdout)
        self.assertEqual(len(r.stdout.strip().splitlines()), 1, r.stdout)
        with db.connect() as conn:
            self.assertIsNone(db.load_handover(conn, self.DAY[0]))
        r = self._cli("approve", self.DAY[0], "--all", "--status", "완료")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with db.connect() as conn:
            self.assertIsNotNone(db.load_handover(conn, self.DAY[0]))
            st = [row[0] for row in conn.execute("SELECT status FROM draft_item WHERE id IN (?, ?) ORDER BY id", ids)]
        self.assertEqual(st, ["완료", "완료"])

    def test_cli_reapprove_skips_carried_rows_and_keeps_carry_decisions(self):
        """재검토한 근무를 명령줄로 재승인하면 이월 행 id 가 decisions 에 섞여 「이 초안에 없는 항목」 으로 멈췄다.
        명령줄은 이월 판단을 고르지 못하니 화면에서 고른 판단을 그대로 둔다 — 지우면 닫힌 항목이 조용히 다시 열린다."""
        ids = self._three_shifts()
        import approve, db
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "완료", "comment": "교체 끝"}})
        with db.connect() as conn:
            db.reopen_handover(conn, self.NIGHT[0], "명령줄로 다시")
        r = self._cli("approve", self.NIGHT[0], "--all", "--status", "완료")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        with db.connect() as conn:
            kept = db.carried_choices(conn, db.load_draft(conn, self.NIGHT[0])["id"])
            self.assertEqual(db.open_items(conn, self.NEXT[0]), [], "화면에서 닫은 판단이 명령줄 재승인으로 풀리면 안 된다")
        self.assertEqual(kept[ids[0]]["status"], "완료")
        self.assertEqual(kept[ids[0]]["comment"], "교체 끝")

    def _snapshot_tool(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("snapshot_tool", os.path.join(ROOT, "tools", "snapshot.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_snapshot_neutralizes_the_approve_form(self):
        """승인 폼 태그에 onsubmit 이 붙자 snapshot.py 의 문자열 치환이 조용히 빗나가 정적 페이지에
        /approve 로 가는 폼이 남았다. 새 태그를 치환하고, 빗나가면 멈춰야 한다."""
        ids = self._three_shifts()
        import db, server
        with db.connect() as conn:
            draft = db.load_draft(conn, self.DAY[0])
        snap = self._snapshot_tool()
        out = snap.to_static(server._view_pending(self.DAY[0], draft), "draft.html")
        self.assertNotIn('action="/approve"', out)
        self.assertIn('<form onsubmit="return false">', out)
        with self.assertRaises(SystemExit):     # 치환이 못 잡는 POST 제출이 남으면 멈춘다
            snap.to_static('<div class="top"></div><div class="card"><button formmethod="post" formaction="/reopen">x'
                           '</button></div>', "draft.html")

    def test_snapshot_neutralizes_every_post_form(self):
        """확정 화면의 재검토 폼(POST /reopen)이 정적 handover.html 에 살아 남았다 — 승인 폼만 치환·검사했다(반증 워커)."""
        ids = self._three_shifts()
        import approve, db, server
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "완료"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            draft = db.load_draft(conn, self.DAY[0])
            h = db.load_handover(conn, self.DAY[0])
        out = self._snapshot_tool().to_static(server._view_confirmed(self.DAY[0], draft, h), "handover.html")
        self.assertNotIn('method="post"', out)

    def test_snapshot_neutralizes_post_form_spelling_variants(self):
        """치환·검사가 소문자·큰따옴표 method="post" 만 봐서, 대소문자·따옴표·공백 표기가 다른 POST 폼은 SystemExit 없이
        정적 페이지에 남았다(반증 워커 실측 — 지금 server.py 폼은 전부 method="post" 라 잠재 결함)."""
        import re
        snap = self._snapshot_tool()
        for attr in ('method="POST"', 'METHOD="post"', "method='post'", "method=post", 'method = "post"'):
            with self.subTest(attr=attr):
                out = snap.to_static(f'<div class="top"></div><div class="card"><form {attr} action="/reopen">'
                                     '</form></div>', "handover.html")
                self.assertIn('<form onsubmit="return false">', out)
                self.assertIsNone(re.search(r'method\s*=\s*["\']?post\b', out, re.I), out)

    def test_reapproving_during_root_shift_review_keeps_carry_decision(self):
        """원 근무를 재검토로 되돌린 사이 뒤 근무를 재승인하면 원 항목이 open_items 에서 빠져, save_carried 가
        「완료로 닫음」 을 조용히 지웠다 — 원 근무를 재확정하면 닫힌 항목이 다시 열렸다(반증 워커 실측).
        명령줄·화면 모두 거부하고 이월 행을 남긴다."""
        ids = self._three_shifts()
        import approve, db
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            nids = self._draft(conn, self.NIGHT[0], ("야간 항목",))
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "완료", "comment": "교체 끝"}})
        with db.connect() as conn:
            db.reopen_handover(conn, self.DAY[0], "원 근무 고침")
        r = self._cli("approve", self.NIGHT[0], "--all", "--status", "완료")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn(self.DAY[0], r.stdout)
        code, body = self._post([("shift_id", self.NIGHT[0]), ("item", nids[0]), (f"status_{nids[0]}", "완료")])
        self.assertEqual(code, 400)
        self.assertIn(self.DAY[0], body)
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "진행중"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            kept = db.carried_choices(conn, db.load_draft(conn, self.NIGHT[0])["id"])
            self.assertEqual(db.open_items(conn, self.NEXT[0]), [], "원 근무를 재확정해도 뒤 근무가 닫은 항목은 닫혀 있어야 한다")
        self.assertEqual(kept.get(ids[0], {}).get("status"), "완료")

    # --- 머지 전 마무리 — 기존 기록의 상태 표시 · 요청 잘못은 400 (팀장 결정 2026-09-14) -------------

    def test_legacy_null_status_draws_no_pill(self):
        """기능 도입 전에 확정된 기록은 상태가 NULL 이다. 「상태 없음」 알약을 붙이면 데모 사이트의 확정 근무가
        결함처럼 보인다 — 완료/진행중이 있는 항목에만 알약을 붙인다."""
        ids = self._three_shifts()
        import approve, db, server
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "완료"}, ids[1]: {"adopted": True, "status": "완료"}})
        with db.connect() as conn:
            conn.execute("UPDATE draft_item SET status = NULL WHERE id = ?", (ids[1],))   # 기능 도입 전에 확정된 행
            draft = db.load_draft(conn, self.DAY[0])
            h = db.load_handover(conn, self.DAY[0])
        page = server._view_confirmed(self.DAY[0], draft, h)
        self.assertNotIn("상태 없음", page)
        self.assertEqual(page.count('class="pill done">완료'), 1, "상태가 있는 항목에만 알약")

    def test_forged_item_number_is_400_not_500(self):
        ids = self._three_shifts()
        code, _ = self._post([("shift_id", self.DAY[0]), ("item", "abc")])
        self.assertEqual(code, 400, "숫자가 아닌 항목 번호는 요청 잘못이다")

    def test_approving_a_shift_without_draft_is_400_not_500(self):
        ids = self._three_shifts()
        code, body = self._post([("shift_id", "2026-01-01-day"), ("item", ids[0]), (f"status_{ids[0]}", "완료")])
        self.assertEqual(code, 400)
        self.assertIn("초안이 없습니다", body)


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
