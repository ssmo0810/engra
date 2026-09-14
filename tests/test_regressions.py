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
    for m in ("db", "collect", "pipeline", "approve", "jobs", "ports", "config", "llm", "server", "live"):
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
        src = inspect.getsource(server._item_card)      # 카드 한 장을 그리는 자리(초안 화면과 실시간 폴링이 함께 쓴다)
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
        self.assertIn('채택 <b id="n">1</b> / <span id="tot">2</span>건', page,
                      "채택 카운터가 켜진 개수와 맞아야 한다")

    def test_worker_decision_beats_previous_shift(self):
        """근무자가 이번에 채택했으면 그 값이 우선한다 — 지난 결정이 덮으면 안 된다."""
        import json
        pj = json.dumps({"shift_id": "2026-08-25-day", "by": "운전원A", "times": 1},
                        ensure_ascii=False)
        page, ids = self._pending(pj, adopted="1")
        self.assertIn('value="%d" checked onchange=' % ids[1], page)

    def test_previously_excluded_stays_at_bottom_without_severity(self):
        """지난 근무에서 제외한 항목은 최하단 접힌 묶음에 둔다(#30 결정 — 승격 규칙은 넣지 않는다).

        예전에는 접힌 제목에 「중요도 상 N건 포함」 을 밝혔는데, 경모님이 중요도를 화면에서 없애기로 해
        (2026-09-14) 그 표시도 뺐다. 자리는 그대로 최하단이다.
        """
        import json
        pj = json.dumps({"shift_id": "2026-08-25-day", "by": "운전원A", "times": 1},
                        ensure_ascii=False)
        page, ids = self._pending(pj, sev="상")
        self.assertNotIn("중요도", page, "중요도는 화면에서 뺐다 — 접힌 제목에도 밝히지 않는다")
        # 자리는 최하단이다 — 승격 규칙을 넣은 것이 아니다
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
        # 앞 근무가 대기인 채 뒤 근무를 먼저 승인하는 길은 승인 순서 규칙이 막는다. 같은 상태(뒤 근무 확정 · 앞 근무 대기)는
        # 순서대로 확정한 뒤 앞 근무를 재검토로 되돌리면 여전히 생긴다 — 그 길로 만든다.
        approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "진행중"}})
        approve.decide(self.NEXT[0], {xids[0]: {"adopted": False}}, carried={ids[0]: {"status": "진행중"}})
        with db.connect() as conn:
            db.reopen_handover(conn, self.NIGHT[0], "뒤 근무 확정 뒤 되돌림")
        with self.assertRaises(ValueError) as cm:
            approve.decide(self.NIGHT[0], {nids[0]: {"adopted": False}}, carried={ids[0]: {"status": "완료"}})
        self.assertIn(self.NEXT[0], str(cm.exception), "어느 뒤 근무 때문인지 짚어야 한다")
        self.assertIn("이어받아 판단했습니다", str(cm.exception), "앞 근무 DAY 는 확정 — 순서 규칙이 아니라 이월 계보 가드가 거부해야 한다")
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
        # 상세는 한 화면의 오른쪽 조각이라 page() 로 감싸야 실제로 뜨는 HTML 이 된다
        out = snap.to_static(server.page("근무 일지", server._view_pending(self.DAY[0], draft)), "index.html")
        self.assertNotIn('action="/approve"', out)
        self.assertIn('<form onsubmit="return false">', out)
        with self.assertRaises(SystemExit):     # 치환이 못 잡는 POST 제출이 남으면 멈춘다
            snap.to_static('<main class="wrap"><div class="card"><button formmethod="post" formaction="/reopen">x'
                           '</button></div>', "index.html")

    def test_snapshot_neutralizes_every_post_form(self):
        """확정 화면의 재검토 폼(POST /reopen)이 정적 handover.html 에 살아 남았다 — 승인 폼만 치환·검사했다(반증 워커)."""
        ids = self._three_shifts()
        import approve, db, server
        approve.decide(self.DAY[0], {ids[0]: {"adopted": True, "status": "완료"}, ids[1]: {"adopted": False}})
        with db.connect() as conn:
            draft = db.load_draft(conn, self.DAY[0])
            h = db.load_handover(conn, self.DAY[0])
        out = self._snapshot_tool().to_static(
            server.page("근무 일지", server._view_confirmed(self.DAY[0], draft, h)), "handover.html")
        self.assertNotIn('method="post"', out)

    def test_snapshot_neutralizes_post_form_spelling_variants(self):
        """치환·검사가 소문자·큰따옴표 method="post" 만 봐서, 대소문자·따옴표·공백 표기가 다른 POST 폼은 SystemExit 없이
        정적 페이지에 남았다(반증 워커 실측 — 지금 server.py 폼은 전부 method="post" 라 잠재 결함)."""
        import re
        snap = self._snapshot_tool()
        for attr in ('method="POST"', 'METHOD="post"', "method='post'", "method=post", 'method = "post"'):
            with self.subTest(attr=attr):
                out = snap.to_static(f'<main class="wrap"><div class="card"><form {attr} action="/reopen">'
                                     '</form></div>', "handover.html")
                self.assertIn('<form onsubmit="return false">', out)
                self.assertIsNone(re.search(r'method\s*=\s*["\']?post\b', out, re.I), out)

    def test_snapshot_banner_lands_in_the_new_page_shell(self):
        """안내 배너가 index.html 에서 조용히 빠졌다(반증 워커 실측 — 삽입 False). 옛 page() 의 이동줄
        `<div class="nav">…</div>` 을 앵커로 잡고 있었는데 새 page() 는 `<header class="top">…</header>
        <main class="wrap">` 라 본문 앞에 `</div>` 가 없다. re.sub 는 매치가 0 이어도 예외를 내지 않아
        빗나간 것이 안 보였다 — 앵커를 `<main class="wrap">` 으로 옮기고 못 넣으면 멈춘다."""
        import server
        snap = self._snapshot_tool()
        for name in snap.PAGES:
            with self.subTest(name=name):
                out = snap.to_static(server.page("일지 목록", '<div class="card">본문</div>'), name)
                self.assertTrue("실제로 돌아가는 프로토타입 화면입니다" in out, f"{name}: 안내 배너가 빠졌다")
                self.assertTrue(snap.NOTES[name] in out, f"{name}: 화면별 설명이 빠졌다")
                self.assertLess(out.index("실제로 돌아가는"), out.index('<div class="card">본문'),
                                f"{name}: 배너는 본문 앞에 온다")
        with self.assertRaises(SystemExit):     # 구조가 또 바뀌어 앵커를 못 찾으면 조용히 빠지지 말고 멈춘다
            snap.to_static('<div class="card">앵커 없음</div>', "index.html")

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


class TwoScreens(unittest.TestCase):
    """결선 화면 재구성 — DCS(개요 전체 화면) · 초안(근무 목록 → 상세) 두 화면, 관리는 주소를 아는 사람만.

    결선 심사위원들이 「DCS → RTDB → 파이프라인 → 초안 → 일지」 다섯 화면을 따라가기 힘들어했다.
    옛 화면이 되살아나거나 공개 화면에 관리 링크가 새면 여기서 걸린다.
    """

    longMessage = False   # 실패하면 화면 전체 대신 이유 한 줄만
    DCS_ON = ":has(.hmi #viewOverview):has(.wrap>.disc)"   # /dcs 전체 화면 규칙이 켜지는 조건 — 원본 구조가 CSS 가 기대는 대로일 때만

    DAY = ("2026-08-25-day", "2026-08-25T06:00:00", "2026-08-25T18:00:00")
    NIGHT = ("2026-08-25-night", "2026-08-25T18:00:00", "2026-08-26T06:00:00")

    def _get(self, path):
        """소켓 없이 GET 핸들러 본체만 부른다. (코드, 본문, 보낸 헤더)."""
        import server

        class Fake(server.Handler):
            def __init__(self):
                self.path = path; self.sent = []; self.headers = {}; self.out = {}
            def _send(self, code, body, ctype=""): self.sent.append((code, body))
            def send_response(self, code): self.sent.append((code, ""))
            def send_header(self, k, v): self.out[k] = v
            def end_headers(self): pass

        h = Fake()
        h.do_GET()
        return h.sent[-1] + (h.out,)

    def _add(self, sid, start, end, status="pending"):
        """근무 하나 + 항목 하나짜리 초안. 항목 id. status='live' 면 실시간으로 쌓이는 중인 구간."""
        import db
        with db.connect() as conn:
            conn.execute("INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) "
                         "VALUES (?,?,?,?,'test',?)", (sid, sid.rsplit("-", 1)[1], start, end, db.now()))
            did = conn.execute("INSERT INTO draft (shift_id, status, generator, generated_at) VALUES (?,?,'test',?)",
                               (sid, status, db.now())).lastrowid
            return conn.execute("INSERT INTO draft_item (draft_id, event_id, seq, origin, tag, title, body, severity) "
                                "VALUES (?, NULL, 1, 'detected', 'TI-403', '항목', '본문', '중')", (did,)).lastrowid

    def test_old_screens_are_gone(self):
        """옛 화면 주소는 새 화면으로 돌려보낸다 — 공개 소개 페이지(docs/intro.html)·발표 PC 북마크가 아직 /pipeline 을 써서
        404 가 됐다(조각 1 반증 발견). /pipeline 의 기능은 관리로 갔지만 공개 링크로 관리가 새면 안 되므로 초안으로 보낸다."""
        _fresh_db()
        for old, new in (("/", "/draft"), ("/pipeline", "/draft"), ("/rtdb", "/dcs")):
            code, _, out = self._get(old)
            self.assertEqual((code, out.get("Location")), (303, new), old)
        code, body, _ = self._get("/nope")
        self.assertEqual(code, 404)
        self.assertNotIn('class="nav"', body, "이동줄은 없앴다 — 두 화면은 주소로 연다")
        self.assertIn('href="/draft">일지 목록으로', body, "없는 주소로 들어와도 돌아갈 길 한 줄은 있다")
        self.assertNotIn('href="/admin"', body, "404 에도 관리 링크는 없다")

    def test_dcs_is_the_overview_alone(self):
        _fresh_db()
        code, body, _ = self._get("/dcs")
        self.assertEqual(code, 200)
        self.assertIn('id="viewOverview"', body, "원본 개요 화면이 떠야 한다")
        self.assertNotIn('class="nav"', body, "ENGRA 이동줄 없이 전체 화면")
        self.assertNotIn("<b>ENGRA</b> 교대 인수인계", body, "ENGRA 상단바 없음")
        self.assertNotIn('href="/admin"', body, "공개 화면에 관리 링크가 새면 안 된다")
        css = body[body.rindex("<style>"):]
        for sel in (".hminav", "#viewData", "#viewScen", "#viewRtdb"):
            self.assertIn(f"body{self.DCS_ON} {sel}", css, f"원본 탭 줄·개요 밖 화면({sel})을 가린다")

    def test_dcs_css_is_off_unless_source_structure_matches(self):
        """전체 화면 규칙은 CSS 가 기대는 원본 구조(.hmi 안 #viewOverview · .wrap 바로 아래 .disc)가 있을 때만 켜진다.
        원본이 바뀌어 구조가 어긋나면 빈 화면이나 안내 누락이 조용히 나는 대신 원본이 그대로 보인다(조각 1 반증 발견)."""
        import re
        _fresh_db()
        import server
        css = server._DCS_FULL_CSS.split("<style>", 1)[1]
        sels = [s.strip() for s in re.findall(r"([^{}]+)\{", css) if not s.strip().startswith("@")]
        self.assertTrue(sels, "규칙을 못 찾음")
        for s in sels:
            for part in s.split(","):
                self.assertIn(self.DCS_ON, part, f"조건 없이 켜지는 규칙: {part.strip()}")

    def test_dcs_source_has_the_structure_the_css_expects(self):
        """/dcs CSS 가 기대는 원본 구조를 서빙된 HTML 에서 요소 관계로 확인한다 — 문자열이 아니라 트리로.
        원본(docs/asu_dcs_overview.html, 임도영님 파일)이 바뀌어 이게 깨지면 전체 화면이 꺼지므로 여기서 먼저 알린다."""
        from html.parser import HTMLParser
        _fresh_db()
        code, body, _ = self._get("/dcs")
        self.assertEqual(code, 200)

        class Tree(HTMLParser):
            VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

            def __init__(self):
                super().__init__()
                self.stack, self.hmi, self.disc, self.disc_under_wrap, self.ov_in_hmi, self.mimic_in_hmi = [], 0, 0, 0, 0, 0

            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                cls = (a.get("class") or "").split()
                in_hmi = any("hmi" in c for _, c in self.stack)
                self.hmi += "hmi" in cls
                if "disc" in cls:
                    self.disc += 1
                    self.disc_under_wrap += bool(self.stack) and "wrap" in self.stack[-1][1]
                self.ov_in_hmi += a.get("id") == "viewOverview" and in_hmi
                self.mimic_in_hmi += tag == "svg" and "mimic" in cls and in_hmi
                if tag not in self.VOID:
                    self.stack.append((tag, cls))

            def handle_endtag(self, tag):
                for i in range(len(self.stack) - 1, -1, -1):   # 짝이 안 맞으면 가장 가까운 같은 태그까지 닫는다
                    if self.stack[i][0] == tag:
                        del self.stack[i:]
                        break

        t = Tree()
        t.feed(body)
        self.assertEqual(t.hmi, 1, "class=hmi 는 정확히 1개")
        self.assertEqual((t.disc, t.disc_under_wrap), (1, 1), ".disc 는 1개이고 .wrap 바로 아래")
        self.assertEqual(t.ov_in_hmi, 1, ".hmi 안에 #viewOverview")
        self.assertGreaterEqual(t.mimic_in_hmi, 1, ".hmi 안에 svg.mimic")

    def test_dcs_keeps_mock_data_notice(self):
        """전체 화면이어도 원본의 「모의 화면 · 가상 데이터」 안내는 보이게 둔다 — 공개 주소라 심사위원이 보는 화면에서 가상 데이터 표시를 빼지 않는다."""
        _fresh_db()
        code, body, _ = self._get("/dcs")
        self.assertEqual(code, 200)
        self.assertIn('class="disc"', body, "원본의 가상 데이터 안내가 응답에 있어야 한다")
        self.assertIn(".wrap>.disc{visibility:visible", body, "전체 화면 CSS 가 그 안내를 가리면 안 된다")

    def test_draft_list_and_detail_without_admin_link(self):
        _fresh_db()
        import approve
        iid = self._add(*self.DAY)
        approve.decide(self.DAY[0], {iid: {"adopted": True, "status": "완료"}})
        self._add(*self.NIGHT)
        code, body, _ = self._get("/draft")
        self.assertEqual(code, 200)
        for sid in (self.DAY[0], self.NIGHT[0]):
            self.assertIn(f'href="/shift/{sid}"', body, "목록에 확정·대기 근무가 다 있어야 한다")
        self.assertNotIn('href="/admin"', body, "공개 화면에 관리 링크가 새면 안 된다")
        # 확정 일지 · 승인 대기 초안 — 같은 한 화면의 오른쪽에 편다
        for sid, mark in ((self.DAY[0], "인수인계서"), (self.NIGHT[0], 'onsubmit="return chk(this)"')):
            code, body, _ = self._get(f"/shift/{sid}")
            self.assertEqual(code, 200, sid)
            self.assertTrue(mark in body, sid)
            self.assertFalse('<a class="back" href="/draft">' in body,
                             "목록이 늘 왼쪽에 있으니 되돌아가기 줄은 없다")
            self.assertTrue('class="side"' in body, f"{sid}: 상세에도 왼쪽 목록이 함께 있어야 한다")
            self.assertFalse('href="/admin"' in body, sid)
            self.assertFalse('class="nav"' in body, "상세에도 이동줄은 없다")

    def test_list_shows_state_labels(self):
        """일지 목록의 표시 — 확정 전 「초안」 · 확정 「확정 · 채택 N건」 · 실시간으로 쌓이는 중 「LIVE · 쌓이는 중」(맨 위).
        경모님 2026-09-14: 목록이 첫 화면이라 줄만 보고 무엇이 남았는지 알아야 한다."""
        _fresh_db()
        import approve
        iid = self._add(*self.DAY)
        approve.decide(self.DAY[0], {iid: {"adopted": True, "status": "완료"}})
        self._add(*self.NIGHT)
        self._add("2026-08-26-day", "2026-08-26T06:00:00", "2026-08-26T18:00:00", status="live")
        code, body, _ = self._get("/draft")
        self.assertEqual(code, 200)
        self.assertIn("LIVE · 쌓이는 중", body, "쌓이는 구간 줄")
        self.assertIn(">초안<", body, "확정 전 근무는 「초안」")
        self.assertIn("확정 · 채택 1건", body, "확정된 근무는 채택 건수까지")
        self.assertLess(body.index("LIVE · 쌓이는 중"), body.index(">초안<"), "쌓이는 구간이 맨 위")
        self.assertFalse('class="nav"' in body, "이동줄 없음")
        # 경모님 2026-09-14: 줄에는 날짜·주간/야간·상태만. 구간 시각과 감지 요약은 뺀다
        # — 뒤에 채택 건수가 나오는데 감지 요약이 또 있으면 헷갈리고, 주간/야간이면 시각은 짐작된다.
        self.assertFalse("06:00–18:00" in body, "줄에 구간 시각을 적지 않는다")
        self.assertFalse("TI-403" in body.split('class="detail"')[0], "줄에 감지 요약을 적지 않는다")
        self.assertFalse("교대 1시간 전" in body, "안내는 한 줄까지만 — 뒷문장은 뺀다")

    def test_one_page_opens_with_a_shift_already_chosen(self):
        """목록과 상세가 따로 열려 같은 근무를 두 번 찾아 들어가야 했다(경모님 2026-09-14) — 한 화면으로 합친다.
        /draft 는 쌓이는 중인 근무를, 없으면 가장 최근 근무를 골라 연다. /shift/<근무> 는 그 근무를 고른 같은 화면이다."""
        _fresh_db()
        import approve
        iid = self._add(*self.DAY)
        approve.decide(self.DAY[0], {iid: {"adopted": True, "status": "완료"}})
        self._add(*self.NIGHT)

        code, body, _ = self._get("/draft")
        self.assertEqual(code, 200)
        self.assertTrue('class="side"' in body and 'class="detail"' in body, "왼쪽 목록 + 오른쪽 내용")
        self.assertTrue('onsubmit="return chk(this)"' in body, "빈 화면을 만들지 않는다 — 가장 최근 근무를 펴 둔다")
        self.assertTrue(f'class="nrow on" data-shift="{self.NIGHT[0]}" href="/shift/{self.NIGHT[0]}"' in body, "고른 근무는 목록에서 표시한다")
        self.assertEqual(body.count('class="nrow on"'), 1, "표시는 한 줄만")
        self.assertEqual(body, self._get(f"/shift/{self.NIGHT[0]}")[1], "주소만 다르고 같은 한 화면이다")

        # 쌓이는 중인 근무가 있으면 그것부터
        self._add("2026-08-26-day", "2026-08-26T06:00:00", "2026-08-26T18:00:00", status="live")
        _, body, _ = self._get("/draft")
        self.assertTrue('class="nrow live on" data-shift="2026-08-26-day" href="/shift/2026-08-26-day"' in body,
                        "재생 중이면 그 근무를 연다")

    def test_one_page_with_no_shifts_shows_the_empty_state(self):
        """근무가 하나도 없으면 오른쪽에 안내를 둔다 — 왼쪽만 비고 오른쪽이 빈 화면이 되면 안 된다.
        고를 것이 없으면 왼쪽 목록과 「근무 고르기」는 아예 그리지 않는다 — 제목만 남은 껍데기가
        모바일에서 빈 상자로 열렸다(반증 워커)."""
        _fresh_db()
        import db      # _fresh_db 가 모듈을 갈아 끼우므로 그 뒤에 가져와야 같은 DB 를 본다
        for body in (self._get("/draft")[1], ):
            self.assertTrue("아직 근무가 없습니다" in body, "빈 상태 안내")
            self.assertFalse('class="nrow' in body, "줄은 없다")
            self.assertFalse('class="side"' in body, "고를 것이 없으면 왼쪽 목록을 그리지 않는다")
            self.assertFalse('id="pick"' in body, "펼칠 것이 없으면 「근무 고르기」도 없다")

        # 적재만 되고 초안이 아직 없는 근무도 목록에 세우지 않는다(경모님: "미생성은 있을 필요 없다")
        with db.connect() as conn:
            conn.execute("INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) "
                         "VALUES (?,'day',?,?,'test',?)", (*self.DAY, db.now()))
        code, body, _ = self._get("/draft")
        self.assertEqual(code, 200)
        self.assertTrue("아직 초안이 없습니다" in body, "적재만 된 상태의 안내")
        self.assertFalse('class="side"' in body, "줄이 0개면 왼쪽은 껍데기로도 남지 않는다")
        self.assertFalse('id="pick"' in body, "펼칠 것이 없으면 「근무 고르기」도 없다")

    def test_shift_picker_is_not_a_hidden_tab_stop_on_pc(self):
        """PC 에서 첫 Tab 이 보이지 않는 체크박스에 걸렸다(반증 워커 실측 1440) — 목록이 늘 펼쳐져 있어
        눌러도 아무 일도 안 나는 컨트롤이다. 기본은 아예 빼고, 접기가 실제로 도는 모바일 폭에서만 되살린다."""
        import server
        base, _, mobile = server.STYLE.partition("@media (max-width:760px)")
        self.assertTrue(mobile, "모바일 미디어 쿼리를 못 찾음")
        self.assertTrue(".pickbox{display:none}" in base, "PC 기본은 초점 순서에서 뺀다")
        self.assertTrue(".pickbox{position:absolute" in mobile, "모바일에서는 초점을 받도록 되살린다")
        self.assertFalse(".pickbox{position:absolute" in base, "PC 에 보이지 않는 초점 자리를 남기지 않는다")

    def test_live_shift_locks_approval_but_keeps_the_form(self):
        """쌓이는 중인 근무(status='live')는 **승인만** 잠근다. 처음에는 화면 전체를 읽기 전용으로 뒀는데(조각 1),
        실시간 누적이 붙으면서 경모님 결정대로 바꿨다 — 관찰 중은 회색이라 못 건드리고, AI 가 다 쓴 항목은 미리 고르고
        코멘트를 달 수 있고, 잠기는 것은 승인 버튼과 그 이유 문구다. 폼을 우회한 제출은 approve.decide 가 막는다."""
        _fresh_db()
        import approve
        live_sid = "2026-08-26-day"
        iid = self._add(live_sid, "2026-08-26T06:00:00", "2026-08-26T18:00:00", status="live")
        code, body, _ = self._get(f"/shift/{live_sid}")
        self.assertEqual(code, 200)
        self.assertTrue('action="/approve"' in body, "고르기·코멘트는 미리 할 수 있다")
        self.assertRegex(body, r'<button[^>]*id="approve"[^>]*disabled', "승인 버튼은 잠긴다")
        self.assertRegex(body, r'id="approvelock"[^>]*>[^<]+<', "왜 못 누르는지 문구가 있다")
        self.assertIn("LIVE · 쌓이는 중", body, "상태 이름은 남는다 — 설명 문장은 없앴다(경모님 지시 2026-09-15)")
        self.assertTrue("TI-403" in body, "쌓인 항목은 읽을 수 있다")
        with self.assertRaises(ValueError):     # 폼을 우회한 POST
            approve.decide(live_sid, {iid: {"adopted": True, "status": "완료"}})

        # 대조군 — 끝난 근무는 잠기지 않는다(위 검사가 공허하지 않음을 보인다)
        done = self._add(*self.NIGHT)
        _, body2, _ = self._get(f"/shift/{self.NIGHT[0]}")
        self.assertTrue('action="/approve"' in body2, "끝난 근무의 승인 폼까지 사라지면 안 된다")
        self.assertNotRegex(body2, r'<button[^>]*id="approve"[^>]*disabled', "끝난 근무의 승인 버튼은 열려 있다")
        approve.decide(self.NIGHT[0], {done: {"adopted": True, "status": "완료"}})

    def test_admin_has_one_pipeline_card(self):
        """/pipeline 의 업로드·실행·진행과 관리의 정답지 업로드·다시 만들기가 한 카드로 — 같은 폼이 두 번 나오지 않는다."""
        _fresh_db()
        code, body, _ = self._get("/admin")
        self.assertEqual(code, 200)
        self.assertEqual(body.count('action="/pipeline/upload"'), 1, "업로드 폼은 하나")
        self.assertEqual(body.count('action="/pipeline/run"'), 1, "실행 폼은 하나")
        self.assertIn('accept=".csv,.json"', body, "근무 CSV 와 정답지를 같은 폼으로 올린다")
        self.assertIn('type="checkbox" name="redo" value="1"', body, "「확정돼 있어도 다시 만들기」는 실행의 선택지")
        self.assertIn('id="joblog"', body, "/pipeline 에만 있던 작업 진행 표시가 관리로 와야 한다")


class UploadAllOrNothing(unittest.TestCase):
    """올린 파일은 전부 검사한 뒤에 저장한다 — 하나라도 거부되면 아무것도 저장하지 않는다.

    파일마다 저장하면서 검사했더니, 열쇠 잠금에서 CSV 와 정답지 JSON 을 함께 올리면 앞의 CSV 가 uploads/ 에 저장만 되고
    적재는 안 되는 부분 상태가 남았다(조각 1 반증 발견). 형식이 틀린 정답지도 같은 길로 파일이 먼저 남았다.
    """

    longMessage = False
    CSV = ("shift_a.csv", b"timestamp,tag,value\n2026-08-25T06:00:00,TI-403,-172.5\n")
    KEY = ("asu_answer_x.json", b'{"shift_id": "2026-08-25-day", "injected": []}')

    def setUp(self):
        from pathlib import Path
        _fresh_db()
        import jobs
        self.dir = tempfile.mkdtemp(prefix="engra_up_")
        self.ingested = []
        self._keep = (jobs.UPLOAD_DIR, jobs.SEEN_FILE, jobs.ingest_async, os.environ.get("ENGRA_ADMIN_KEY"))
        jobs.UPLOAD_DIR, jobs.SEEN_FILE = Path(self.dir), Path(self.dir) / ".keys_first_seen.json"
        jobs.ingest_async = lambda paths, skip_bad=False: self.ingested.extend(paths) or True   # 적재 스레드는 띄우지 않는다
        os.environ["ENGRA_ADMIN_KEY"] = "k-test"   # 심사 기간 잠금

    def tearDown(self):
        import shutil
        import jobs
        jobs.UPLOAD_DIR, jobs.SEEN_FILE, jobs.ingest_async, key = self._keep
        if key is None:
            os.environ.pop("ENGRA_ADMIN_KEY", None)
        else:
            os.environ["ENGRA_ADMIN_KEY"] = key
        shutil.rmtree(self.dir, ignore_errors=True)

    def _upload(self, files, cookie=None):
        """multipart 본문을 만들어 업로드 핸들러 본체만 부른다. (코드, 본문, Location)."""
        import server
        b = "ENGRAtestBOUNDARY"
        raw = b"".join(f'--{b}\r\nContent-Disposition: form-data; name="files"; filename="{fn}"\r\n'
                       f'Content-Type: application/octet-stream\r\n\r\n'.encode() + data + b"\r\n" for fn, data in files)
        raw += f"--{b}--\r\n".encode()

        class Fake(server.Handler):
            def __init__(self):
                self.path = "/pipeline/upload"; self.sent = []; self.out = {}
                self.headers = {"Cookie": cookie} if cookie else {}
            def _send(self, code, body, ctype=""): self.sent.append((code, body))
            def send_response(self, code): self.sent.append((code, ""))
            def send_header(self, k, v): self.out[k] = v
            def end_headers(self): pass

        h = Fake()
        h._handle_upload(f"multipart/form-data; boundary={b}", raw)
        return h.sent[-1] + (h.out.get("Location"),)

    def test_locked_csv_with_key_file_saves_nothing(self):
        code, body, _ = self._upload([self.CSV, self.KEY])
        self.assertEqual(code, 400, "열쇠 없이 정답지가 섞이면 거부")
        self.assertIn("asu_answer_x.json", body, "어느 파일이 왜 거부됐는지 적는다")
        self.assertEqual(os.listdir(self.dir), [], "하나라도 거부되면 앞의 CSV 도 저장하지 않는다")
        self.assertEqual(self.ingested, [], "적재도 시작하지 않는다")

    def test_locked_csv_alone_is_ingested(self):
        code, _, loc = self._upload([self.CSV])
        self.assertEqual((code, loc), (303, "/admin"), "CSV 는 열쇠 없이 올린다")
        self.assertEqual(os.listdir(self.dir), ["shift_a.csv"])
        self.assertEqual([p.name for p in self.ingested], ["shift_a.csv"], "올린 CSV 는 바로 적재")

    def test_broken_key_file_saves_nothing(self):
        code, body, _ = self._upload([self.CSV, ("asu_answer_bad.json", b'{"nope": 1}')], cookie="engra_admin=k-test")
        self.assertEqual(code, 400, "형식이 틀린 정답지는 거부")
        self.assertIn("asu_answer_bad.json", body, "어느 파일인지 적는다")
        self.assertEqual(os.listdir(self.dir), [], "형식 검사도 저장 전에 — 파일이 먼저 남으면 안 된다")

    def test_broken_gzip_saves_nothing(self):
        code, body, _ = self._upload([self.CSV, ("shift_b.csv.gz", b"not gzip")])
        self.assertEqual(code, 400, "풀리지 않는 압축 파일은 거부")
        self.assertIn("shift_b.csv.gz", body, "어느 파일인지 적는다")
        self.assertEqual(os.listdir(self.dir), [], "압축 검사도 저장 전에")

    def test_bad_shift_id_saves_nothing_and_keeps_same_name_file(self):
        """열쇠가 있어도 shift_id 가 목록·객체면 등록에서 TypeError 가 나 앞의 CSV·JSON 이 uploads/ 에 남았고,
        같은 이름의 기존 파일은 덮였다(조각 1 재반증). 검사가 저장 전제조건까지 보고, 기존 파일은 그대로 둔다."""
        old = os.path.join(self.dir, "shift_a.csv")
        with open(old, "wb") as f:
            f.write(b"OLD")
        for key in (b'{"shift_id": ["x"], "injected": []}', b'{"shift_list": [{"shift_id": {"a": 1}}]}'):
            code, body, _ = self._upload([self.CSV, ("asu_answer_b.json", key)], cookie="engra_admin=k-test")
            self.assertEqual(code, 400, key)
            self.assertIn("asu_answer_b.json", body, "어느 파일인지 적는다")
            self.assertEqual(os.listdir(self.dir), ["shift_a.csv"], "새 파일이 남으면 안 된다")
            with open(old, "rb") as f:
                self.assertEqual(f.read(), b"OLD", "같은 이름의 기존 파일을 덮으면 안 된다")

    def test_empty_file_name_saves_nothing(self):
        """파일 이름이 「/」·「.」 이면 저장할 이름이 비어 폴더에 쓰려다 실패했고, 그 전에 저장된 CSV 가 남거나
        같은 이름 파일을 덮었다(조각 1 재반증)."""
        old = os.path.join(self.dir, "shift_a.csv")
        with open(old, "wb") as f:
            f.write(b"OLD")
        for bad in ("/", "."):
            code, _, _ = self._upload([("shift_a.csv", b"NEW," + self.CSV[1]), (bad, b"x")])
            self.assertEqual(code, 400, bad)
            self.assertEqual(os.listdir(self.dir), ["shift_a.csv"], bad)
            with open(old, "rb") as f:
                self.assertEqual(f.read(), b"OLD", bad)

    def test_numeric_shift_id_is_rejected(self):
        """숫자 shift_id 가 등록되면 관리 카드의 sorted(KEYS) 가 문자열과 섞여 TypeError — /admin 이 500 이 됐다(조각 1 재반증)."""
        import jobs, server
        code, _, _ = self._upload([self.KEY, ("asu_answer_n.json", b'{"shift_id": 123, "injected": []}')], cookie="engra_admin=k-test")
        self.assertEqual(code, 400, "숫자 shift_id 는 거부")
        self.assertEqual(os.listdir(self.dir), [], "함께 올린 정상 정답지도 저장하지 않는다")
        self.assertEqual(jobs.KEYS, {}, "등록도 없다")
        server._pipeline_card(False)   # 500 이 나던 자리 — 예외 없이 그려져야 한다

    def test_failure_while_saving_leaves_nothing(self):
        """검사를 통과한 뒤 저장 도중 실패해도 이번 요청의 파일은 하나도 반영하지 않는다 — 임시 이름으로 다 쓴 뒤에 옮긴다."""
        import jobs
        old = os.path.join(self.dir, "a.csv")
        with open(old, "wb") as f:
            f.write(b"OLD")

        def files():
            yield "a.csv", b"NEW"
            raise OSError("디스크 가득 참(시험)")

        with self.assertRaises(OSError):
            jobs.save_uploads(files())
        self.assertEqual(os.listdir(self.dir), ["a.csv"], "임시 파일도 남기지 않는다")
        with open(old, "rb") as f:
            self.assertEqual(f.read(), b"OLD", "옮기기 전에 실패했으니 기존 파일은 그대로")

    def test_key_is_judged_on_the_saved_name(self):
        """열쇠·형식 검사는 저장될 이름으로 판정한다 — 원래 이름 「x.json/」 으로 판정해 열쇠를 건너뛰고 x.json 으로 저장·등록됐다(조각 1 2차 재반증)."""
        import gzip, jobs
        for name, data in (("x.json/", self.KEY[1]), ("x.json/.gz", gzip.compress(self.KEY[1]))):
            code, _, _ = self._upload([self.CSV, (name, data)])
            self.assertEqual(code, 400, name)
            self.assertEqual(os.listdir(self.dir), [], name)
            self.assertEqual(jobs.KEYS, {}, name)

    def test_same_saved_name_twice_is_rejected(self):
        """한 요청 안에서 정리된 이름이 겹치면 알림 없이 뒤 파일이 앞 파일을 덮었다(조각 1 2차 재반증)."""
        import gzip, jobs
        key2 = b'{"shift_id": "2026-08-25-night", "injected": []}'
        for files in ([("dup.csv", self.CSV[1]), ("dup.csv.gz", gzip.compress(b"NEW," + self.CSV[1]))],
                      [("d1/asu_answer_x.json", self.KEY[1]), ("d2/asu_answer_x.json", key2)]):
            code, body, _ = self._upload(files, cookie="engra_admin=k-test")
            self.assertEqual(code, 400, files[0][0])
            self.assertIn("같은 이름으로 저장될 파일이 둘", body, files[0][0])
            self.assertEqual(os.listdir(self.dir), [], files[0][0])
            self.assertEqual(jobs.KEYS, {}, files[0][0])
        with self.assertRaises(ValueError):   # 저장 함수도 스스로 막는다
            jobs.save_uploads([("a.csv", b"1"), ("a.csv", b"2")])
        self.assertEqual(os.listdir(self.dir), [], "임시 파일도 남기지 않는다")

    def test_unsavable_names_are_rejected_before_saving(self):
        """NUL 이 들어간 이름 · 255바이트를 넘는 이름은 검사에서 400 — 저장 단계 500 이 되던 것(조각 1 2차 재반증)."""
        for name in ("a\x00b.csv", "a" * 252 + ".csv"):
            code, _, _ = self._upload([self.CSV, (name, b"x")])
            self.assertEqual(code, 400, repr(name[:12]))
            self.assertEqual(os.listdir(self.dir), [], repr(name[:12]))

    def test_long_but_valid_name_is_saved(self):
        """245바이트 이름은 정상 — 임시 이름에 원래 이름을 붙여 임시 파일에서만 길이 제한에 걸리던 회귀(조각 1 2차 재반증)."""
        name = "a" * 241 + ".csv"
        code, _, loc = self._upload([(name, self.CSV[1])])
        self.assertEqual((code, loc), (303, "/admin"))
        self.assertEqual(os.listdir(self.dir), [name])

    def test_save_failure_does_not_leak_server_paths(self):
        """검사를 통과한 뒤 저장이 실패하면 500 이지만, 본문에는 고정 문구와 예외 종류만 — 서버 경로는 로그에만(조각 1 2차 재반증)."""
        from pathlib import Path
        import jobs
        not_a_dir = Path(self.dir) / "uploads"
        not_a_dir.write_bytes(b"")          # 폴더 자리에 파일 → 저장 단계에서 실패
        jobs.UPLOAD_DIR = not_a_dir
        code, body, _ = self._upload([self.CSV])
        self.assertEqual(code, 500)
        self.assertNotIn(self.dir, body, "서버 경로가 응답에 나가면 안 된다")
        self.assertIn("FileExistsError", body, "예외 종류는 보인다")

    def test_key_check_ignores_extension_case(self):
        """열쇠 검사가 대소문자를 구분해 「asu_answer_x.JSON」 이 빠져나갔다 — 대소문자를 구분하지 않는 파일시스템에서는
        기존 .json 정답지를 덮어썼다(조각 1 3차 재반증)."""
        import jobs
        code, _, _ = self._upload([("asu_answer_x.JSON", self.KEY[1])])
        self.assertEqual(code, 400, "확장자 대소문자와 무관하게 열쇠를 본다")
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(jobs.KEYS, {})

    def test_same_name_after_unicode_normalization_is_rejected(self):
        """중복 검사가 문자열 비교라, 자모 조합만 다른 같은 이름(NFC/NFD)이 조용히 덮였다(조각 1 3차 재반증)."""
        import unicodedata
        nfc, nfd = unicodedata.normalize("NFC", "근무.csv"), unicodedata.normalize("NFD", "근무.csv")
        self.assertNotEqual(nfc, nfd, "두 꼴이 달라야 시험이 성립한다")
        code, body, _ = self._upload([(nfc, self.CSV[1]), (nfd, b"NEW," + self.CSV[1])])
        self.assertEqual(code, 400)
        self.assertIn("같은 이름으로 저장될 파일이 둘", body)
        self.assertEqual(os.listdir(self.dir), [])

    def test_long_name_with_gz_is_saved(self):
        """.gz 를 떼기 전에 이름을 검사해, 253바이트 이름이 압축되면(브라우저가 .gz 를 붙여 256바이트) 400 이 됐다 —
        같은 파일이 크기에 따라 되다 안 되다 했다(조각 1 3차 재반증)."""
        import gzip
        name = "a" * 249 + ".csv"
        code, _, loc = self._upload([(name + ".gz", gzip.compress(self.CSV[1]))])
        self.assertEqual((code, loc), (303, "/admin"), "압축을 풀고 난 이름이 255바이트 안이면 정상 저장")
        self.assertEqual(os.listdir(self.dir), [name])

    def test_normal_uploads_still_pass(self):
        import gzip, jobs
        code, _, loc = self._upload([self.CSV, self.KEY], cookie="engra_admin=k-test")
        self.assertEqual((code, loc), (303, "/admin"))
        self.assertTrue({"shift_a.csv", "asu_answer_x.json"} <= set(os.listdir(self.dir)))
        self.assertIn("2026-08-25-day", jobs.KEYS)
        bundle = b'{"shift_list": [{"shift_id": "s1", "injected": []}, {"shift_id": "s2", "injected": []}]}'
        code, _, loc = self._upload([("shift_b.csv.gz", gzip.compress(self.CSV[1])), ("asu_answer_all.json", bundle)],
                                    cookie="engra_admin=k-test")
        self.assertEqual((code, loc), (303, "/admin"))
        self.assertTrue({"shift_b.csv", "asu_answer_all.json"} <= set(os.listdir(self.dir)), "압축은 풀고 이름에서 .gz 를 뗀다")
        self.assertTrue({"s1", "s2"} <= set(jobs.KEYS))
        self.assertEqual([p.name for p in self.ingested], ["shift_a.csv", "shift_b.csv"])


class ScreenNumbersAndTrends(unittest.TestCase):
    """경모님 화면 지적(2026-09-14) — 숫자 지수 표기 금지 · 그래프는 근무 구간 전체 하나 · 중요도는 화면에서 뺀다 ·
    확정 일지에도 추이(기본 접힘) · 곡선은 항목에 저장해 원본이 회전돼 지워져도 남는다."""

    longMessage = False
    SID = ("2026-08-25-day", "2026-08-25T06:00:00", "2026-08-25T18:00:00")
    # 값이 큰 태그(40,000대 · 18,000대)와 1 미만의 아주 작은 값 — 지수 표기가 나오는 두 끝
    TAGS = {"FI-602": 40131.0, "SI-507": 18500.0, "XI-001": 0.000032}

    @staticmethod
    def _exp(body):
        """지수 표기 숫자. CSS 색상값(#e4e1dc)·태그(A1E5)처럼 앞뒤가 영문·숫자·# 인 것은 숫자가 아니라 뺀다.
        경계는 ASCII 로만 잡는다 — \\w 는 한글도 글자로 봐서 「1e8배」 처럼 한글이 바로 붙은 지수 표기를 놓쳤다."""
        import re
        return re.findall(r"(?<![0-9A-Za-z_#.])\d+(?:\.\d+)?[eE][+-]?\d+(?![0-9A-Za-z_.])", body)

    @staticmethod
    def _detail(body):
        """한 화면의 오른쪽만 — 파비콘의 <svg> 와 바닥 스크립트를 세지 않게."""
        return body.split('class="detail"', 1)[1].split("</main>", 1)[0]

    def _seed(self, raw=True):
        """근무 하나 · 태그마다 12시간 1분 원본 · 감지 이벤트(파형) · 초안 항목. 엔진·AI 문장처럼 제목·근거에 `.4g` 수치를 넣는다."""
        _fresh_db()
        import db
        import json
        import math
        from engine.detectors import _fmt as fmt      # 엔진 근거 문장과 같은 수치 표기 — 손으로 지수 문자열을 넣지 않는다
        sid, start, end = self.SID
        ids = []
        with db.connect() as conn:
            conn.execute("INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) "
                         "VALUES (?, 'day', ?, ?, 'test', ?)", (sid, start, end, db.now()))
            did = conn.execute("INSERT INTO draft (shift_id, status, generator, generated_at) "
                               "VALUES (?, 'pending', 'test', ?)", (sid, db.now())).lastrowid
            for seq, (tag, base) in enumerate(self.TAGS.items(), 1):
                if raw:
                    conn.executemany("INSERT INTO raw_sample (tag, ts, value) VALUES (?,?,?)",
                                     [(tag, f"2026-08-25T{6 + m // 60:02d}:{m % 60:02d}:00",
                                       base * (1 + 0.002 * math.sin(m / 40))) for m in range(720)])
                wave = {"v": [base * (1 + 0.001 * i) for i in range(60)],
                        "t0": "2026-08-25T09:00:00", "t1": "2026-08-25T10:00:00",
                        "mark": ["2026-08-25T09:20:00", "2026-08-25T09:40:00"]}
                eid = conn.execute(
                    "INSERT INTO event (shift_id, tag, kind, start_ts, end_ts, severity, score, metrics_json, evidence, "
                    "detector, created_at, waveform_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, tag, "드리프트", "2026-08-25T09:20:00", "2026-08-25T09:40:00", "상", 3.0,
                     json.dumps({"unit": ""}), f"{tag} 변화율 {fmt(0.000032, '/h')}", "engine", db.now(), json.dumps(wave))).lastrowid
                ids.append(conn.execute(
                    "INSERT INTO draft_item (draft_id, event_id, seq, origin, tag, title, body, evidence, severity, "
                    "severity_rule, severity_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (did, eid, seq, "detected", tag, f"{tag} 드리프트 — {fmt(base)} 부근", "문장",
                     f"{tag} 변화율 {fmt(0.000032, '/h')} · 지금 {fmt(base)} · 흔들림 평소의 {fmt(1200)}배", "상", "중",
                     "놓치면 설비에 직결")).lastrowid)
        return ids

    def _approve(self, ids, adopted=None):
        import approve
        adopted = ids if adopted is None else adopted
        approve.decide(self.SID[0], {i: ({"adopted": True, "status": "완료"} if i in adopted else {"adopted": False})
                                     for i in ids})

    def test_numbers_never_use_exponent_notation(self):
        """축 눈금이 `1.77e+04` 처럼 지수로 나왔다(경모님 지적 — 미리보기 21곳). 값이 큰 태그의 눈금이 `:.4g` 로 찍혔고,
        엔진 근거 문장도 1 미만 값을 `.4g`(detectors._fmt)로 만들어 `3.2e-05` 가 됐다. 화면 직전에 가리면 정적 스냅숏·
        AI 입력·명령줄 같은 다른 출구로 다시 새므로 원인(엔진 _fmt · 앱 눈금)에서 고친다. 시드 문장은 엔진 _fmt 가 만든다."""
        ids = self._seed()
        import server
        sys.path.append(os.path.join(ROOT, "tools"))
        import snapshot
        body = server.view_shift(self.SID[0])
        self.assertEqual(self._exp(body), [], "초안 화면에 지수 표기가 남았다")
        self.assertTrue("40,1" in self._detail(body), "큰 값은 천 단위 쉼표 붙은 보통 숫자로")
        self.assertEqual(self._exp(snapshot.to_static(body, "index.html")), [], "초안 정적 스냅숏에 지수 표기가 남았다")
        self._approve(ids)
        body = server.view_shift(self.SID[0])
        self.assertEqual(self._exp(body), [], "확정 일지에 지수 표기가 남았다")
        self.assertEqual(self._exp(snapshot.to_static(body, "handover.html")), [], "확정 일지 정적 스냅숏에 지수 표기가 남았다")

    def test_engine_fmt_never_uses_exponent_notation(self):
        """엔진 근거 문장의 수치(detectors._fmt)가 1 미만 값을 `.4g` 로 찍어 3.2e-05 같은 지수 표기가 나왔다.
        원인에서 고친다(경모님 확인 — 팀원 파일 수정 허용): 1 미만은 유효숫자 4자리 고정소수, 0 은 0, 1 이상 규칙은 그대로."""
        from engine.detectors import _fmt
        cases = {1e8: "100,000,000", 3.2e-05: "0.000032", 3.214e-05: "0.00003214", 1.234e-03: "0.001234", 0.5: "0.5", 0: "0",
                 -3.2e-05: "-0.000032", 0.99996: "1.00", -0.99996: "-1.00", 9.996: "10.0", 999.95: "1,000", 1: "1.00", 9.994: "9.99", 10: "10.0", 999.94: "999.9",
                 1000: "1,000", 40131.4: "40,131"}
        for v, want in cases.items():
            with self.subTest(v=v):
                self.assertEqual(_fmt(v), want)
        self.assertEqual(_fmt(3.2e-05, "/h"), "0.000032/h", "단위는 그대로 붙는다")
        self.assertEqual(_fmt(None), "?")

    def test_app_number_rule_matches_engine(self):
        """숫자 표기 규칙은 하나다 — 엔진 근거 문장은 엔진 _fmt 가, 앱이 만드는 숫자(그래프 눈금·호버·최저·최고 · 임시 엔진 근거 ·
        곡선 반올림)는 app/numfmt.fmt 가 찍는다. 엔진은 앱 없이, 앱은 엔진 없이 돌아야 해서 코드가 두 곳이라 여기서 맞춰 본다."""
        from engine.detectors import _fmt
        import numfmt
        for v in (1e8, 40131.4, 18500.4, -1234.5, 1000, 999.95, 999.94, 121.83, 10, 9.996, 9.994, 1, 0.99996, -0.99996, 0.5, 0.001234,
                  3.214e-05, -3.2e-05, 0, None):
            with self.subTest(v=v):
                self.assertEqual(numfmt.fmt(v), _fmt(v))
                if v is not None:
                    self.assertEqual(numfmt.fmt(v, "℃"), _fmt(v, "℃"), "단위를 붙여도 같다")

    def test_stub_engine_evidence_uses_the_number_rule(self):
        """임시 엔진(엔진이 없을 때 도는 앱 쪽 검출)의 근거 문장이 한계·최고값을 서식 없이 끼워 넣었다 — 「최고 126.345℃」 처럼
        규칙과 다른 모양이고, 실수를 그대로 글자로 만들면 아주 작거나 큰 값에서 지수 표기가 된다. 앱 규칙(numfmt.fmt) 하나로 찍는다."""
        import stub_engine
        pts = [("2026-08-25T06:00:00", 120.0), ("2026-08-25T06:01:00", 126.345), ("2026-08-25T06:02:00", 121.0)]
        events = stub_engine.detect({"TI-205": pts}, {})
        self.assertEqual(len(events), 1, events)
        ev = events[0]["evidence"]
        self.assertTrue("한계 125.0℃" in ev and "최고 126.3℃" in ev, ev)
        self.assertEqual(self._exp(ev), [])

    def test_item_cards_have_no_left_color_band(self):
        """중요도 색 띠를 뺀 뒤에도 확정 일지·제외·이월 카드에 두꺼운 왼쪽 선(회색·호박색)이 남았다. 색이 남으면 근무자는
        중요도로 읽고, 회색 띠는 「하」 로 읽힌다(경모님 지적). 항목 카드는 무채색 1px 테두리뿐 — 왼쪽 선 규칙 0건."""
        import re
        import server
        css = re.sub(r"/\*.*?\*/", "", server.STYLE, flags=re.S)
        rules = {}
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            rules.setdefault(sel.strip(), []).append(body)
        for sel in (".item", ".ent", ".ex", ".carry .ci"):
            with self.subTest(selector=sel):
                self.assertTrue(sel in rules, f"{sel} 규칙을 못 찾음")        # 이름이 바뀌어 검사가 공허해지지 않게
                self.assertFalse(any("border-left" in b for b in rules[sel]), f"{sel} 에 왼쪽 선이 남았다")
        # 이월 묶음 카드(.card.carry)는 따로 규칙을 두지 않는다 — 두게 되면 왼쪽 선이 없어야 한다
        self.assertFalse(any("border-left" in b for b in rules.get(".carry", [])), ".carry 에 왼쪽 선이 남았다")
        self.assertIsNone(re.search(r"sev-|data-sev", css), "중요도 값을 쓰는 CSS 선택자가 남았다")

    def test_each_item_has_one_full_shift_trend_with_hover(self):
        """확대 그래프(±30분)를 없애고 근무 구간 전체 그래프 하나만 둔다(경모님 정정). 호버는 그 하나에 붙는다.
        확대 쪽에만 있던 최저·최고는 없애지 않고 전체 그래프 아래로 옮긴다."""
        self._seed()
        import server
        part = self._detail(server.view_shift(self.SID[0]))
        n = len(self.TAGS)
        self.assertEqual(part.count("<svg"), n, "항목마다 그래프 하나")
        self.assertEqual(part.count("data-trend="), n, "그 그래프에 호버 데이터가 붙는다")
        self.assertFalse("확대" in part, "확대 그래프와 그 설명줄은 없앴다")
        self.assertTrue("최저" in part and "최고" in part, "최저·최고는 전체 그래프 아래로 옮겼다")

    def test_severity_is_gone_from_every_screen(self):
        """중요도를 화면에서 뺀다(경모님 지시) — 선택 상자 · 왼쪽 색 띠 · 「중요도 판단」 줄 · 「통계 기준 → AI 판정」.
        엔진·AI 가 만든 DB 값은 남긴다(지우면 엔진·AI·측정이 줄줄이 흔들린다)."""
        ids = self._seed()
        import db
        import server
        sys.path.append(os.path.join(ROOT, "tools"))
        import snapshot
        pages = {"초안": server.view_shift(self.SID[0]), "목록": server.view_draft()}
        self._approve(ids, adopted=ids[:2])
        pages["확정"] = server.view_shift(self.SID[0])
        pages["정적본"] = snapshot.to_static(pages["확정"], "handover.html")
        for name, body in pages.items():
            for mark in ("중요도", "sevsel", 'name="sev_', "sev-high", "sev-mid", "통계 기준"):
                self.assertFalse(mark in body, f"{name} 화면에 {mark!r} 가 남았다")
        with db.connect() as conn:
            self.assertEqual({it["severity"] for it in db.load_draft(conn, self.SID[0])["items"]}, {"상"},
                             "DB 의 중요도 값은 그대로 남아야 한다")

    def test_server_ignores_severity_in_the_approval_form(self):
        """화면에서 중요도를 뺐으니 승인할 때 sev_* 로 중요도를 바꾸는 경로도 없앤다 — 옛 폼이 보내와도 쓰지 않는다."""
        from urllib.parse import urlencode
        ids = self._seed()
        import db      # _fresh_db 가 모듈을 갈아 끼우므로 _seed 뒤에 가져온다 — 앞에서 가져오면 옛 DB 를 본다
        import server

        class Fake(server.Handler):
            def __init__(self):
                self.path = "/approve"; self.sent = []; self.headers = {}
            def _send(self, code, body, ctype=""): self.sent.append((code, body))
            def send_response(self, code): self.sent.append((code, ""))
            def send_header(self, *a): pass
            def end_headers(self): pass

        h = Fake()
        h._handle_form(urlencode([("shift_id", self.SID[0]), ("item", ids[0]), (f"status_{ids[0]}", "완료"),
                                  (f"sev_{ids[0]}", "하")]).encode())
        self.assertEqual(h.sent[-1][0], 303, "중요도 없이 승인은 그대로 된다")
        with db.connect() as conn:
            got = {it["id"]: it["severity"] for it in db.load_draft(conn, self.SID[0])["items"]}
        self.assertEqual(got[ids[0]], "상", "sev_* 를 받아 중요도를 바꾸면 안 된다")

    def test_items_are_ordered_by_first_detection_time(self):
        """엔진은 항목을 중요도 순으로 정렬해 넘긴다(engine/api.py compose). 중요도를 화면에서 뺐으니 그 순서는 근거 없이
        뒤섞여 보인다 — 화면 순서를 처음 감지된 시각으로 바꾼다(초안·확정 일지 둘 다). DB 의 seq 는 그대로 둔다."""
        ids = self._seed()
        import db
        import server
        with db.connect() as conn:      # seq 순서(FI-602 → SI-507 → XI-001)와 다른 감지 시각
            for tag, start in (("FI-602", "2026-08-25T11:00:00"), ("SI-507", "2026-08-25T09:00:00"), ("XI-001", "2026-08-25T10:00:00")):
                conn.execute("UPDATE event SET start_ts = ? WHERE tag = ?", (start, tag))
        want = ["SI-507 드리프트", "XI-001 드리프트", "FI-602 드리프트"]
        part = self._detail(server.view_shift(self.SID[0]))
        self.assertEqual(sorted(want, key=part.index), want, "초안 화면이 처음 감지된 시각 순이 아니다")
        self._approve(ids)
        part = self._detail(server.view_shift(self.SID[0]))
        self.assertEqual(sorted(want, key=part.index), want, "확정 일지가 처음 감지된 시각 순이 아니다")

    def test_confirmed_log_has_a_folded_trend_per_item(self):
        """확정 일지에 추이가 없었다(지휘자 실측 — 전체 그래프 0). 항목마다 넣되 기본은 접고 「<태그> 추이 보기」로 편다(JS 없이).
        일지는 읽는 문서라 기본으로 펴 두면 길어진다. 초안 화면은 펼친 채 둔다."""
        ids = self._seed()
        import server
        self._approve(ids, adopted=ids[:2])       # 채택 둘 · 제외 하나 — 제외 항목에도 추이를 붙인다
        part = self._detail(server.view_shift(self.SID[0]))
        for tag in self.TAGS:
            self.assertTrue(f"{tag} 추이 보기" in part, f"{tag} 추이 보기 버튼이 없다")
        self.assertEqual(part.count('class="trbox"'), len(self.TAGS), "접기 체크박스는 항목마다")
        self.assertEqual(part.count("<svg"), len(self.TAGS), "펼치면 그래프가 있다")
        self.assertTrue(".trbox:checked~.trwrap{display:block}" in server.STYLE.replace(" ", ""),
                        "기본은 접혀 있고 체크하면 편다")

    def test_stored_curve_draws_after_raw_is_gone(self):
        """전체 곡선을 원본에서 그때그때 읽어, 원본이 3일 보관 뒤 회전으로 지워지면 오래된 확정 일지는 그래프를 영영 못 그렸다.
        확정 때 항목에 1분 평균 곡선을 저장한다 — 원본을 지운 뒤에도 저장된 곡선으로 그려야 한다."""
        ids = self._seed()
        import db
        import server
        self._approve(ids)
        with db.connect() as conn:
            conn.execute("DELETE FROM raw_sample")
            stored = [it.get("curve") for it in db.load_draft(conn, self.SID[0])["items"]]
        self.assertTrue(stored and all(stored), "확정 때 모든 항목에 곡선이 저장돼야 한다")
        self.assertLessEqual(max(len(c["v"]) for c in stored), 720, "12시간 1분 평균 = 720점 이하")
        self.assertEqual(self._detail(server.view_shift(self.SID[0])).count("<svg"), len(self.TAGS),
                         "원본이 없어도 저장된 곡선으로 그린다")

    def test_no_curve_and_no_raw_means_no_trend_button(self):
        """저장된 곡선도 원본도 없으면 「추이 보기」 버튼을 그리지 않는다 — 빈 그래프·오류 없이 근거만 보인다."""
        ids = self._seed(raw=False)
        import server
        self._approve(ids)
        body = server.view_shift(self.SID[0])
        part = self._detail(body)
        self.assertFalse("추이 보기" in part, "그릴 곡선이 없으면 버튼도 없다")
        self.assertFalse("<svg" in part, "빈 그래프도 없다")
        self.assertTrue("변화율" in part, "근거는 그대로 보인다")

    def test_backfill_tool_fills_old_confirmed_records_while_raw_remains(self):
        """이 변경 전에 확정된 기록은 곡선이 없다. 원본이 남은 근무는 도구로 채우고, 원본이 이미 지워진 항목은 센다."""
        ids = self._seed()
        import db
        self._approve(ids)
        with db.connect() as conn:
            conn.execute("UPDATE draft_item SET curve_json = NULL")     # 옛 기록 흉내
        sys.path.append(os.path.join(ROOT, "tools"))
        sys.modules.pop("backfill_curves", None)
        import backfill_curves
        got = backfill_curves.backfill()
        self.assertEqual((got["shifts"], got["filled"], got["no_raw"]), (1, len(ids), 0), f"원본이 있으면 전부 채운다: {got}")
        with db.connect() as conn:
            self.assertTrue(all(it.get("curve") for it in db.load_draft(conn, self.SID[0])["items"]))
            conn.execute("UPDATE draft_item SET curve_json = NULL")
            conn.execute("DELETE FROM raw_sample")
        got = backfill_curves.backfill()
        self.assertEqual((got["filled"], got["no_raw"]), (0, len(ids)), f"원본이 없으면 못 채운 수를 센다: {got}")

    def test_cli_approve_adds_missing_columns_before_approving(self):
        """곡선 칸(curve_json)이 없는 옛 DB 에 명령줄로 승인하면 곡선 저장에서 「no such column」 으로 승인 전체가 되돌아갔다(반증 워커 재현).
        배포(rsync) 뒤 서버를 다시 켜기 전에 명령줄 승인이 먼저 돌 수 있다 — 교대 타이머의 `cli.py run` 처럼 명령이 스스로 칸을 맞춘다."""
        self._seed()
        import sqlite3
        import subprocess
        c = sqlite3.connect(os.environ["ENGRA_DB"])
        c.execute("ALTER TABLE draft_item DROP COLUMN curve_json")          # 이 변경 전 DB 흉내
        c.close()
        r = subprocess.run([sys.executable, os.path.join(ROOT, "app", "cli.py"), "approve", self.SID[0], "--all", "--status", "완료"],
                           capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(r.returncode, 0, f"옛 DB 에서도 승인돼야 한다: {(r.stdout + r.stderr).strip()[-300:]}")
        import db
        with db.connect() as conn:
            self.assertIsNotNone(db.load_handover(conn, self.SID[0]), "확정 일지가 남아야 한다")
            self.assertTrue(all(it.get("curve") for it in db.load_draft(conn, self.SID[0])["items"]), "칸을 더한 뒤 곡선도 저장한다")

    # 곡선 자리수 계산이 못 받는 원본 두 가지 — 폭이 부동소수 범위를 넘어 무한대가 되는 ±1e308, 폭을 200 으로 나누면 0 이 되는 비정규 소수
    EXTREME = {"폭 무한대 ±1e308": [1e308, -1e308, 5.0], "폭이 0 으로 사라지는 비정규 소수": [5e-324, 1e-323, 0.0]}

    def _extreme_raw(self, vals):
        """FI-602 원본에 vals 를 1분 간격으로 넣는다. 다른 태그는 원본 없음."""
        ids = self._seed(raw=False)
        import db
        with db.connect() as conn:
            conn.executemany("INSERT INTO raw_sample (tag, ts, value) VALUES (?,?,?)",
                             [("FI-602", f"2026-08-25T07:{k:02d}:00", v) for k, v in enumerate(vals)])
        return ids

    def test_extreme_raw_values_do_not_undo_approval(self):
        """곡선 폭이 무한대면 자리수 계산이 OverflowError 를 내 승인 전체가 되돌아갔다(반증 워커 재현). 폭이 비정규 소수만큼 작으면
        같은 자리의 log10 이 ValueError 를 내 역시 되돌아갔다(그 수정 뒤 탐침으로 찾음). 곡선을 못 만들면 곡선만 빼고
        승인·확정 일지는 그대로 간다. 저장한 곡선에는 유한한 값만 있다."""
        import math
        for name, vals in self.EXTREME.items():
            with self.subTest(name):
                ids = self._extreme_raw(vals)
                import db
                import server
                self._approve(ids)
                with db.connect() as conn:
                    self.assertIsNotNone(db.load_handover(conn, self.SID[0]), "승인이 되돌아가지 않는다")
                    items = db.load_draft(conn, self.SID[0])["items"]
                self.assertEqual([it["adopted"] for it in items], [1] * len(ids))
                self.assertTrue(all(math.isfinite(v) for it in items if it.get("curve") for v in it["curve"]["v"]),
                                "저장한 곡선에 무한대가 없다")
                self.assertTrue("FI-602" in self._detail(server.view_shift(self.SID[0])), "확정 일지 화면이 그려진다")

    def test_extreme_raw_values_do_not_break_the_draft_screen(self):
        """초안 화면도 저장된 곡선이 없으면 같은 계산으로 원본 곡선을 그린다 — 같은 오류로 화면이 멈추면 안 된다."""
        for name, vals in self.EXTREME.items():
            with self.subTest(name):
                self._extreme_raw(vals)
                import server
                self.assertTrue("FI-602" in self._detail(server.view_shift(self.SID[0])), "초안 화면이 그려진다")

    def test_migration_skips_a_column_another_process_just_added(self):
        """서버·교대 타이머·명령줄이 칸 없는 옛 DB 에 동시에 init 하면, 칸이 없다고 본 뒤 다른 프로세스가 먼저 더해 ALTER 가
        「duplicate column name」 으로 죽었다(반증 워커 재현: 6개 동시 120회 중 81회). 그 오류만 넘기고 다른 오류는 그대로 올린다.
        시각에 기대지 않게 「칸 확인 → 다른 연결이 먼저 더함 → ALTER」 순서를 그대로 만든다."""
        _fresh_db()
        import sqlite3
        import db
        path = os.environ["ENGRA_DB"]
        c = sqlite3.connect(path)
        c.execute("ALTER TABLE draft_item DROP COLUMN curve_json")
        c.close()

        class Racing:
            """ALTER 바로 앞에서 다른 연결이 같은 칸을 먼저 더한다. fail 을 주면 그 대신 그 오류를 낸다."""
            def __init__(self, conn, fail=None):
                self.conn, self.fail = conn, fail

            def execute(self, sql, *args):
                if sql.startswith("ALTER TABLE draft_item ADD COLUMN curve_json"):
                    if self.fail:
                        raise self.fail
                    other = sqlite3.connect(path)
                    other.execute(sql)
                    other.close()
                return self.conn.execute(sql, *args)

        conn = db.connect()
        try:
            with self.assertRaises(sqlite3.OperationalError, msg="칸 중복 말고 다른 오류는 삼키지 않는다"):
                db._migrate(Racing(conn, fail=sqlite3.OperationalError("disk I/O error")))
            db._migrate(Racing(conn))
            self.assertTrue("curve_json" in {r[1] for r in conn.execute("PRAGMA table_info(draft_item)")})
        finally:
            conn.close()

    def test_concurrent_init_on_an_old_db_all_succeed(self):
        """위 순서를 실제 프로세스로 — 칸 없는 옛 DB 에 6개가 같은 순간 init 해도 전부 성공하고 칸이 생긴다(반증 워커 race.py 를 줄인 것)."""
        import shutil
        import sqlite3
        import subprocess
        import time
        _fresh_db()
        base = os.environ["ENGRA_DB"]
        c = sqlite3.connect(base)
        c.execute("ALTER TABLE draft_item DROP COLUMN curve_json")
        busy = c.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]     # 본 파일만 복사하므로 WAL 을 먼저 옮긴다
        c.close()
        self.assertEqual(busy, 0)
        child = ("import sys, time\nsys.path.insert(0, sys.argv[2])\nimport db\n"
                 "while time.time() < float(sys.argv[1]):\n    pass\ndb.init()\n")
        died = []
        for trial in range(3):
            path = f"{base}.race{trial}"
            shutil.copyfile(base, path)
            start = time.time() + 1.0
            ps = [subprocess.Popen([sys.executable, "-c", child, str(start), os.path.join(ROOT, "app")],
                                   env=dict(os.environ, ENGRA_DB=path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                  for _ in range(6)]
            for p in ps:
                _, err = p.communicate()
                if p.returncode != 0:
                    died.append((err.strip().splitlines() or [f"exit {p.returncode}"])[-1])
            c = sqlite3.connect(path)
            cols = {r[1] for r in c.execute("PRAGMA table_info(draft_item)")}
            c.close()
            for s in ("", "-wal", "-shm"):
                if os.path.exists(path + s):
                    os.remove(path + s)
            self.assertTrue("curve_json" in cols, f"{trial}회차: 칸이 생기지 않았다")
        self.assertEqual(died, [], f"동시에 init 한 프로세스가 {len(died)}개 죽었다: {sorted(set(died))}")

    def test_real_12h_shift_shows_no_exponent_sigma_or_severity(self):
        """합성 데이터로는 엔진·AI 문장이 실제로 만드는 숫자를 못 본다 — 실데이터(12시간 114만 행)로 적재·실행해 그린다.
        σ 는 main 의 통계 기호 제거(2ccd3a8)가 합쳐져야 사라진다."""
        _fresh_db()
        import approve
        import collect
        import db
        import pipeline
        import server
        path = os.path.join(ROOT, "data", "asu_shift_day_12h.csv")
        self.assertTrue(os.path.exists(path), "실데이터 파일이 저장소에 있어야 한다")
        collect.ingest(collect.source_for(path))
        sid = "2026-08-21-day"
        pipeline.run(sid, verbose=False)
        with db.connect() as conn:
            items = db.load_draft(conn, sid)["items"]
            evidence = [r[0] or "" for r in conn.execute("SELECT evidence FROM event WHERE shift_id = ?", (sid,))]
        self.assertTrue(items, "항목이 있어야 이 검사가 뜻이 있다")
        # 가림 없이 엔진이 낸 그대로 — 검출 근거와 조립한 항목의 제목·본문·근거 원문에 지수 표기가 없어야 한다
        raw = evidence + [t or "" for it in items for t in (it["title"], it["body"], it["evidence"])]
        self.assertTrue(len(evidence) > 10, "엔진 이벤트가 있어야 이 검사가 뜻이 있다")
        self.assertEqual([e for t in raw for e in self._exp(t)], [], "엔진·조립이 낸 문장 원문에 지수 표기")
        self.assertTrue(all(it.get("curve") for it in items if it["tag"]), "초안을 만들 때 곡선을 저장한다")
        pending = server.view_shift(sid)
        approve.decide(sid, {it["id"]: {"adopted": True, "status": "완료"} for it in items if it["origin"] != "carried"})
        confirmed = server.view_shift(sid)
        for name, body in (("초안", pending), ("확정", confirmed)):
            self.assertEqual(self._exp(body), [], f"{name}: 지수 표기")
            self.assertFalse("σ" in body, f"{name}: σ 가 남았다")
            self.assertFalse("중요도" in body, f"{name}: 중요도가 남았다")


class SeedSwap(unittest.TestCase):
    def test_swap_replaces_the_baseline_without_its_stale_sidecars(self):
        """기준선(seed.db)에 옛 곁파일(-wal · -shm)이 남아 있으면, 본 파일만 덮어써도 그 WAL 이 새 기준선에 얹힌다 —
        멈춘 프로세스가 뒤늦게 닫히며 옛 행을 새 파일에 써 넣는다(codex 지적). 곁파일을 지우고 임시 파일에서 한 번에 바꾼다."""
        import importlib.util
        import sqlite3
        from pathlib import Path
        d = Path(tempfile.mkdtemp(prefix="seedside_"))
        build, live, seed = d / "seed_build.db", d / "engra.db", d / "seed.db"
        old = sqlite3.connect(seed)                 # 옛 기준선 — 행 3개를 WAL 에 남긴 채 연결을 열어 둔다(멈춘 프로세스)
        old.execute("PRAGMA journal_mode=WAL")
        old.execute("PRAGMA wal_autocheckpoint=0")
        old.execute("CREATE TABLE t (x)")
        old.commit()
        old.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        old.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(3)])
        old.commit()
        self.assertTrue((d / "seed.db-wal").stat().st_size > 0, "옛 곁파일이 있어야 이 시험이 뜻이 있다")
        w = sqlite3.connect(build)                  # 새 빌드 — 행 50개
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("CREATE TABLE t (x)")
        w.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
        w.commit()
        w.close()
        spec = importlib.util.spec_from_file_location("seed_tool_sidecar", os.path.join(ROOT, "tools", "seed.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        tool.swap_in(build, live, seed)
        self.assertEqual(sorted(x.name for x in d.glob("seed.db-*")), [], "교체 뒤 옛 곁파일이 남으면 안 된다")
        old.close()                                  # 멈춘 프로세스가 뒤늦게 닫혀도 새 기준선을 건드리면 안 된다
        c = sqlite3.connect(seed)
        try:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM t").fetchone()[0], 50, "새 기준선의 행이어야 한다")
        finally:
            c.close()

    def test_swap_keeps_writes_that_are_still_in_the_wal(self):
        """seed 가 빌드 DB 를 라이브로 바꿀 때 WAL 에만 남은 마지막 쓰기(승인 대기 근무의 초안·이벤트)가 사라졌다 — 본 파일만
        복사·교체하고 -wal 을 지웠다(2026-09-14 실측: 로그엔 「초안 10 · 승인 대기」 인데 engra.db 엔 그 근무가 없었다, 변경 전 코드도 같음).
        교체 전에 WAL 을 본 파일로 옮겨야 한다."""
        import importlib.util
        import sqlite3
        from pathlib import Path
        d = Path(tempfile.mkdtemp(prefix="seedswap_"))
        build, live, seed = d / "seed_build.db", d / "engra.db", d / "seed.db"
        w = sqlite3.connect(build)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.execute("CREATE TABLE t (x)")
        w.commit()
        w.execute("PRAGMA wal_checkpoint(TRUNCATE)")          # 표는 본 파일에 — 행만 WAL 에 남긴다
        w.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
        w.commit()
        self.assertTrue((d / "seed_build.db-wal").stat().st_size > 0, "쓰기가 WAL 에 남아 있어야 이 시험이 뜻이 있다")
        spec = importlib.util.spec_from_file_location("seed_tool", os.path.join(ROOT, "tools", "seed.py"))
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        try:
            tool.swap_in(build, live, seed)                     # 빌드 쪽 연결이 열려 있어도(seed 가 실제로 그랬다)
        finally:
            w.close()
        for p in (live, seed):
            c = sqlite3.connect(p)
            try:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM t").fetchone()[0], 50, f"{p.name} 에서 WAL 에 있던 행이 사라졌다")
            finally:
                c.close()


class EditAndColors(unittest.TestCase):
    """경모님 지시 — 확정 일지는 「수정」으로 바로 고치고(재검토라는 이름·확인 단계 없이), 화면에 상태 색을 준다.
    중요도 색은 되살리지 않는다(경모님이 없앤 것)."""

    SID = "2026-08-25-day"

    def setUp(self):
        _fresh_db()
        import approve
        import db
        with db.connect() as conn:
            conn.execute("INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) "
                         "VALUES (?, 'day', '2026-08-25T06:00:00', '2026-08-25T18:00:00', 'test', ?)", (self.SID, db.now()))
            did = conn.execute("INSERT INTO draft (shift_id, status, generator, generated_at) VALUES (?,'pending','test',?)",
                               (self.SID, db.now())).lastrowid
            self.iid = conn.execute("INSERT INTO draft_item (draft_id, seq, origin, tag, title, body, evidence, severity) "
                                    "VALUES (?,1,'detected','TI-403','TI-403 드리프트','본문','근거','중')", (did,)).lastrowid
        approve.decide(self.SID, {self.iid: {"adopted": True, "status": "완료"}})

    def _post(self, path, pairs):
        import server
        from urllib.parse import urlencode

        class Fake(server.Handler):
            def __init__(self):
                self.path = path; self.sent = []; self.headers = {}
            def _send(self, code, body, ctype=""): self.sent.append((code, body))
            def send_response(self, code): self.sent.append((code, ""))
            def send_header(self, *a): pass
            def end_headers(self): pass

        h = Fake()
        h._handle_form(urlencode(pairs).encode())
        return h.sent[-1]

    def test_confirmed_log_offers_edit_without_a_confirm_step(self):
        import server
        body = server.view_shift(self.SID)
        self.assertIn('action="/reopen"', body, "고치는 자리는 그대로 쓴다(이력도 그대로 남는다)")
        self.assertRegex(body, r"<button[^>]*>\s*수정\s*</button>", "「수정」 버튼")
        self.assertNotIn("재검토", body, "이름을 바꿨다")
        self.assertNotIn("confirm(", body, "눌러서 바로 고칠 수 있게 — 확인 단계 없음")

    def test_edit_opens_the_editable_screen_and_keeps_the_history(self):
        import db
        import server
        code, _ = self._post("/reopen", [("shift_id", self.SID)])
        self.assertEqual(code, 303)
        with db.connect() as conn:
            self.assertEqual(db.load_draft(conn, self.SID)["status"], "pending", "초안으로 돌아간다")
            self.assertIsNone(db.load_handover(conn, self.SID))
            self.assertEqual(len(db.handover_rounds(conn, self.SID)), 1, "이전 확정본은 이력으로 남는다(기획서 4-3)")
        body = server.view_shift(self.SID)
        self.assertIn('action="/approve"', body, "바로 고칠 수 있는 화면")
        self.assertIn("TI-403", body)

    def _rules(self):
        import re
        import server
        css = re.sub(r"/\*.*?\*/", "", server.STYLE, flags=re.S)
        out = {}
        for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            out.setdefault(sel.strip(), []).append(body)
        return out

    def test_each_state_has_its_own_color(self):
        rules = self._rules()
        want = (".pill.live", ".pill.done", ".pill.going", '.item[data-state="observing"]',
                '.item[data-state="ready"]', '.item[data-state="ai_failed"]')
        seen = {}
        for sel in want:
            with self.subTest(selector=sel):
                hit = [b for k, b in ((k, b) for k, v in rules.items() for b in v) if sel in k]
                self.assertTrue(hit, f"{sel} 규칙이 없다")
                seen[sel] = "".join(hit)
                self.assertRegex(seen[sel], r"(background|border-left|color)\s*:", f"{sel} 에 색이 없다")
        self.assertEqual(len({v for v in seen.values()}), len(want), "상태마다 다른 색이어야 한다")

    def test_sk_red_stays_the_single_accent(self):
        import server
        self.assertLessEqual(server.STYLE.upper().count("#EA002C"), 2,
                             "SK 레드는 --accent 정의(밝은/어두운 화면)에만 — 나머지는 그 토큰을 쓴다")

    def test_dark_screen_redefines_the_tokens(self):
        import re
        import server
        m = re.search(r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{(.*?)\n\}", server.STYLE, re.S)
        self.assertIsNotNone(m, "어두운 화면 규칙이 없다")
        for tok in ("--bg:", "--card:", "--ink:", "--line:"):
            self.assertIn(tok, m.group(1), f"어두운 화면에서 {tok} 를 다시 정하지 않았다")

    def test_graph_draws_in_color(self):
        import server
        svg = server._curve([(i / 10, 10.0 + i) for i in range(11)], "2026-08-25T06:00:00", "2026-08-25T18:00:00",
                            band=(0.2, 0.5), limit=18.0, unit="℃")
        self.assertIn('stroke="var(--chart)"', svg, "선에 색")
        self.assertIn("var(--band)", svg, "감지 구간 음영에 색")
        self.assertIn('stroke="var(--limit)"', svg, "한계선은 선과 다른 색 — 선이 SK 레드가 됐다")
        self.assertNotIn('stroke="var(--accent)"', svg, "선과 한계선이 같은 색이면 구분이 안 된다")


    def test_graph_is_skipped_when_the_range_is_not_finite(self):
        """값이 부동소수 최대치 근처면 여백을 더한 범위가 무한대가 되어 자리수 계산이 멈췄다(탐침 재현) —
        그릴 수 없으면 그래프만 빼고 화면의 나머지는 그대로 간다."""
        import server
        wide = [(0.0, 1.0e308), (0.5, 1.79e308), (1.0, 1.5e308)]
        self.assertEqual(server._curve(wide, "2026-08-25T06:00:00", "2026-08-25T18:00:00"), "",
                         "그릴 수 없으면 빈 문자열 — 오류를 내지 않는다")
        ok = server._curve([(0.0, 10.0), (1.0, 20.0)], "2026-08-25T06:00:00", "2026-08-25T18:00:00")
        self.assertIn("<svg", ok, "보통 값은 그대로 그린다")


class ConfirmedBodyOrder(unittest.TestCase):
    """확정 일지 본문의 번호도 화면과 같은 순서(처음 감지된 시각)로 매긴다 — 엔진이 넘긴 중요도 순서가 본문에만 남아 있었다(반증 A)."""

    SID = "2026-08-25-day"
    ROWS = (("PI-901", "2026-08-25T15:00:00", "상"),      # 엔진 순서 = 중요도 순(늦게 감지된 것이 먼저 온다)
            ("TI-403", "2026-08-25T07:00:00", "중"),
            ("FI-602", "2026-08-25T11:00:00", "중"))

    def setUp(self):
        _fresh_db()
        import db
        self.ids = {}
        with db.connect() as conn:
            conn.execute("INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) "
                         "VALUES (?, 'day', '2026-08-25T06:00:00', '2026-08-25T18:00:00', 'test', ?)", (self.SID, db.now()))
            did = conn.execute("INSERT INTO draft (shift_id, status, generator, generated_at) VALUES (?,'pending','test',?)",
                               (self.SID, db.now())).lastrowid
            for seq, (tag, start, sev) in enumerate(self.ROWS, start=1):
                eid = conn.execute(
                    "INSERT INTO event (shift_id, tag, kind, start_ts, end_ts, severity, score, metrics_json, evidence, detector, created_at) "
                    "VALUES (?,?,'드리프트',?,?,?,1.0,'{}','근거','engine',?)", (self.SID, tag, start, start, sev, db.now())).lastrowid
                self.ids[tag] = conn.execute(
                    "INSERT INTO draft_item (draft_id, event_id, seq, origin, tag, title, body, evidence, severity) "
                    "VALUES (?,?,?,'detected',?,?,'본문','근거',?)", (did, eid, seq, tag, f"{tag} 드리프트", sev)).lastrowid

    def test_body_numbers_follow_the_first_detection_time(self):
        import re
        import approve
        import db
        import server
        r = approve.decide(self.SID, {i: {"adopted": True, "status": "완료"} for i in self.ids.values()})
        numbered = [ln for ln in r["body"].splitlines() if re.match(r"^\d+\. ", ln)]
        self.assertEqual([ln.split()[1] for ln in numbered], ["TI-403", "FI-602", "PI-901"], "본문 번호는 처음 감지된 시각 순")
        seen = re.findall(r"(TI-403|FI-602|PI-901) 드리프트", server.view_shift(self.SID))
        first = list(dict.fromkeys(seen))
        self.assertEqual(first, ["TI-403", "FI-602", "PI-901"], "화면 차례와 같아야 한다")
        with db.connect() as conn:
            self.assertEqual([it["seq"] for it in db.load_draft(conn, self.SID)["items"]], [1, 2, 3], "DB 의 seq 는 그대로 둔다")


class SnapshotLinks(unittest.TestCase):
    longMessage = False

    def test_shift_links_become_static(self):
        """정적 스냅숏(sample/)에 서버 주소가 남으면 죽은 링크다. 손으로 쓴 픽스처의 「‹ 목록으로」만 보다가
        화면에서 그 줄이 사라지자 검사가 공허해졌다(반증 워커) — 실제 화면 출력의 왼쪽 목록 링크로 본다."""
        sys.path.append(os.path.join(ROOT, "tools"))
        _fresh_db()
        import approve, db, server, snapshot
        day, night = snapshot.DAY + "-day", snapshot.DAY + "-night"
        items = {}
        for sid, start, end in ((day, snapshot.DAY + "T06:00:00", snapshot.DAY + "T18:00:00"),
                                (night, snapshot.DAY + "T18:00:00", snapshot.DAY + "T23:59:00")):
            with db.connect() as conn:
                conn.execute("INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at) "
                             "VALUES (?,?,?,?,'test',?)", (sid, sid.rsplit("-", 1)[1], start, end, db.now()))
                did = conn.execute("INSERT INTO draft (shift_id, status, generator, generated_at) "
                                   "VALUES (?,'pending','test',?)", (sid, db.now())).lastrowid
                items[sid] = conn.execute(
                    "INSERT INTO draft_item (draft_id, event_id, seq, origin, tag, title, body, severity) "
                    "VALUES (?, NULL, 1, 'detected', 'TI-403', '항목', '본문', '중')", (did,)).lastrowid
        approve.decide(day, {items[day]: {"adopted": True, "status": "완료"}})   # 주간조만 확정 — handover.html

        out = snapshot.to_static(server._one_page(day), "handover.html")
        self.assertFalse("/shift/" in out, "서버 주소가 남으면 정적본에서 죽은 링크다")
        self.assertFalse('href="/draft"' in out, "/draft 도 남으면 안 된다")
        import re as _re
        self.assertTrue(_re.search(r'class="nrow on"[^>]*href="handover\.html"', out), "고른 근무 줄은 제 파일을 가리킨다")
        self.assertTrue('href="index.html"' in out, "야간 초안 줄은 index.html 로 간다")


if __name__ == "__main__":
    unittest.main(verbosity=2)
