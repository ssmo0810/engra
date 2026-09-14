"""AI 서술 2단계(항목별 동시 서술 → 연속성 판정)의 계약 시험.

2026-09-14 — 5개 묶음 순차(16항목 5분 42초)를 항목 1개 = 호출 1개 동시 실행으로 갈랐다. 이 시험이 지키는 것:
  · 항목 하나라도 끝내 실패하면 전체가 LLMUnavailable — 조용히 문장 틀로 내려가지 않는다 (llm.py 원칙 ①). 빈 문장도 실패다
  · 동시 상한을 지킨다 · 일시 오류는 그 항목만 한 번 더 · 한 항목이 끝내 실패하면 새 호출을 띄우지 않고, 처음 실패를 올린다
  · 연속성(2단계)만 실패하면 항목은 살리되 related 를 비우고 상태 문구에 「연속성 판정 실패」를 명시한다
  · related_idx 는 근무 전체 인덱스로 검증된다. 번호가 어긋난 응답(범위 밖·중복·태그 표기 불일치 — 같은 태그는 #1·#2 로 가름)은
    버리고, 빠진 항목은 연속성 판정에서 통째로 빠진다(가리킨 묶음도 버림) + 상태 문구 (실측에서 9항목 중 1개를 빠뜨린 응답이 왔다)
  · duplicate_of 는 항목을 지우지 않고 related_note 에만 남는다 (승인은 항목 단위)
  · 정지 확인 질문(origin=question)·원본 품질(origin=quality) 항목은 문장을 보존하고, 태그 None 은 related_tags_ai 에 섞이지 않는다

실제 CLI·API 는 부르지 않는다 — `llm._call` 을 바꿔 끼운다.

    python3 -m pytest tests/ -q
    python3 tests/test_llm_parallel.py
"""
import copy
import inspect
import os
import re
import subprocess
import sys
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))
sys.path.insert(0, ROOT)


def _llm():
    os.environ["ENGRA_LLM"] = "cli"
    os.environ.pop("ENGRA_LLM_PARALLEL", None)
    sys.modules.pop("llm", None)
    import llm
    return llm


def _items(n, question=False):
    out = []
    if question:
        out.append({"origin": "question", "tag": None, "event_id": None, "severity": "상",
                    "title": "플랜트를 정지하셨습니까? — 18:00 경 48개 태그가 동시에 이탈",
                    "body": "18:00 부터 10분 안에 …", "evidence": "10분 창 동시 이탈 48/53", "precedents": []})
    for i in range(n):
        out.append({"origin": "detected", "tag": f"TI-{400 + i}", "event_id": 100 + i, "severity": "중",
                    "title": f"원제목 {i}", "body": f"원본문 {i}", "evidence": f"근거 {i}",
                    "metrics": {"duration_min": 10 + i},
                    "precedents": [{"shift_id": "2026-08-25-day", "text": f"조치 {i}"}]})
    return out


def _item_answer(idx, sev="중"):
    return {"idx": idx, "title": f"AI제목 {idx}", "body": f"AI본문 {idx}", "severity": sev,
            "severity_reason": "이유", "handover_worthy": True, "precedent_fit": [0], "precedent_note": "맞음"}


def _idx_of(user):
    return int(user.split("이 중 [", 1)[1].split("]", 1)[0])


class FakeCall(object):
    """llm._call 대역. 스키마로 1단계/2단계를 구분한다. 스레드에서 불리므로 기록은 잠금 아래에서.
    연속성 응답은 프롬프트의 「[n] 태그 T」 줄을 읽어 번호·태그 표기를 그대로 되돌려 준다(실제 모델이 받는 지시와 같다)."""

    def __init__(self, llm, link=None, fail_idx=None, flaky_idx=None, link_fail=None, item_wrong_idx=None):
        self.llm = llm
        self.link = link or {}
        self.fail_idx = fail_idx
        self.flaky_idx = flaky_idx
        self.link_fail = link_fail
        self.item_wrong_idx = item_wrong_idx
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, system, user, schema, timeout=None, stop=None):
        if schema is self.llm.ITEM_SCHEMA:
            idx = _idx_of(user)
            with self.lock:
                self.calls.append(("item", idx))
                nth = self.calls.count(("item", idx))
            if idx == self.fail_idx:
                raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
            if idx == self.flaky_idx and nth == 1:
                raise self.llm.LLMUnavailable("claude CLI 실패 (exit 1): overloaded")
            if idx == self.item_wrong_idx:
                return _item_answer(idx + 99)
            return _item_answer(idx)
        if schema is self.llm.LINK_SCHEMA:
            with self.lock:
                self.calls.append(("link", None))
            if self.link_fail:
                raise self.link_fail
            tags = sorted((int(i), t) for i, t in re.findall(r"^\[(\d+)\] 태그 (\S+)", user, re.M))
            return {"items": [dict({"idx": i, "tag": t, "related_idx": [], "related_note": "", "duplicate_of": -1},
                                   **self.link.get(i, {})) for i, t in tags]}
        raise AssertionError("모르는 스키마")


def _dropping(fake, gone):
    """연속성 응답에서 gone 번호의 행을 빼는 대역 — 실측에서 모델이 항목을 빠뜨린 모양."""
    def call(system, user, schema, timeout=None, stop=None):
        out = fake(system, user, schema, timeout)
        if schema is fake.llm.LINK_SCHEMA:
            out["items"] = [o for o in out["items"] if o["idx"] not in gone]
        return out
    return call


class TwoStage(unittest.TestCase):
    def setUp(self):
        self._env = {k: os.environ.get(k) for k in ("ENGRA_LLM", "ENGRA_LLM_PARALLEL")}
        self.llm = _llm()
        self.shift = {"id": "2026-09-14-day", "window_start": "2026-09-14T06:00:00", "window_end": "2026-09-14T18:00:00"}

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_one_call_per_item_then_one_link_call(self):
        fake = FakeCall(self.llm)
        self.llm._call = fake
        out, st = self.llm.rewrite(self.shift, _items(12))
        self.assertEqual(sorted(i for k, i in fake.calls if k == "item"), list(range(12)), "항목마다 호출 1개")
        self.assertEqual([k for k, _ in fake.calls].count("link"), 1, "연속성 호출은 1회")
        self.assertEqual(fake.calls[-1][0], "link", "연속성은 서술이 전부 끝난 뒤")
        self.assertEqual([o["title"] for o in out], [f"AI제목 {i}" for i in range(12)])
        self.assertNotIn("실패", st)
        self.assertNotIn("빠진", st)

    def test_parallel_cap_is_respected(self):
        """상한 3 이면 3개가 동시에 돌고 넷째는 기다린다. 동시 수는 막힘(Barrier) 안에서 센다 —
        대역 호출은 너무 빨리 끝나서, 막힘을 지난 뒤에 세면 겹침이 안 잡힌다(처음 쓴 시험이 그래서 늘 peak=1 로 실패했다)."""
        os.environ["ENGRA_LLM_PARALLEL"] = "3"
        gate = threading.Barrier(3, timeout=5)
        fake = FakeCall(self.llm)
        lock = threading.Lock()
        seen = {"active": 0, "peak": 0}

        def slow(system, user, schema, timeout=None, stop=None):
            if schema is self.llm.ITEM_SCHEMA:
                with lock:
                    seen["active"] += 1
                    seen["peak"] = max(seen["peak"], seen["active"])
                try:
                    gate.wait()        # 3개가 동시에 들어와야 열린다
                finally:
                    with lock:
                        seen["active"] -= 1
            return fake(system, user, schema, timeout)
        self.llm._call = slow
        out, _ = self.llm.rewrite(self.shift, _items(9))
        self.assertEqual(len(out), 9)
        self.assertEqual(seen["peak"], 3, f"동시 상한 3 을 넘거나 못 채우면 안 된다 (peak={seen['peak']})")

    def test_any_item_failure_fails_the_whole_run(self):
        fake = FakeCall(self.llm, fail_idx=4)
        self.llm._call = fake
        with self.assertRaises(self.llm.LLMUnavailable) as cm:
            self.llm.rewrite(self.shift, _items(8))
        self.assertIn("항목 4", str(cm.exception))
        self.assertEqual(sum(1 for k, i in fake.calls if k == "item" and i == 4), self.llm.ATTEMPTS, "타임아웃은 2회까지만 시도")
        self.assertNotIn("link", [k for k, _ in fake.calls], "서술이 실패했으면 연속성은 부르지 않는다")

    def test_blank_sentences_are_not_success(self):
        """빈 제목·본문을 성공으로 받으면 _merge 가 문장 틀로 채워 AI 가 쓴 것처럼 저장된다 — 조용한 폴백 (Codex 반증)."""
        fake = FakeCall(self.llm)

        def blank(system, user, schema, timeout=None, stop=None):
            out = fake(system, user, schema, timeout)
            if schema is self.llm.ITEM_SCHEMA and out["idx"] == 2:
                out = dict(out, title="  ")
            return out
        self.llm._call = blank
        with self.assertRaises(self.llm.LLMUnavailable) as cm:
            self.llm.rewrite(self.shift, _items(4))
        self.assertIn("항목 2", str(cm.exception))
        self.assertEqual(sum(1 for k, i in fake.calls if k == "item" and i == 2), self.llm.ATTEMPTS)

    def test_transient_error_is_retried_for_that_item_only(self):
        """CLI 오류 한 번(순간 과부하)으로 근무 전체를 날리지 않는다 — 항목마다 부르면 호출이 몇 배라 일시 오류를 만날 확률도 몇 배다."""
        fake = FakeCall(self.llm, flaky_idx=3)
        self.llm._call = fake
        out, st = self.llm.rewrite(self.shift, _items(6))
        self.assertEqual(out[3]["title"], "AI제목 3")
        self.assertEqual(sum(1 for k, i in fake.calls if k == "item" and i == 3), 2)
        self.assertEqual(sum(1 for k, _ in fake.calls if k == "item"), 7, "다른 항목은 다시 부르지 않는다")
        self.assertNotIn("실패", st)

    def test_no_new_attempts_after_another_item_failed(self):
        """항목 0 이 끝내 실패하면, 이미 돌던 항목 1 은 첫 응답이 틀려도 두 번째 시도를 띄우지 않는다(재시도 폭주 방지).
        순서를 결정적으로 만든다 — 항목 0 은 항목 1 이 호출에 들어온 뒤에야 실패하고, 항목 1 은 rewrite 가 실패를 알리는
        say 를 받은 뒤에야 첫 응답(틀린 번호)을 돌려준다. 처음엔 앞의 잠금이 없어서, 항목 1 이 대기열에 있는 동안 항목 0 이
        실패하면 항목 1 이 취소돼 버려 변이 시험 13회 중 11회를 엉뚱하게 실패했다."""
        os.environ["ENGRA_LLM_PARALLEL"] = "2"
        started, failed = threading.Event(), threading.Event()
        lock = threading.Lock()
        calls, waited = [], []

        def call(system, user, schema, timeout=None, stop=None):
            idx = _idx_of(user)
            with lock:
                calls.append(idx)
            if idx == 0:
                waited.append(("항목 1 시작", started.wait(5)))
                raise self.llm.LLMUnavailable("claude CLI 실패 (exit 1)")
            started.set()
            waited.append(("실패 알림", failed.wait(5)))
            return _item_answer(idx + 99)      # 틀린 번호 — 멈춤 신호가 없으면 재시도할 응답

        def say(msg):
            if msg.startswith("AI 서술 실패"):
                failed.set()
        self.llm._call = call
        with self.assertRaises(self.llm.LLMUnavailable) as cm:
            self.llm.rewrite(self.shift, _items(2), say=say)
        self.assertIn("항목 0", str(cm.exception))
        self.assertEqual(sorted(waited), [("실패 알림", True), ("항목 1 시작", True), ("항목 1 시작", True)],
                         "항목 1 이 돌기 시작한 뒤 항목 0 이 실패하고, 실패 알림(say)이 항목 1 의 응답보다 먼저 와야 시험이 성립한다")
        self.assertEqual(calls.count(0), self.llm.ATTEMPTS)
        self.assertEqual(calls.count(1), 1, "다른 항목이 끝내 실패했으면 새 시도를 띄우지 않는다")

    def test_failing_worker_blocks_queued_items_and_the_first_failure_is_raised(self):
        """실패를 본 워커가 직접 멈춤 신호를 켠다 — 메인 스레드가 실패를 알아채기 전에 같은 워커가 대기 항목을 집어 새 호출을
        띄울 수 있었다 (Codex 반증). 틈을 벌리려고 as_completed 를 「전부 끝난 뒤 거꾸로 넘기기」로 바꿔 끼운다.
        그러면 멈춤으로 끝난 항목이 먼저 넘어오는데, 사용자에게는 그 결과가 아니라 처음 실패(항목 0)가 올라가야 한다."""
        os.environ["ENGRA_LLM_PARALLEL"] = "1"
        fake = FakeCall(self.llm, fail_idx=0)
        self.llm._call = fake
        real = self.llm.as_completed

        def late(fs):
            done = list(real(fs))
            time.sleep(0.1)
            for f in sorted(done, key=lambda f: fs[f], reverse=True):
                yield f
        self.llm.as_completed = late
        with self.assertRaises(self.llm.LLMUnavailable) as cm:
            self.llm.rewrite(self.shift, _items(3))
        self.assertIn("항목 0", str(cm.exception), "멈춤으로 그만둔 항목이 아니라 처음 실패한 항목을 올린다")
        self.assertEqual(fake.calls, [("item", 0)] * self.llm.ATTEMPTS, "끝내 실패한 항목의 시도 말고는 호출이 없어야 한다")

    def test_wrong_idx_response_is_retried_then_fails(self):
        fake = FakeCall(self.llm, item_wrong_idx=2)
        self.llm._call = fake
        with self.assertRaises(self.llm.LLMUnavailable):
            self.llm.rewrite(self.shift, _items(5))
        self.assertEqual(sum(1 for k, i in fake.calls if k == "item" and i == 2), self.llm.ATTEMPTS)

    def test_link_failure_keeps_items_and_says_so(self):
        fake = FakeCall(self.llm, link_fail=subprocess.TimeoutExpired(cmd="claude", timeout=1))
        self.llm._call = fake
        out, st = self.llm.rewrite(self.shift, _items(6))
        self.assertEqual([o["title"] for o in out], [f"AI제목 {i}" for i in range(6)], "1단계 결과는 살린다")
        self.assertTrue(all(o["related_tags_ai"] == [] and o["related_note"] == "" for o in out))
        self.assertTrue(all("duplicate_of" not in o for o in out))
        self.assertIn("연속성 판정 실패", st, "상태 문구에 실패를 명시한다 — 숨기지 않는다")
        self.assertEqual([k for k, _ in fake.calls].count("link"), self.llm.ATTEMPTS, "연속성도 2회 시도")

    def test_link_missing_items_mean_no_judgment_and_are_reported(self):
        """실측(9항목)에서 한 항목을 빠뜨린 응답이 두 번 중 두 번 왔다. 그걸 실패로 보면 재시도(54초)를 더 쓰거나 묶음을 통째로 잃는다.
        빠진 항목은 연속성 판정에서 통째로 빠진다 — 그 항목을 가리킨 묶음·중복도 버리고(한쪽에만 남지 않게, Codex),
        빠졌다는 사실은 상태 문구에 적는다."""
        fake = FakeCall(self.llm, link={0: {"related_idx": [1], "related_note": "묶음"},
                                        1: {"related_idx": [0], "related_note": "묶음"},
                                        2: {"related_idx": [4], "related_note": "빠진 항목과 묶음"},
                                        3: {"duplicate_of": 5, "related_note": "빠진 항목의 중복"}})
        self.llm._call = _dropping(fake, (4, 5))
        out, st = self.llm.rewrite(self.shift, _items(6))
        self.assertEqual(out[0]["related_tags_ai"], ["TI-401"])
        self.assertEqual((out[2]["related_tags_ai"], out[2]["related_note"]), ([], ""), "빠진 항목을 가리킨 묶음은 버린다")
        self.assertEqual((out[3]["related_tags_ai"], out[3]["related_note"]), ([], ""), "빠진 항목을 대표로 가리킨 중복도 버린다")
        self.assertEqual(out[4]["related_tags_ai"], [])
        self.assertIn("연속성 판정에서 빠진 항목 2개(TI-404, TI-405)", st)
        self.assertNotIn("연속성 판정 실패", st)
        self.assertEqual([k for k, _ in fake.calls].count("link"), 1, "빠진 항목 때문에 다시 부르지 않는다")

    def test_missing_question_item_is_named_in_status(self):
        fake = FakeCall(self.llm)
        self.llm._call = _dropping(fake, (0,))
        _, st = self.llm.rewrite(self.shift, _items(2, question=True))
        self.assertIn("빠진 항목 1개(확인 질문)", st, "태그 없는 항목을 None 으로 적지 않는다")

    def test_link_with_misaligned_numbers_is_a_failure(self):
        """번호가 어긋난 응답을 부분적으로라도 쓰면 엉뚱한 항목끼리 묶인다 — 통째로 버린다.
        「1부터 세고 끝을 뺌」 은 번호가 전부 범위 안이라 번호만 보면 통과한다 — 되돌려 받은 태그로 잡는다."""
        bends = (("1부터 셈", lambda rows: [dict(o, idx=o["idx"] + 1) for o in rows]),
                 ("한 항목 두 번", lambda rows: rows + [dict(rows[1], related_idx=[3])]),
                 ("1부터 세고 끝을 뺌", lambda rows: [dict(o, idx=o["idx"] + 1) for o in rows[:-1]]),
                 ("items 가 목록이 아님", lambda rows: {"0": rows}))
        for name, bend in bends:
            with self.subTest(name):
                fake = FakeCall(self.llm, link={1: {"related_idx": [2], "related_note": "묶음"}})

                def bent(system, user, schema, timeout=None, stop=None, real=fake.__call__, bend=bend):
                    out = real(system, user, schema, timeout)
                    if schema is self.llm.LINK_SCHEMA:
                        out["items"] = bend(out["items"])
                    return out
                self.llm._call = bent
                out, st = self.llm.rewrite(self.shift, _items(4))
                self.assertIn("연속성 판정 실패", st)
                self.assertTrue(all(o["related_tags_ai"] == [] for o in out))

    def test_same_tag_items_are_told_apart(self):
        """같은 태그가 두 항목이면 태그만으로는 번호 어긋남이 안 잡힌다 (Codex 반증) — 프롬프트와 대조에서 TI-401#1·#2 로 가른다.
        뒤 항목의 판정을 앞 번호로 적고 뒤 항목은 빠뜨린 응답을 버려야 한다."""
        items = _items(4)
        items[2]["tag"] = "TI-401"          # 1·2번이 같은 태그
        fake = FakeCall(self.llm, link={2: {"related_idx": [3], "related_note": "묶음"}})
        prompts = []

        def slip(system, user, schema, timeout=None, stop=None):
            out = fake(system, user, schema, timeout)
            if schema is self.llm.LINK_SCHEMA:
                prompts.append(user)
                rows = out["items"]
                out["items"] = [rows[0], dict(rows[2], idx=1), rows[3]]    # 2번 판정을 1번 자리에 적고 2번은 빠뜨림
            return out
        self.llm._call = slip
        out, st = self.llm.rewrite(self.shift, items)
        self.assertIn("[1] 태그 TI-401#1", prompts[0])
        self.assertIn("[2] 태그 TI-401#2", prompts[0])
        self.assertIn("연속성 판정 실패", st)
        self.assertEqual(out[1]["related_tags_ai"], [], "2번의 묶음이 엉뚱한 1번에 붙으면 안 된다")

    def test_related_idx_spans_whole_shift_not_batch(self):
        # 0번과 11번 — 예전 5개 묶음이면 서로 다른 묶음이라 절대 이어질 수 없던 쌍
        fake = FakeCall(self.llm, link={0: {"related_idx": [11], "related_note": "서지 전조 한 사건"},
                                        11: {"related_idx": [0, 99, -1, 11], "related_note": "같은 사건"}})
        self.llm._call = fake
        out, _ = self.llm.rewrite(self.shift, _items(12))
        self.assertEqual(out[0]["related_tags_ai"], ["TI-411"])
        self.assertEqual(out[0]["related_note"], "서지 전조 한 사건")
        self.assertEqual(out[11]["related_tags_ai"], ["TI-400"], "범위 밖(99, -1)·자기 자신(11)은 버린다")

    def test_duplicate_of_marks_but_never_removes(self):
        fake = FakeCall(self.llm, link={3: {"duplicate_of": 1, "related_note": "같은 태그 같은 시간대"}})
        self.llm._call = fake
        out, _ = self.llm.rewrite(self.shift, _items(5))
        self.assertEqual(len(out), 5, "항목을 지우지 않는다 — 승인은 항목 단위")
        self.assertTrue(all("duplicate_of" not in o for o in out), "저장 스키마에 없는 칸은 내보내지 않는다")
        self.assertEqual(out[3]["related_tags_ai"], ["TI-401"], "대표 항목의 태그를 가리킨다")
        self.assertEqual(out[3]["related_note"], "대표 항목과 같은 사건(중복) — TI-401 「AI제목 1」 · 같은 태그 같은 시간대")
        self.assertEqual(out[1]["related_note"], "")

    def test_duplicate_of_a_question_item_is_ignored(self):
        """확인 질문은 사건이 아니라 중복의 대표가 될 수 없다 — 대표로 가리켜도 「중복」 표시를 달지 않고, 같은 항목의 다른 묶음은 그대로 둔다."""
        fake = FakeCall(self.llm, link={2: {"duplicate_of": 0, "related_idx": [1], "related_note": "정지에 따른 결과"}})
        self.llm._call = fake
        out, _ = self.llm.rewrite(self.shift, _items(2, question=True))
        self.assertEqual(out[2]["related_tags_ai"], ["TI-400"])
        self.assertEqual(out[2]["related_note"], "정지에 따른 결과", "태그 None 이 「대표 항목 None」 으로 새어 나오면 안 된다")

    def test_question_item_keeps_its_sentences_and_none_tag_is_dropped(self):
        """정지 근무: 질문 항목(tag None)이 related 로 가리켜져도 정렬이 죽지 않고, 질문 문장은 AI 가 고치지 않는다."""
        fake = FakeCall(self.llm, link={1: {"related_idx": [0], "related_note": "정지에 따른 결과"},
                                        2: {"related_idx": [0, 1], "related_note": "정지에 따른 결과"}})
        self.llm._call = fake
        items = _items(3, question=True)
        out, st = self.llm.rewrite(self.shift, items)
        self.assertEqual(out[0]["title"], items[0]["title"], "확인 질문의 제목은 그대로")
        self.assertEqual(out[0]["body"], items[0]["body"], "확인 질문의 본문은 그대로")
        self.assertEqual(out[1]["related_tags_ai"], [], "가리킨 것이 태그 없는 질문뿐이면 태그 목록은 빈다")
        self.assertEqual(out[1]["related_note"], "", "화면은 태그가 있을 때만 이유를 보인다 — 가리킬 태그 없는 이유는 남기지 않는다")
        self.assertEqual(out[2]["related_tags_ai"], ["TI-400"], "None 은 빼고 정렬한다")
        self.assertEqual(out[2]["related_note"], "정지에 따른 결과")
        self.assertNotIn("연속성 판정", st, "태그 None 을 되돌려 받은 확인 질문 행도 번호 대조를 통과한다")

    def test_quality_item_keeps_its_sentences(self):
        fake = FakeCall(self.llm)
        self.llm._call = fake
        items = _items(2)
        items[0] = dict(items[0], origin="quality", title="계측 결측 — TI-205 07:09~09:40 (150분)", body="값 없음")
        out, _ = self.llm.rewrite(self.shift, items)
        self.assertEqual((out[0]["title"], out[0]["body"]), (items[0]["title"], items[0]["body"]))
        self.assertEqual(out[1]["title"], "AI제목 1")

    def test_link_prompt_has_times_and_the_titles_shown_on_screen(self):
        items = _items(2, question=True)
        items[1]["metrics"] = dict(items[1]["metrics"], start_ts="2026-09-14T07:10:00", end_ts="2026-09-14T08:45:30")
        told = [_item_answer(0), _item_answer(1), _item_answer(2)]
        p = self.llm._link_prompt(self.shift, items, told, self.llm._tag_labels(items))
        self.assertIn("[1] 태그 TI-400  시각 07:10~08:45", p, "근거 문장엔 시각이 없어 연속성 판정이 시간대를 볼 재료다")
        self.assertIn("[2] 태그 TI-401  시각 -", p)
        self.assertIn("제목: 플랜트를 정지하셨습니까?", p, "확인 질문은 화면에 보이는 원문 제목으로 넘긴다")
        self.assertNotIn("AI제목 0", p)
        self.assertIn("AI제목 1", p, "일반 항목은 1단계가 고친 제목")

    def test_single_item_skips_the_link_call_and_keeps_the_pipeline_contract(self):
        """실시간 모드가 확정 항목을 몇 개씩 rewrite 에 넘긴다(2026-09-14 결정). 항목 하나는 묶을 상대가 없어 연속성 호출을 띄우지 않고,
        pipeline.run 이 쓰는 모양 — rewrite(shift, items, say=None) → (같은 길이·순서의 목록, 상태 문구) — 은 그대로다."""
        params = inspect.signature(self.llm.rewrite).parameters
        self.assertEqual(list(params)[:3], ["shift", "items", "say"], "pipeline.run 이 쓰는 앞 세 인자는 그대로")
        self.assertIsNone(params["context"].default, "맥락 인자는 선택 — 주지 않으면 예전 동작")
        fake = FakeCall(self.llm)
        self.llm._call = fake
        out, st = self.llm.rewrite(self.shift, _items(1))
        self.assertEqual(fake.calls, [("item", 0)], "항목 하나면 연속성 호출을 띄우지 않는다")
        self.assertIsInstance(st, str)
        self.assertNotIn("연속성", st)
        self.assertEqual((len(out), out[0]["title"], out[0]["related_tags_ai"], out[0]["related_note"]), (1, "AI제목 0", [], ""))
        self.assertTrue(all("duplicate_of" not in o for o in out))

    def test_auto_mode_does_not_switch_to_api_after_another_item_failed(self):
        """auto 모드에서 CLI 가 도는 사이 다른 항목이 끝내 실패하면, 그 CLI 가 실패해도 API 로 새로 부르지 않는다
        (검증 재현: 동시 4 에서 멈춤 뒤 API 3회). 항목 0 은 CLI·API 모두 실패, 항목 1 의 CLI 는 실패 알림 뒤에 실패한다."""
        os.environ["ENGRA_LLM"] = "auto"
        os.environ["ENGRA_LLM_PARALLEL"] = "2"
        started, failed = threading.Event(), threading.Event()
        lock = threading.Lock()
        api = []

        def cli(system, user, schema, timeout=None):
            idx = _idx_of(user)
            if idx == 0:
                started.wait(5)
            else:
                started.set()
                failed.wait(5)
            raise self.llm.LLMUnavailable("claude CLI 실패 (exit 1)")

        def api_call(system, user, schema, timeout=None):
            with lock:
                api.append(_idx_of(user))
            raise self.llm.LLMUnavailable("API HTTP 529: overloaded")

        def say(msg):
            if msg.startswith("AI 서술 실패"):
                failed.set()
        self.llm._call_cli, self.llm._call_api = cli, api_call
        key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "test-not-a-real-key"
        try:
            with self.assertRaises(self.llm.LLMUnavailable) as cm:
                self.llm.rewrite(self.shift, _items(2), say=say)
        finally:
            if key is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = key
        self.assertIn("항목 0", str(cm.exception))
        self.assertEqual(api, [0, 0], "멈춤 뒤 항목 1 의 CLI 실패는 API 로 넘어가지 않는다")

    def test_same_tag_row_without_number_is_dropped_not_the_whole_answer(self):
        """같은 태그를 가르려고 붙인 #번호를 모델이 떼고 돌려주면 어느 항목인지 가릴 수 없다 — 그 행만 판정 없음으로 두고 나머지 묶음은 쓴다.
        처음엔 응답 전체를 버려서 호출 2회 뒤 묶음을 통째로 잃었다(검증 재현)."""
        items = _items(4)
        items[2]["tag"] = "TI-401"
        fake = FakeCall(self.llm, link={0: {"related_idx": [3], "related_note": "묶음"}, 3: {"related_idx": [0], "related_note": "묶음"}})

        def strip_number(system, user, schema, timeout=None, stop=None):
            out = fake(system, user, schema, timeout)
            if schema is self.llm.LINK_SCHEMA:
                out["items"] = [dict(o, tag=o["tag"].split("#")[0]) if o["idx"] == 2 else o for o in out["items"]]
            return out
        self.llm._call = strip_number
        out, st = self.llm.rewrite(self.shift, items)
        self.assertEqual(out[0]["related_tags_ai"], ["TI-403"])
        self.assertIn("빠진 항목 1개(TI-401)", st)
        self.assertNotIn("연속성 판정 실패", st)
        self.assertEqual([k for k, _ in fake.calls].count("link"), 1)

    def test_tag_echo_tolerates_none_for_dash_and_a_description_suffix(self):
        """원본 품질 요약(태그 '-')을 None 으로, 태그에 설명을 붙여 돌려줘도 번호 어긋남이 아니다 — 응답 전체를 버리지 않는다."""
        items = _items(3)
        items[0] = dict(items[0], origin="quality", tag="-", title="산발 결측 — 여러 태그", body="값 없음")
        fake = FakeCall(self.llm, link={1: {"related_idx": [2], "related_note": "묶음"}})

        def loose(system, user, schema, timeout=None, stop=None):
            out = fake(system, user, schema, timeout)
            if schema is self.llm.LINK_SCHEMA:
                out["items"] = [dict(o, tag={0: "None", 1: "TI-401 (A탑 온도)"}.get(o["idx"], o["tag"])) for o in out["items"]]
            return out
        self.llm._call = loose
        out, st = self.llm.rewrite(self.shift, items)
        self.assertNotIn("연속성 판정", st)
        self.assertEqual(out[1]["related_tags_ai"], ["TI-402"])

    def test_note_on_screen_has_no_hash_number_or_index(self):
        """프롬프트에만 있는 #번호·[n] 이 이유 문장으로 화면에 새지 않는다."""
        fake = FakeCall(self.llm, link={0: {"related_idx": [1], "related_note": "TI-401#2 와 [1] 같은 시각 — 한 사건"}})
        self.llm._call = fake
        out, _ = self.llm.rewrite(self.shift, _items(2))
        self.assertEqual(out[0]["related_note"], "TI-401 와 같은 시각 — 한 사건")

    def test_note_is_dropped_with_a_reference_to_a_missing_item(self):
        """없는 항목(응답에서 빠짐)을 가리킨 참조를 버리면 이유 문장도 버린다 — 남기면 화면에 없는 항목을 가리킨다. 남은 묶음 태그는 둔다."""
        fake = FakeCall(self.llm, link={0: {"related_idx": [1, 2], "related_note": "TI-401·TI-402 와 한 사건"}})
        self.llm._call = _dropping(fake, (2,))
        out, _ = self.llm.rewrite(self.shift, _items(3))
        self.assertEqual((out[0]["related_tags_ai"], out[0]["related_note"]), (["TI-401"], ""))

    def test_context_items_are_linked_but_never_rewritten(self):
        """실시간 모드: 확정될 때마다 새 항목만 서술하고, 이미 화면에 나간 항목(같은 근무 앞 항목·이월 항목)은 맥락으로만 받는다.
        새 항목이 1개여도 맥락이 있으면 연속성 호출을 하고, 연결은 맥락 항목의 태그를 가리킨다. 맥락 항목은 한 글자도 바뀌지 않는다."""
        context = [dict(_items(1)[0], tag="TI-500", title="앞 항목 A", body="이미 나간 문장 A", related_tags_ai=[], related_note=""),
                   dict(_items(1)[0], tag="TI-501", title="앞 항목 B", body="이미 나간 문장 B", related_tags_ai=["TI-500"], related_note="앞 묶음")]
        before = copy.deepcopy(context)
        fake = FakeCall(self.llm, link={0: {"related_idx": [2], "related_note": "맥락 행은 판정으로 쓰지 않는다"},
                                        2: {"related_idx": [0], "duplicate_of": 1, "related_note": "앞 항목과 같은 사건"}})
        prompts = []

        def spy(system, user, schema, timeout=None, stop=None):
            if schema is self.llm.LINK_SCHEMA:
                prompts.append(user)
            return fake(system, user, schema, timeout)
        self.llm._call = spy
        out, st = self.llm.rewrite(self.shift, _items(1), context=context)
        self.assertEqual(context, before, "맥락 항목의 문장·필드는 다시 쓰지 않는다")
        self.assertEqual(len(out), 1, "돌려주는 것은 새 항목뿐")
        self.assertEqual([k for k, _ in fake.calls].count("link"), 1, "새 항목이 1개여도 맥락이 있으면 연속성 호출")
        self.assertIn("[0] 태그 TI-500", prompts[0])
        self.assertIn("[2] 태그 TI-400", prompts[0])
        self.assertEqual(out[0]["related_tags_ai"], ["TI-500", "TI-501"])
        self.assertEqual(out[0]["related_note"], "대표 항목과 같은 사건(중복) — TI-501 「앞 항목 B」 · 앞 항목과 같은 사건")
        self.assertNotIn("연속성 판정", st)

    def test_links_outside_context_and_new_items_are_rejected(self):
        """맥락 + 새 항목 밖을 가리키는 번호는 버리고(이유 문장도), 입력에 없는 태그 표기를 되돌린 응답은 통째로 버린다."""
        context = [dict(_items(1)[0], tag="TI-500", title="앞 항목 A"), dict(_items(1)[0], tag="TI-501", title="앞 항목 B")]
        with self.subTest("범위 밖 번호"):
            fake = FakeCall(self.llm, link={2: {"related_idx": [0, 7], "duplicate_of": 9, "related_note": "밖을 가리킴"}})
            self.llm._call = fake
            out, st = self.llm.rewrite(self.shift, _items(1), context=context)
            self.assertEqual((out[0]["related_tags_ai"], out[0]["related_note"]), (["TI-500"], ""))
            self.assertNotIn("연속성 판정 실패", st)
        bends = (("새 항목 행이 입력에 없는 태그", lambda rows: [dict(o, tag="TI-999") if o["idx"] == 2 else o for o in rows]),
                 ("맥락 행이 다른 태그", lambda rows: [dict(o, tag="TI-400") if o["idx"] == 0 else o for o in rows]))
        for name, bend in bends:
            with self.subTest(name):
                fake = FakeCall(self.llm, link={2: {"related_idx": [0], "related_note": "묶음"}})

                def bent(system, user, schema, timeout=None, stop=None, real=fake.__call__, bend=bend):
                    out = real(system, user, schema, timeout)
                    if schema is self.llm.LINK_SCHEMA:
                        out["items"] = bend(out["items"])
                    return out
                self.llm._call = bent
                out, st = self.llm.rewrite(self.shift, _items(1), context=context)
                self.assertIn("연속성 판정 실패", st)
                self.assertEqual(out[0]["related_tags_ai"], [])

    def test_without_context_the_result_is_unchanged(self):
        """맥락을 주지 않으면(생략·None·빈 목록) 예전과 같다 — 연속성 프롬프트·결과·상태 문구가 같고, 항목 1개면 연속성 호출이 없다."""
        results = []
        for kw in ({}, {"context": None}, {"context": []}):
            fake = FakeCall(self.llm, link={0: {"related_idx": [1], "related_note": "묶음"}})
            prompts = []

            def spy(system, user, schema, timeout=None, stop=None, fake=fake, prompts=prompts):
                if schema is self.llm.LINK_SCHEMA:
                    prompts.append(user)
                return fake(system, user, schema, timeout)
            self.llm._call = spy
            out, st = self.llm.rewrite(self.shift, _items(3), **kw)
            results.append((out, st, prompts))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])
        self.assertNotIn("이미 화면에 나간", results[0][2][0], "맥락이 없으면 프롬프트에 맥락 줄이 없다")
        fake = FakeCall(self.llm)
        self.llm._call = fake
        self.llm.rewrite(self.shift, _items(1), context=[])
        self.assertEqual(fake.calls, [("item", 0)])

    def test_parallel_cap_is_shared_by_overlapping_rewrites(self):
        """실시간 모드는 rewrite 를 겹쳐 부른다 — 상한은 호출마다가 아니라 프로세스 전체다(상한 3 이 두 호출에 따로 걸리면 6).
        대역 호출마다 넷째가 들어오기를 0.3초 기다려 겹칠 기회를 최대로 준다. 항목 2개짜리 호출은 먼저 연속성 호출로 넘어가므로
        연속성 호출이 자리표 밖이면 그것도 드러난다."""
        os.environ["ENGRA_LLM_PARALLEL"] = "3"
        fake = FakeCall(self.llm)
        cond = threading.Condition()
        seen = {"active": 0, "peak": 0}

        def crowded(system, user, schema, timeout=None, stop=None):
            with cond:
                seen["active"] += 1
                seen["peak"] = max(seen["peak"], seen["active"])
                cond.notify_all()
                cond.wait_for(lambda: seen["active"] > 3, timeout=0.3)
                seen["active"] -= 1
            return fake(system, user, schema, timeout)
        self.llm._call = crowded
        errors = []

        def run(n):
            try:
                self.llm.rewrite(self.shift, _items(n))
            except Exception as exc:   # 시험 스레드의 예외는 메인 스레드에서 확인한다
                errors.append(exc)
        threads = [threading.Thread(target=run, args=(n,)) for n in (2, 6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        self.assertEqual(errors, [])
        self.assertLessEqual(seen["peak"], 3, f"두 호출을 합친 동시 외부 호출 수가 상한을 넘었다 (peak={seen['peak']})")
        self.assertEqual((sum(1 for k, _ in fake.calls if k == "item"), [k for k, _ in fake.calls].count("link")), (8, 2))

    def test_item_prompt_carries_a_context_summary_only_when_context_is_given(self):
        """맥락이 있으면 1단계 항목 프롬프트에도 이미 나간 항목의 태그·시각·중요도·제목을 한 줄씩 — 중요도 판단의 교차 참조를 되살린다.
        맥락이 없으면 프롬프트가 예전과 같고, 맥락 요약은 항목 서술 부분 뒤에 붙을 뿐이다."""
        context = [dict(_items(1)[0], tag="TI-500", title="앞 항목 A", severity="상",
                        metrics={"start_ts": "2026-09-14T07:10:00", "end_ts": "2026-09-14T08:00:00"})]
        fake = FakeCall(self.llm)
        prompts = []

        def spy(system, user, schema, timeout=None, stop=None):
            if schema is self.llm.ITEM_SCHEMA:
                prompts.append(user)
            return fake(system, user, schema, timeout)
        self.llm._call = spy
        self.llm.rewrite(self.shift, _items(1))
        self.llm.rewrite(self.shift, _items(1), context=[])
        self.llm.rewrite(self.shift, _items(1), context=context)
        self.assertEqual(prompts[0], prompts[1], "맥락이 없으면 예전 프롬프트 그대로")
        self.assertNotIn("이미 화면에 나간", prompts[0])
        self.assertTrue(prompts[2].startswith(prompts[0] + "\n"), "항목 서술 부분은 같고 맥락 요약은 뒤에 붙는다")
        self.assertIn("  - 태그 TI-500  시각 07:10~08:00  중요도 상  제목: 앞 항목 A", prompts[2])

    def test_link_with_an_empty_row_list_means_every_item_is_missing(self):
        """연속성 응답이 구조는 맞는 빈 목록({"items": []})이면 전 항목이 빠진 것이다 — 다시 부르지 않고 판정 없음 + 상태 문구.
        처음엔 「빈 응답」으로 재시도한 뒤 「연속성 판정 실패」로 적었다 (Codex 반증). 진짜 빈 출력은 _call 이 예외로 올린다."""
        fake = FakeCall(self.llm)

        def empty(system, user, schema, timeout=None, stop=None):
            out = fake(system, user, schema, timeout)
            if schema is self.llm.LINK_SCHEMA:
                out["items"] = []
            return out
        self.llm._call = empty
        out, st = self.llm.rewrite(self.shift, _items(3))
        self.assertIn("연속성 판정에서 빠진 항목 3개(TI-400, TI-401, TI-402)", st)
        self.assertNotIn("연속성 판정 실패", st)
        self.assertEqual([k for k, _ in fake.calls].count("link"), 1, "빈 목록 때문에 다시 부르지 않는다")
        self.assertTrue(all(o["related_tags_ai"] == [] for o in out))

    def test_final_failure_stops_before_the_slot_is_released(self):
        """다른 rewrite 가 자리 하나를 쥐고 있어 이 호출이 자리 하나로 도는 상황 — 한 항목의 마지막 시도가 실패하면 자리를 놓기 전에
        멈춤 신호가 켜져야 한다. 놓은 뒤에 켜면 그 틈에 기다리던 다른 항목이 자리를 얻어 새 외부 호출을 띄운다 (Codex 반증).
        틈을 벌리려고 자리표가 놓일 때 0.2초 쉬게 바꿔 끼우고, 먼저 자리를 얻은 항목은 다른 항목이 기다리기 시작한 뒤에 실패한다."""
        os.environ["ENGRA_LLM_PARALLEL"] = "2"
        self.llm.ATTEMPTS = 1
        real = self.llm._gate()
        real.acquire()                                  # 다른 rewrite 가 쥔 자리
        lock, log, first = threading.Lock(), [], []
        second_waiting = threading.Event()
        entered = {"n": 0}

        class SlowRelease(object):
            def __enter__(self_):
                with lock:
                    entered["n"] += 1
                    if entered["n"] == 2:
                        second_waiting.set()
                real.acquire()

            def __exit__(self_, *exc):
                real.release()
                time.sleep(0.2)
                return False
        self.llm._gate = lambda: SlowRelease()

        def call(system, user, schema, timeout=None, stop=None):
            idx = _idx_of(user)
            with lock:
                if not first:
                    first.append(idx)
                log.append(("start", idx))
            if idx == first[0]:
                second_waiting.wait(5)
                with lock:
                    log.append(("fail", idx))
                raise self.llm.LLMUnavailable("claude CLI 실패 (exit 1)")
            return _item_answer(idx)
        self.llm._call = call
        try:
            with self.assertRaises(self.llm.LLMUnavailable):
                self.llm.rewrite(self.shift, _items(2))
        finally:
            real.release()
        self.assertTrue(second_waiting.is_set(), "다른 항목이 자리를 기다려야 시험이 성립한다")
        fail_at = [i for i, e in enumerate(log) if e[0] == "fail"][-1]
        self.assertEqual([e for e in log[fail_at + 1:] if e[0] == "start"], [], f"마지막 실패 뒤에 새 외부 호출이 시작됐다: {log}")

    def test_off_and_empty_do_not_call(self):
        fake = FakeCall(self.llm)
        self.llm._call = fake
        self.assertEqual(self.llm.rewrite(self.shift, [])[0], [])
        os.environ["ENGRA_LLM"] = "off"
        items = _items(2)
        out, st = self.llm.rewrite(self.shift, items)
        self.assertIs(out, items)
        self.assertIn("미연결", st)
        self.assertEqual(fake.calls, [])

    def test_bad_parallel_setting_fails_loudly(self):
        self.llm._call = FakeCall(self.llm)
        for bad in ("0", "abc"):
            os.environ["ENGRA_LLM_PARALLEL"] = bad
            with self.assertRaises(self.llm.LLMUnavailable):
                self.llm.rewrite(self.shift, _items(2))

    def test_item_schema_has_no_numbers_and_no_related(self):
        props = self.llm.ITEM_SCHEMA["properties"]
        self.assertNotIn("related_idx", props, "묶음 판정은 2단계로 옮겼다")
        for k, v in props.items():
            self.assertNotIn(v.get("type"), ("number",), f"{k}: 수치 필드는 모델을 지나가지 않는다")
        row = self.llm.LINK_SCHEMA["properties"]["items"]["items"]
        self.assertIn("duplicate_of", row["properties"])
        self.assertIn("tag", row["required"], "번호 어긋남을 가려내는 태그 표기를 반드시 되돌려 받는다")


class PipelineHandsEventTimes(unittest.TestCase):
    """근거 문장엔 시각이 없다 — pipeline 이 대표 이벤트의 start_ts·end_ts 를 metrics 에 얹어 rewrite 에 넘겨야
    연속성 판정이 근무 전체의 시간대를 대조한다. metrics 는 rewrite 뒤에 떼므로 저장 스키마는 그대로다."""

    def test_run_puts_event_times_into_metrics_before_rewrite(self):
        sys.modules.pop("pipeline", None)
        import pipeline
        src = inspect.getsource(pipeline.run)
        at = src.index('start_ts=e.get("start_ts"), end_ts=e.get("end_ts")')
        self.assertLess(at, src.index("llm.rewrite("))
        self.assertLess(src.index("llm.rewrite("), src.index('it.pop("metrics", None)'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
