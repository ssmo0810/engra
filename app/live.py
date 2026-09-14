"""실시간 누적 — 재생 시계 · 1분 묶음 적재 · 5분 틱 주 엔진 · 문제별 추적기 · 마감 동기화 · 문제별 AI.

현장에서 바뀌는 것은 수집기 어댑터 하나다 — RTDB 에서 「마지막 시각 이후」를 읽기 전용으로 받는다.
재생은 그 입구만 생성기 CSV(이어진 근무 체인)로 바꾼 것이고, 적재·검출·추적·AI·초안 저장은 같은 코드를 탄다.
근거: 틱 실험 probe/probe-live tmp_live/RESULTS.md 「추가 실험 — 최근 12시간 창 · 문제별 추적기」.

공개 계약 (server.py 가 부른다 — 바꾸기 전에 지휘자·live 워커에게 먼저 알린다)
=====================================================================

모든 함수는 스레드 안전하다. 사용자 잘못(이름·순서·재생 중 등)은 ValueError(문장 그대로 400 으로), 그 밖의 예외는 버그다.
status()·items()·values()·approve_lock() 은 예외를 내지 않는다.

start(csv_names, speed=100, prev_csv_name=None, replace_unconfirmed=False) -> status()
    이어진 근무 CSV(근무 하나짜리 생성기 CSV)들을 stop 할 때까지 끊김 없이 재생한다. 하나만 줘도 된다. 바로 돌아온다.
    - 이름만 받는다: 업로드 폴더(jobs.UPLOAD_DIR) 안의 .csv 파일 이름. 경로 구분자·'..'·폴더 밖으로 풀리는 이름·없는 파일은 ValueError.
    - 근무 = 파일 첫 행 시각의 근무. 앞 근무 끝 = 뒤 근무 시작으로 이어져야 한다(아니면 ValueError).
    - 한 근무의 마감 틱(06/18시)에서 그 근무를 마감 동기화하고(마감 AI 는 뒤에서 계속 돈다), 같은 시계로 다음 근무를 곧바로 쌓는다.
    - 마지막 근무 마감까지 가면 시계가 멈춘다 — phase "ended" · phase_note 「재생 데이터 끝」. 마감 AI 는 끝까지 마친다.
    - prev_csv_name: 첫 근무의 바로 앞 근무 CSV 이름(선택). 적재 규칙(collect.ingest + 품질 기록 + 요약)으로 먼저 적재한 뒤 시계를 켠다.
    - 거부: 재생 중 · 확정된 근무가 섞임 · 승인 대기 초안이 있음(replace_unconfirmed=False) · 적재·일괄 실행·리셋이 도는 중.
    - replace_unconfirmed=True(데모 재촬영): 확정 안 된 초안(pending·live)은 그 근무를 쌓기 시작할 때 지우고 다시 쌓는다.
      확정 초안은 어떤 경우에도 지우지 않는다. 같은 근무의 'live' 초안(멈춘·중단된 재생)은 옵션 없이도 다시 쌓는다.
stop() -> status()
    곧바로 멈춘다. 시계·틱을 멈추고, 줄에 선 AI 작업은 버리고, 이미 돌던 AI 호출은 돌아와도 DB 에 쓰지 않는다(재생마다 정지 표시를
    DB 쓰기 잠금 안에서 확인). 기다리는 것은 진행 중인 DB 쓰기 한 번뿐이고, jobs 작업 락은 돌아오기 전에 푼다.
    마감하지 않았거나 마감 AI 가 덜 끝난 근무의 초안은 'live' 로 남아 승인이 잠긴다.
set_speed(speed) -> status()     배속(0 < speed ≤ 1000). 근무 시각은 끊기지 않고 이어진다.
retry(key) -> status()           「AI 서술 실패」 항목 하나를 다시 쓴다(키는 재생 안에서 유일). 실패 상태가 아니거나 멈춘 재생이면 ValueError.
forget() -> status()             지난 재생 기억을 지운다(리셋·재시작 뒤 화면이 옛 재생을 보이지 않게). 재생 중이면 ValueError.
status() -> dict                 폴링용(/api/live)
items(shift_id=None) -> [dict]   그 근무(기본 = 지금 쌓는 근무, 끝난 뒤엔 마지막 근무)의 문제별 항목 전부, 처음 감지 시각순. 이 재생에 없는 근무면 []
values() -> dict                 현재 근무 시각의 태그값(/dcs 숫자) = {"clock": ts|None, "values": {tag: [ts, value]}}
approve_lock(shift_id) -> str|None   승인 버튼 잠금 문구(없으면 None). 서버 강제는 approve.decide 가 한다 — 화면을 우회해도 걸린다.

status() 모양
    {"phase": "idle"|"preparing"|"running"|"ended"|"stopped"|"failed",
     "phase_note": None|"재생 데이터 끝 — …",
     "chain": [shift_id, ...], "files": [이름, ...],
     "shift_id": str|None,             # 지금 쌓는 근무(끝난 뒤엔 마지막 근무). 아래 window~approve_lock 은 이 근무의 것
     "window": [구간 시작, 구간 끝]|None, "file": str|None,
     "clock": ts|None,                 # 근무 시각 = 1분 묶음이 들어간 끝 (체인 전체에 하나)
     "speed": float|None,
     "window_mode": "12h"|"shift_start"|None,    # (나) 최근 12시간 창 / (가) 근무 시작 창
     "window_note": None|"앞 근무 데이터가 모자람 — 1,436점(99%) · 초반 결과가 흔들릴 수 있음",
     "stop_question_from": None|ts,    # (가) 일 때만 — 이 근무 시각 전에는 정지 질문을 띄우지 않는다
     "prev": {"shift_id", "complete", "samples", "tags", "buckets", "filled", "ratio", "first", "last",
              "head_gap_min", "tail_gap_min"}|None,      # 앞 12시간 채움 — 1시간 칸마다 표본이 있나 + 양끝 빈 시간으로 판정
     "tick": {"t", "n", "of", "events", "items", "sec", "late_sec"}|None,
     "counts": {"observing", "writing", "ready", "ai_failed", "vanished", "not_in_close"},
     "stats": {"created", "max_observing", "vanished", "ended", "ended_at_birth", "closed",
               "recurrence", "folded"(원 항목에 접힌 재발),
               "false_confirm"(마감 전 None — 카드 기준. 추적기 기준 값은 마감 로그의 false_confirm_tracks),
               "ai_jobs", "ai_failed"},        # created·ended·closed 는 추적기 기준(접힌 재발 포함)
     "rows": {"appended", "bad", "after_window"(근무 끝 뒤 행이 있었나 — 그 행은 쌓지 않는다)},
     "approve_lock": str|None,
     "closed": [{"shift_id", "draft": "live"|"pending", "approve_lock", "counts", "stats"}, ...],   # 이 재생에서 마감한 근무
     "interrupted": [shift_id],        # 'live' 초안이 남았는데 이 프로세스가 이어 쌓지 않는 근무(재시작·정지·실패)
     "error": str|None}

items() 한 건
    {"key": "p17",                     # 재생 안에서 유일 (retry 인자). 정지 질문 q·원본 품질 g
     "state": "observing"|"writing"|"ready"|"ai_failed"|"vanished",
         # 관찰 중(회색) · 확정돼 AI 서술 중(회색) · 선택 가능 · AI 서술 실패 · 관찰 후 사라짐(접힌 한 줄)
         # (view() 에는 "folded" 도 있지만 items() 는 내보내지 않는다 — 접힌 재발은 원 항목의 근거 줄이다)
     "origin": "detected"|"question"|"quality",
     "confirm": None|"ended"|"closed", # 끝남 확정 / 마감 확정
     "ongoing": None|bool,             # 확정 때 아직 움직이고 있었나 (멤버 끝 시각 > 확정 시각 − M)
     "not_in_close": bool,             # 마감 검출에 없음 — 끝남 확정했는데 마감 틱에 이 tag·kind 가 없다
     "recurrence_of": None|key,        # 기억된 확정 문제가 최근 M분 안에 다시 움직여 새로 만든 문제
     "tag", "kind", "title", "severity",
     "evidence": str,                  # 대표 이벤트 근거 한 줄 = 지금 크기
     "score": float, "score_max": float,
     "start": ts, "end": ts,           # 멤버 중 가장 이른 시작 · 가장 늦은 끝
     "members": [[tag, kind], ...], "related_tags": [tag, ...],
     "recurrences": [ts, ...],         # 이 항목에 접힌 재발이 다시 난 시각 — 근거 줄 「재발 N회 — 06:20 · 09:15」 와 같다
     "stop": None|{"at": ts, "tags": int, "first": [tag, ...]},   # 정지 질문 카드 — 매 틱 엔진의 현재 추정(시각 · 동시에 벗어난 태그 수)
     "first_seen": ts, "confirmed": ts|None,   # 근무 시각
     "ai_sec": float|None,             # 확정 → AI 완료 실제 초 (내부 확인용)
     "draft_item_id": int|None,        # ready 일 때 — 선택·상태·코멘트·승인은 기존 폼 그대로 이 id 로
     "error": str|None}

항목 상태의 뜻
- 관찰 중: 추적기가 붙어 매 틱 근거를 갱신한다. 초안 표에는 없다(메모리).
- 서술 중: 끝남·마감 확정 뒤 AI 작업이 도는 중. 화면에는 관찰 중과 같은 회색이다.
- 선택 가능: AI 가 다 써서 draft_item 에 들어갔다. 확정 뒤에는 다시 쓰지 않는다.
- AI 서술 실패: 조용히 넘기지 않는다 — 초안에 넣지 않고 이 상태로 남기며, 남아 있는 동안 승인이 잠긴다.
- 사라짐: 확정 전에 주 엔진 결과에서 K틱 연속 빠졌다. AI 를 부르지 않는다. 같은 동일성이 다시 뜨면 새 추적기다.
- 재발은 관찰 중에는 따로 회색 카드로 보이지만, 확정되거나 확정 전에 사라지는 순간 **원 항목에 접힌다** — 새 초안 항목도 새 AI 호출도
  만들지 않는다(사라진 재발도 실제로 다시 움직인 일이라 원 항목의 끝 시각·근거에 들어가야 한다).
  원 항목의 근거에 「재발 N회 — 시각들」 한 줄이 붙고 끝 시각이 마지막 재발까지 늘어난다. 문장은 확정 때 것 그대로다.
  근무자에게 이건 다섯 건이 아니라 「근무 중 5번 반복된 한 건」이기 때문이다. items() 는 접힌 카드를 더 보여 주지 않는다.
- 정지 질문(origin question)은 동일성 하나로 추적한다. 끝남 확정은 없고, 마감 틱에 있으면 마감 확정한다.
  12시간 창에서 정지(Trip) 근무는 검증하지 못했다 — 앞 근무와 이어진 정지 체인 데이터가 없다.

스레드 모델
- 재생 스레드 1개(live-replay): 시계 → 1분 묶음(CSV → 메모리 버퍼 + DB 덧붙이기) → 5분 틱(검출·조립·추적) → 근무마다 마감 동기화 → 다음 근무.
  버퍼·추적기를 고치는 것은 이 스레드뿐이다. 검출(12시간 창 틱당 ~0.7~1초)은 잠금 밖에서 한다.
- AI 작업 스레드 AI_WORKERS 개(live-ai): 확정된 문제를 줄에서 꺼내 _write_ai() 한 곳에서만 llm.rewrite 를 부르고, 끝나면 짧은 트랜잭션으로
  그 근무 초안에 넣는다. 맥락 인자·전역 동시 상한(final/speed-parallel)은 병합 뒤 _write_ai() 에만 붙인다.
- 모듈 잠금 _lock: 메모리 상태를 고치거나 복사하는 동안만 잡는다. 서버 요청은 이것만 잡으므로 검출·AI·DB 를 기다리지 않는다.
- 재생마다 DB 쓰기 잠금(wlock): 이 재생의 DB 쓰기는 전부 이 안에서 정지 표시를 확인한 뒤 쓴다. stop() 이 이 잠금 아래에서 정지를 켠다.
- jobs 작업 락: 재생 동안(준비 ~ 마지막 근무 마감 AI 끝) jobs.hold() 로 쥔다. 그래서 서버의 기존 잠금 검사가 POST /pipeline/run ·
  /reset · 업로드 적재를 막고(적재는 줄을 선다), 서버는 따로 검사 없이 기존 거부 문구를 보여 주면 된다. 정지하면 곧바로 푼다.
  데모 재생 전용이다 — 현장 연속 모드(RTDB 상시 수신)는 작업 락을 쥐는 방식이 아니다.
- 매 틱 jobs.mark_qa_active() — 정시 자동 비움(tools/reset_if_idle.sh)이 재생 도중 DB 를 비우지 않게.

일괄 실행·재시작과의 관계
- 멈춘 재생(stopped · failed · 서버 재시작으로 interrupted)이 남긴 'live' 초안은 일괄 실행(pipeline.run)으로 그대로 대체된다.
- 마감 동기화와 AI 가 끝난 근무의 초안은 'pending' — 일반 승인 대기 초안과 똑같다.
- 마감 때 event 표를 마감 집합(= 배치)에 맞춘다: 확정 항목의 대표 이벤트는 마감 행을 가리키게 다시 잇고(파형은 마감본이 된다),
  마감 검출에 없는 확정 항목의 근거 행만 남긴다. 관리 화면 채점이 일괄 실행과 같아진다.
- 서버 재시작 때 남는 것(DB): 덧붙인 원본(마지막 1분 묶음까지) · 근무 행 · 'live'/'pending' 초안과 선택 가능 항목 · 그 이벤트와 기록(draft_item.live_json).
  사라지는 것(메모리): 시계 · 버퍼 · 추적기(관찰 중·서술 중·실패·사라짐) · 통계 · AI 줄. 재생은 이어가지 않는다.

승인 규칙 (approve.decide 가 강제)
- draft.status = 'live'(마감 전 · 마감 AI 진행 중 · AI 실패 남음 · 멈춤·중단)이면 거부.
- 앞 근무(구간 시작이 더 이른 근무)에 확정되지 않은 초안(pending·live)이 있으면 거부 — 앞 근무의 진행중 항목이 인계에서 빠지지 않게.
"""
import bisect
import itertools
import json
import queue
import statistics
import threading
import time
import traceback
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import collect
import db
import jobs
import llm
import pipeline
import ports

TICK_MIN = 5            # 주 엔진 틱(근무 시계). 12시간 창 틱 ~0.7초라 100배속 3초 간격에 여유 — 1분 틱은 밀린다(실측)
WINDOW_H = 12           # (나) 최근 12시간 창
M_MIN = 30              # 끝남 확정: 모든 멤버 끝 시각이 지금보다 M분 이상 앞
K_TICKS = 3             # 사라짐: 확정 전 K틱 연속 결과에 없음
STOP_Q_BAN_MIN = 60     # (가) 근무 시작 창에서만 — 경과 60분 전 정지 질문 금지(시작 창 10/10 근무 경과 10분 오탐, 실측)
AI_WORKERS = 8
MAX_SPEED = 1000
# 앞 12시간이 온전한지는 db.raw_window_complete(채움률 + 양끝 빈 시간)로 본다 — 배치 재적재 판정과 같은 규칙이다

_TICK = timedelta(minutes=TICK_MIN)
_WAVE_PAD = timedelta(seconds=pipeline.WAVE_PAD_SEC)
_STOP_NOTE = "재생을 정지해 서술하지 않았습니다 — 다시 재생하세요"

_lock = threading.Lock()
_replay = None          # 지금(또는 마지막) 재생
_ai_queue = queue.Queue()
_ai_threads = []
_order = itertools.count()
_clock = time.monotonic  # 재생 시계의 벽시계. 시험이 바꿔 끼운다


def _log(msg):
    print(f"  [live] {msg}", flush=True)


def _iso(t):
    return t.isoformat(timespec="seconds") if t is not None else None


def _speed(speed):
    try:
        s = float(speed)
    except (TypeError, ValueError):
        raise ValueError(f"배속은 숫자여야 합니다. 받은 것: {speed!r}") from None
    if not 0 < s <= MAX_SPEED:
        raise ValueError(f"배속은 0 보다 크고 {MAX_SPEED} 이하입니다. 받은 것: {speed!r}")
    return s


def _upload_path(name):
    """업로드 폴더 안의 CSV 이름 → 경로. 서버는 이름만 넘긴다 — 경로를 받으면 서버의 아무 파일이나 읽게 된다."""
    if not isinstance(name, str) or not name or name in (".", "..") or any(c in name for c in "/\\\x00"):
        raise ValueError(f"업로드 폴더의 파일 이름만 받습니다: {name!r}")
    if not name.lower().endswith(".csv"):
        raise ValueError(f"CSV 파일 이름이어야 합니다: {name!r}")
    base = Path(jobs.UPLOAD_DIR).resolve()
    path = (base / name).resolve()
    if path.parent != base:
        raise ValueError(f"업로드 폴더 밖을 가리키는 이름입니다: {name!r}")
    if not path.is_file():
        raise ValueError(f"업로드 폴더에 없는 파일입니다: {name!r}")
    return path


def _first_row(path):
    rows = collect.source_for(path, collect.BadRows(max_ratio=collect.MAX_BAD_RATIO)).read()
    try:
        row = next(rows, None)
    finally:
        rows.close()            # 첫 행만 읽고 파일을 바로 닫는다 — 체인 CSV 마다 열린 채 남지 않게
    if row is None:
        raise ValueError(f"{path.name} 에 읽을 행이 없습니다.")
    return row


class _Stopped(Exception):
    """정지 — 재생 스레드는 다음 묶음·틱 전에, DB 쓰기는 쓰기 잠금 안에서 이것으로 멈춘다."""


class _StopAware:
    """적재 소스를 감싸 정지를 행 읽기 중에 본다. collect.ingest 는 한 트랜잭션이라 여기서 멈추면 반쪽 원본 없이 되돌아간다."""

    def __init__(self, source, flag):
        self.source, self.flag, self.name = source, flag, source.name

    def read(self):
        for i, row in enumerate(self.source.read()):
            if i % 5000 == 0 and self.flag.is_set():
                raise _Stopped()
            yield row


# --- 메모리 버퍼 --------------------------------------------------------

class _Buffer:
    """흘러 들어온 원본의 메모리 사본 {tag: [(ts, value)]} — 틱마다 DB 에서 12시간 창을 다시 읽으면 1.05초가 더 든다(실측).
    DB 와 같게 (태그, 시각) 하나에 값 하나(INSERT OR REPLACE)를 지킨다."""

    def __init__(self):
        self.pts, self.keys = {}, {}

    def load_sorted(self, rows):
        """(tag, ts, value) 를 태그·시각순으로 받아 한 번에 채운다 — 앞 근무 12시간(약 114만 점)."""
        cur = keys = pts = None
        for tag, ts, value in rows:
            if tag != cur:
                cur, keys, pts = tag, self.keys.setdefault(tag, []), self.pts.setdefault(tag, [])
            keys.append(ts)
            pts.append((ts, value))

    def add(self, tag, ts, value):
        keys, pts = self.keys.setdefault(tag, []), self.pts.setdefault(tag, [])
        if not keys or ts > keys[-1]:
            keys.append(ts)
            pts.append((ts, value))
            return
        j = bisect.bisect_left(keys, ts)
        if j < len(keys) and keys[j] == ts:
            pts[j] = (ts, value)
        else:
            keys.insert(j, ts)
            pts.insert(j, (ts, value))

    def slice(self, lo, hi):
        """[lo, hi) — 태그 이름순. load_series(ORDER BY tag, ts) 와 같은 모양으로 검출에 넘긴다."""
        out = {}
        for tag in sorted(self.keys):
            keys = self.keys[tag]
            i, j = bisect.bisect_left(keys, lo), bisect.bisect_left(keys, hi)
            if j > i:
                out[tag] = self.pts[tag][i:j]
        return out

    def around(self, tag, lo, hi):
        """[lo, hi] 양끝 포함 — pipeline._waveform 이 거르는 범위와 같다."""
        keys = self.keys.get(tag)
        if not keys:
            return []
        return self.pts[tag][bisect.bisect_left(keys, lo):bisect.bisect_right(keys, hi)]

    def trim(self, lo):
        for tag, keys in self.keys.items():
            j = bisect.bisect_left(keys, lo)
            if j:
                del keys[:j]
                del self.pts[tag][:j]


# --- 틱 결과 → 문제 ----------------------------------------------------

class _Problem:
    """틱 결과의 초안 항목 하나와 그 멤버 이벤트(대표가 먼저) — 추적기가 읽는 근거."""

    def __init__(self, item, members, now=None):
        self.item, self.members, self.stop = item, members, None
        if not members:                     # 정지 질문 · 원본 품질 항목
            self.keys, self.minstart, self.maxend, self.kind, self.score = frozenset(), None, None, None, None
            return
        head = members[0]
        self.keys = frozenset((e["tag"], e["kind"]) for e in members)
        try:
            # 규약(ports.validate_events)은 start_ts·end_ts 를 필수로 하지 않는다. 비어 있으면 「지금도 이어지는 중」으로
            # 보고 이 틱 시각을 쓴다 — 조용히 버리면 일괄 실행에는 있는 이벤트가 실시간에서만 사라진다(반증 A).
            self.minstart = min(datetime.fromisoformat(e["start_ts"]) if e.get("start_ts") else now for e in members)
            self.maxend = max(datetime.fromisoformat(e["end_ts"]) if e.get("end_ts") else now for e in members)
        except (TypeError, ValueError) as exc:
            # 끝남·재발 판정이 끝 시각에 달렸다. 시각 없는 이벤트를 조용히 넘기면 판정이 틀어진다.
            raise RuntimeError(f"시각이 없거나 깨진 이벤트 — {head['tag']} {head['kind']}: {exc}") from exc
        self.kind, self.score = head["kind"], head.get("score")


def _order_like_db(events):
    """pipeline 과 같은 번호·순서로 compose 에 넘긴다: 번호 = 검출 순서(save_events 의 INSERT 순), 순서 = load_events 의 start_ts, tag."""
    stored = [dict(e, id=i) for i, e in enumerate(events, start=1)]
    stored.sort(key=lambda e: (e.get("start_ts") or "", e["tag"]))
    return stored


def _problems(items, stored, now=None):
    """compose 결과 → (문제 목록, 정지 질문 항목|None). 멤버 = compose 가 한 항목으로 접은 묶음(group, 없으면 solo-id)의 이벤트.
    now = 이 틱 시각 — 시각이 비어 있는 이벤트를 「지금도 이어지는 중」으로 볼 때 쓴다."""
    def bucket(e):
        return (e.get("metrics") or {}).get("group") or f"solo-{e.get('id')}"

    by_id, groups = {}, {}
    for e in stored:
        by_id[e["id"]] = e
        groups.setdefault(bucket(e), []).append(e)
    probs, question = [], None
    for it in items:
        if it.get("origin") == "question":
            question = it
            continue
        head = by_id.get(it.get("event_id"))
        if head is None:
            raise RuntimeError(f"초안 항목이 가리키는 이벤트가 없습니다 — {it.get('title')} (event_id={it.get('event_id')})")
        probs.append(_Problem(it, [head] + [e for e in groups[bucket(head)] if e is not head], now))
    return probs, question


# --- 문제별 추적기 ------------------------------------------------------

class _Track:
    """문제 하나를 전담하는 추적기(엔진 N). 확정하면 근거(prob)가 멈추고, AI 가 그것을 한 번 쓴다."""

    def __init__(self, key, idx, prob, k, t, origin="detected", recurrence_of=None):
        self.key, self.idx, self.order, self.origin, self.recurrence_of = key, idx, next(_order), origin, recurrence_of
        self.prob, self.keys, self.created = prob, prob.keys, k
        self.miss, self.state, self.attached = 0, "open", False      # state: open | ended | closed | vanished
        self.first_seen, self.score_max = t, prob.score
        self.confirmed = self.conf_wall = self.conf_keys = self.conf_tick = self.ongoing = self.gone = None
        self.not_in_close = False
        self.parent = self.root = None          # 재발을 만든 추적기 · 초안 항목을 가진 추적기
        self.folded, self.recurrences, self.saved_recur, self.ai_severity = False, [], 0, None
        self.ai = None                                                # None | writing | ready | failed
        self.ai_sec = self.draft_item_id = self.error = None
        self.db_events = []

    def see(self, prob):
        self.miss, self.keys, self.prob = 0, prob.keys, prob
        if prob.score is not None:
            self.score_max = prob.score if self.score_max is None else max(self.score_max, prob.score)

    def confirm(self, how, t, k, gap):
        self.state, self.confirmed, self.conf_wall, self.conf_tick = how, t, time.monotonic(), k
        self.conf_keys = self.prob.keys
        self.ongoing = None if self.prob.maxend is None else self.prob.maxend > t - gap
        self.ai = "writing"

    def owner(self):
        """이 문제의 초안 항목을 가진 추적기 — 재발은 원 항목에 접히므로 그 원 추적기다."""
        return self.root if self.root is not None else self

    def end_at(self):
        """마지막 재발까지 늘린 끝 시각."""
        ends = [e for e in [self.prob.maxend] + [r.prob.maxend for r in self.recurrences] if e is not None]
        return max(ends) if ends else None

    def evidence_line(self):
        """감지 근거 — 접힌 재발이 있으면 그 시각을 한 줄로 남긴다(문장은 확정 때 것 그대로)."""
        base = self.prob.item.get("evidence") or ""
        if not self.recurrences:
            return base
        return f"{base}\n재발 {len(self.recurrences)}회 — " + " · ".join(_iso(r.first_seen)[11:16] for r in self.recurrences)

    def severity(self, base=None):
        """접힌 재발까지 합친 중요도 — 더 큰 쪽을 쓴다. base 를 주면 그것(AI 가 판정한 값)을 원 항목 값으로 본다."""
        rank = {"상": 0, "중": 1, "하": 2}
        own = base if base is not None else (self.ai_severity or self.prob.item.get("severity"))
        cands = [s for s in [own] + [r.prob.item.get("severity") for r in self.recurrences] if s in rank]
        return min(cands, key=lambda s: rank[s]) if cands else own

    def members_all(self):
        """접힌 재발까지 합친 멤버 (tag, kind)."""
        out = {(e["tag"], e["kind"]) for e in self.prob.members}
        for r in self.recurrences:
            out |= {(e["tag"], e["kind"]) for e in r.prob.members}
        return out

    def related_all(self):
        """합친 멤버에서 대표 태그를 뺀 나머지 — 함께 움직인 태그."""
        head = self.prob.members[0]["tag"] if self.prob.members else None
        return {tag for tag, _kind in self.members_all()} - {head}

    def score_top(self):
        scores = [s for s in [self.score_max] + [r.score_max for r in self.recurrences] if s is not None]
        return max(scores) if scores else None

    def live(self):
        """draft_item.live_json — 내부 확인용 추적 기록."""
        return {"key": self.key, "first_seen": _iso(self.first_seen), "confirmed": _iso(self.confirmed),
                "confirm": self.state, "ongoing": self.ongoing, "not_in_close": self.not_in_close,
                "recurrence_of": self.recurrence_of, "ai_sec": self.ai_sec,
                "recurrences": [_iso(r.first_seen) for r in self.recurrences], "end": _iso(self.end_at()),
                "members": [list(m) for m in sorted(self.members_all())], "related_tags": sorted(self.related_all())}

    def view(self):
        p, it = self.prob, self.prob.item
        if self.folded:
            state = "folded"        # 원 항목에 접힌 재발 — items() 는 이 카드를 내보내지 않는다
        elif self.state in ("open", "vanished"):
            state = "observing" if self.state == "open" else "vanished"
        else:
            state = {"writing": "writing", "ready": "ready", "failed": "ai_failed"}.get(self.ai, "writing")
        head = p.members[0]["tag"] if p.members else None
        return {
            "key": self.key, "state": state, "origin": self.origin,
            "confirm": self.state if self.state in ("ended", "closed") else None,
            "ongoing": self.ongoing, "not_in_close": self.not_in_close, "recurrence_of": self.recurrence_of,
            "tag": it.get("tag"), "kind": p.kind, "title": it.get("title"), "severity": self.severity(),
            "evidence": self.evidence_line(), "score": p.score, "score_max": self.score_top(),
            "start": _iso(p.minstart), "end": _iso(self.end_at()),
            "recurrences": [_iso(r.first_seen) for r in self.recurrences],
            "members": [list(m) for m in sorted(self.members_all())],
            "related_tags": sorted(self.related_all()),
            "stop": dict(p.stop) if p.stop else None,
            "first_seen": _iso(self.first_seen), "confirmed": _iso(self.confirmed),
            "ai_sec": self.ai_sec, "draft_item_id": self.draft_item_id, "error": self.error,
        }


class _Tracks:
    """문제별 추적기 모음 — 틱 실험 tracker_analyze.run(detach=False) 규칙 그대로 (RESULTS.md 「문제별 추적기」).

    - 한 틱에서 항목 ↔ 추적기는 겹치는 tag·kind 수가 큰 순(같으면 먼저 만든 추적기)으로 1:1. 동일성은 직전에 본 멤버 기준이라
      대표가 바뀌어도 이어진다. 시작 시각 ±15분으로 잡으면 드리프트 추적이 끊겼다(실측 477건).
    - 끝남 확정 = 결과에 있고 모든 멤버 끝 시각 ≤ 지금 − M(만든 틱에도 적용). 마감 확정 = 마감 틱에 결과에 있음.
      사라짐 = 확정 전 K틱 연속 결과에 없음(마감 틱에 없는 관찰 중 포함).
    - 끝남 확정한 추적기는 구간 끝까지 기억한다 — K틱 안 보여도 놓지 않는다. 놓으면 앞 근무에서 끝난 드리프트가 한 시간마다
      다시 떠 곧장 재확정됐다(실측 ② 08-22 야간 PDI-103 6회).
    - 재발 = 기억된 확정 문제가 최근 M분 안에 다시 움직임(멤버 끝 시각 > 지금 − M) → 새 추적기. 「끝 시각이 확정 때보다
      늦어짐」으로 세면 드리프트 끝 시각이 틱마다 몇 분씩 흔들리는 것만으로 재생성이 127~359건 쏟아졌다(실측).
    """

    def __init__(self, m_min=M_MIN, k_ticks=K_TICKS, next_key=None):
        self.gap, self.k_ticks = timedelta(minutes=m_min), k_ticks
        self.next_key = next_key or (lambda c=itertools.count(1): f"p{next(c)}")
        self.all, self.max_open = [], 0
        self.vanished_keys, self.ended_keys = [], []
        self.stats = {"created": 0, "vanished": 0, "ended": 0, "ended_at_birth": 0, "closed": 0,
                      "recurrence": 0, "folded": 0, "after_vanish": 0, "after_end": 0}

    def step(self, k, t, probs, closing):
        """틱 k(근무 시각 t)의 문제 목록을 반영한다
        → (이번 틱에 확정된 추적기, 결과 문제마다 그것을 대표하는 추적기, 확정 전에 사라진 재발 자식)."""
        confirmed, represent, gone_children = [], [None] * len(probs), []

        def judge(tr):
            if closing:
                tr.confirm("closed", t, k, self.gap)
                self.stats["closed"] += 1
            elif tr.prob.maxend <= t - self.gap:
                tr.confirm("ended", t, k, self.gap)
                tr.attached = True
                self.ended_keys.append(tr.keys)
                self.stats["ended"] += 1
            else:
                return
            confirmed.append(tr)

        def create(pi, parent=None):
            p = probs[pi]
            self.stats["after_vanish"] += any(p.keys & v for v in self.vanished_keys)
            self.stats["after_end"] += any(p.keys & e for e in self.ended_keys)
            tr = _Track(self.next_key(), len(self.all), p, k, t, recurrence_of=(parent.key if parent else None))
            tr.parent, tr.root = parent, (parent.owner() if parent is not None else None)   # 재발은 사슬이 길어도 첫 항목에 접힌다
            self.all.append(tr)
            self.stats["created"] += 1
            represent[pi] = tr
            judge(tr)
            if tr.state == "ended":
                self.stats["ended_at_birth"] += 1   # 창 안의 지난 구간에서 뒤늦게 잡힌 문제 — 회색 카드 없이 곧장 AI 로

        cands = [tr for tr in self.all if tr.state == "open" or (tr.state == "ended" and tr.attached)]
        pairs = sorted((-len(p.keys & tr.keys), tr.created, pi, tr.idx)
                       for pi, p in enumerate(probs) for tr in cands if p.keys & tr.keys)
        item_of, tr_of = {}, {}
        for _n, _c, pi, ti in pairs:
            if pi not in item_of and ti not in tr_of:
                item_of[pi], tr_of[ti] = ti, pi
        for tr in cands:
            pi = tr_of.get(tr.idx)
            if pi is None:
                tr.miss += 1
                if tr.state == "open" and (tr.miss >= self.k_ticks or closing):
                    tr.state, tr.gone = "vanished", t
                    self.vanished_keys.append(tr.keys)
                    self.stats["vanished"] += 1
                    if tr.parent is not None:
                        # 재발 자식이 확정 전에 사라지면 (가) 부모의 확정 기억을 되살리고 — 안 그러면 같은 문제가 새 추적기·
                        # 새 초안 항목으로 다시 난다(PDI-103 중복) — (나) 사라짐으로 끝내지 않고 원 항목에 접는다.
                        # 그 움직임은 실제로 일어난 일이라, 안 접으면 원 항목이 「06:30 에 끝남」인 채 승인된다(반증 B).
                        if tr.parent.state == "ended" and not tr.parent.attached:
                            tr.parent.attached = True
                        if tr.root is not None:
                            tr.conf_keys = tr.prob.keys     # 접기 판단·근거는 마지막으로 본 모습으로
                            gone_children.append(tr)
                continue
            p = probs[pi]
            represent[pi] = tr
            if tr.state == "ended":
                tr.miss, tr.keys = 0, p.keys           # 근거는 확정 때 그대로 두고 동일성만 따라간다
                if p.maxend > t - self.gap:
                    tr.attached = False
                    self.stats["recurrence"] += 1
                    create(pi, parent=tr)
            else:
                tr.see(p)
                judge(tr)
        for pi in range(len(probs)):
            if pi not in item_of:
                create(pi)
        self.max_open = max(self.max_open, sum(1 for tr in self.all if tr.state == "open"))
        return confirmed, represent, gone_children


# --- AI -----------------------------------------------------------------

def _write_ai(shift, item):
    """문제 하나를 AI 로 쓴다 → (항목, AI 상태). AI 호출은 이 한 곳에서만 한다 —
    final/speed-parallel 병합 뒤 context(이미 쓴 항목 + 이월 항목)와 프로세스 전역 동시 상한(_gate)을 여기에만 붙인다."""
    out, status_ = llm.rewrite(shift, [item])
    return out[0], status_


def _ai_worker():
    while True:
        replay, sec, tr = _ai_queue.get()
        try:
            replay.write(sec, tr)
        except Exception as exc:     # write 가 실패를 카드에 적는다. 여기로 오면 버그다 — 크게 남기고 카드도 실패로 둔다
            traceback.print_exc()
            with _lock:
                tr.ai, tr.error = "failed", f"{type(exc).__name__}: {exc}"


def _enqueue(replay, sec, tr, count=True):
    with _lock:
        _ai_threads[:] = [th for th in _ai_threads if th.is_alive()]
        while len(_ai_threads) < AI_WORKERS:
            th = threading.Thread(target=_ai_worker, name=f"live-ai-{len(_ai_threads) + 1}", daemon=True)
            th.start()
            _ai_threads.append(th)
        if count:
            sec.ai_jobs += 1
    _ai_queue.put((replay, sec, tr))


# --- 기준선·품질 --------------------------------------------------------

def _baselines(conn, shift_id, closing=False):
    """pipeline.run 2) 와 같은 규칙의 기준선 — 이 근무를 뺀 지난 근무 요약, 원본이 깨졌던 (근무, 태그) 요약은 뺀다.
    closing: 이 근무 요약을 저장한 뒤라 지난 근무가 없으면 잠정으로 이 근무 요약을 쓰고, 저장한 뒤 표 전체를 돌려준다(pipeline 이
    detect 에 넘기는 값). 검출기는 기준선을 읽지 않는다(빈 기준선 10/10 근무 동일, 실측) — 같은 경로를 타게 둘 뿐이다."""
    past = db.load_summaries(conn, exclude_shift=shift_id)
    if not past and closing:
        past = db.load_summaries(conn)
    bad = db.quality_bad_pairs(conn)
    for tag in list(past):
        keep = [s for s in past[tag] if (s["shift_id"], tag) not in bad]
        if keep:
            past[tag] = keep
        else:
            del past[tag]
    if not closing:
        return ports.build_baseline(past) if past else {}
    db.save_baselines(conn, ports.build_baseline(past))
    return db.load_baselines(conn)


def _record_quality(conn, shift_id, bad):
    """근무 원본의 품질 기록 — jobs.ingest_path 와 같은 규칙(깨진 행 + 태그별 10분 넘는 결측 구간). 원본 품질 항목과 AI 가 읽는다."""
    q = bad.quality(shift_id, False)
    gaps = db.find_gaps(conn, shift_id)
    if gaps:
        q = q or {"bad_rows": 0, "unattributed_rows": 0, "by_tag": {}, "samples": [], "skipped_by_user": False,
                  "pattern": "연속", "affected_tags": 0, "runs": []}
        q["gaps"] = gaps[:20]
    db.set_quality(conn, shift_id, q)
    return db.load_quality(conn, shift_id)


# --- 재생 ---------------------------------------------------------------

class _Section:
    """이어진 재생 안의 근무 하나 — 자기 추적기·초안·마감 동기화를 가진다."""

    def __init__(self, path, first_ts, next_key):
        self.path = path
        self.shift_id, self.kind, self.ws, self.we = collect.shift_id_for(first_ts)
        self.ws_iso, self.we_iso = _iso(self.ws), _iso(self.we)
        self.state = "waiting"          # waiting | accumulating | closing | closed
        self.shift = self.draft_id = self.quality = self.closing = self.false_confirm = self.finish_error = None
        self.baselines = {}
        self.mode = self.note = self.stop_q_from = self.prev = self.tick_info = None
        self.tracks = _Tracks(next_key=next_key)
        self.questions, self.quality_tracks = [], []
        self.bad = collect.BadRows(max_ratio=collect.MAX_BAD_RATIO)
        self.rows, self.rows_after, self.ai_jobs = 0, False, 0
        self.finishing = self.finished = False
        self.false_confirm_tracks = None

    def tracked(self):
        return self.tracks.all + self.questions + self.quality_tracks

    def cards(self):
        """화면에 카드로 나가는 추적기 — 접힌 재발은 원 항목의 근거 줄로 들어갔으므로 뺀다.
        세기·목록은 전부 이것만 쓴다(호출부마다 거르다 한 곳을 잊어 status() 가 터진 적이 있다)."""
        return [tr for tr in self.tracked() if not tr.folded]


def _plan(paths, prev_path=None):
    """CSV 경로들 → 이어진 근무 구간 목록. 순서·이어짐이 어긋나거나 앞 근무 CSV 가 바로 앞 근무가 아니면 ValueError."""
    keys = itertools.count(1)
    sections = []
    for path in paths:
        sec = _Section(path, _first_row(path)[0], lambda: f"p{next(keys)}")
        if sections and sec.ws != sections[-1].we:
            raise ValueError(f"근무가 이어지지 않습니다 — {sections[-1].shift_id}({sections[-1].path.name}) 다음이 "
                             f"{sec.shift_id}({path.name}) 입니다. 시각순으로 이어진 근무 CSV 를 주세요.")
        if (sec.we - sec.ws) % _TICK:
            # 마감 틱은 「마지막 분이 틱 경계」라는 데 기대고 있다 — 안 맞으면 마감 틱이 한 번도 안 돌아
            # sec.closing 이 None 인 채 마감 정리로 들어가 재생이 죽고 초안이 'live' 로 잠긴다.
            raise ValueError(f"틱 간격 {TICK_MIN}분이 근무 길이({sec.we - sec.ws})를 나누지 못합니다 — 마감 틱이 돌지 않습니다.")
        sections.append(sec)
    if not sections:
        raise ValueError("재생할 CSV 이름이 없습니다.")
    if prev_path is not None:
        want = collect.shift_id_for(sections[0].ws - timedelta(seconds=1))[0]
        got = collect.shift_id_for(_first_row(prev_path)[0])[0]
        if got != want:
            raise ValueError(f"앞 근무 CSV 는 {want} 여야 합니다 — {prev_path.name} 은 {got} 입니다.")
    return sections


class _Replay:
    """이어진 근무 체인 재생 하나. 재생 스레드가 시계·적재·틱·마감을 돌리고, AI 작업 스레드가 확정 문제를 쓴다."""

    def __init__(self, sections, speed, prev_path=None, hold=None, clock=None, replace_unconfirmed=False):
        self.sections, self.speed, self.prev_path, self.replace = sections, speed, prev_path, replace_unconfirmed
        self._hold, self._hold_lock = hold, threading.Lock()
        self._clock = clock or _clock
        self.cur, self.phase, self.phase_note, self.error = 0, "preparing", None, None
        self.base_data = self.base_mono = self.data_clock = None
        self.buffer, self.latest = _Buffer(), {}
        self.wlock, self.stopped, self.stop_flag = threading.Lock(), False, threading.Event()
        self._q_keys, self._g_keys = itertools.count(1), itertools.count(1)
        self.done = threading.Event()

    # -- 잠금 --

    def holding(self):
        return self._hold is not None

    def release_hold(self):
        with self._hold_lock:
            hold, self._hold = self._hold, None
        if hold is not None:
            jobs.release_hold(hold)

    @contextmanager
    def writing(self):
        """이 재생의 DB 쓰기 구간. 정지한 뒤에는 들어오지 못한다(_Stopped) — stop() 이 이 잠금 아래에서 정지를 켜므로,
        stop() 이 돌아온 뒤에는 늦게 돌아온 AI 결과도, 틱 하나를 마저 돈 재생 스레드도 DB 에 쓰지 못한다."""
        with self.wlock:
            if self.stopped:
                raise _Stopped()
            yield

    # -- 재생 스레드 --

    def run(self):
        try:
            if self.prev_path is not None:
                self._ingest_prev()
            for i, sec in enumerate(self.sections):
                self._open(sec, i)
                self._loop(sec)
                self._close(sec)
            last = self.sections[-1]
            with _lock:
                self.phase = "ended"
                self.phase_note = f"재생 데이터 끝 — 마지막 근무 {last.shift_id} 마감({last.we:%m-%d %H:%M})까지 재생했습니다"
            _log(self.phase_note)
            self._drain_ai()
        except _Stopped:
            pass                        # stop() 이 상태·잠금을 정리했다
        except Exception as exc:
            with self.wlock:            # 실패한 재생은 더 쓰지 않는다 — 늦게 돌아온 AI 결과도
                self.stopped = True
            with _lock:
                self.phase, self.error = "failed", f"{type(exc).__name__}: {exc}"
                for sec in self.sections:
                    for tr in sec.tracked():
                        if tr.ai == "writing":
                            tr.ai, tr.error = "failed", "재생이 실패해 서술하지 않았습니다"
            _log(f"✗ 재생 실패 — {self.error}")
            traceback.print_exc()
            self._drop_partial()
        finally:
            if not self.stop_flag.is_set():
                self.release_hold()     # 정지일 때는 stop() 이 제 자리에서 놓는다 — 리셋이 재생 스레드가 빠져나오길 기다리지 않게
            self.done.set()

    def _drop_partial(self):
        """쌓다 만 근무의 원본을 비운다 — 반쪽 원본은 쓸 데가 없고, 남겨 두면 재적재 판정이 헷갈린다.
        마감까지 간 근무는 그대로 둔다. 정지·실패 때 작업 락을 쥔 채 부른다."""
        half = [sec for sec in self.sections if sec.state in ("waiting", "accumulating") and sec.draft_id]
        if not half:
            return
        try:
            with db.connect() as conn:
                for sec in half:
                    n = db.forget_raw(conn, sec.shift_id)
                    _log(f"쌓다 만 원본 정리 — {sec.shift_id} {n:,}점(파일에서 다시 적재해야 온전해진다)")
        except Exception as exc:        # 정리 실패가 정지를 막지는 않는다 — 크게 남기고 넘어간다
            _log(f"⚠ 쌓다 만 원본을 비우지 못함 — {type(exc).__name__}: {exc}")

    def _now(self):
        """근무 시각(데이터 시각) = 첫 근무 시작 + 경과 × 배속. _lock 아래에서 부른다."""
        if self.base_mono is None:
            return self.sections[0].ws
        return min(self.sections[-1].we, self.base_data + timedelta(seconds=(self._clock() - self.base_mono) * self.speed))

    def _ingest_prev(self):
        _log(f"앞 근무 적재 — {self.prev_path.name}")
        bad = collect.BadRows(max_ratio=collect.MAX_BAD_RATIO)
        with self.writing():
            counts = collect.ingest(_StopAware(collect.source_for(self.prev_path, bad), self.stop_flag))
            with db.connect() as conn:
                for sid in counts:      # 업로드 적재(jobs.ingest_path + _summarize)와 같게 품질을 매기고 바로 요약한다
                    _record_quality(conn, sid, bad)
                    series = db.load_series(conn, sid)
                    if series:
                        db.save_summaries(conn, sid, ports.summarize(series))

    def _open(self, sec, i):
        """근무 하나를 쌓기 시작한다 — 초안을 'live' 로 열고, 이 근무 원본을 비우고, 창 방식을 정한다."""
        lo = sec.ws - timedelta(hours=WINDOW_H)
        with self.writing(), db.connect() as conn:
            db.upsert_shift(conn, sec.shift_id, sec.kind, sec.ws_iso, sec.we_iso, f"csv:{sec.path.name}")
            draft_id = db.open_live_draft(conn, sec.shift_id, ports.engine_source(),
                                          model=(llm.MODEL if llm.mode() != "off" else None), replace_unconfirmed=self.replace)
            # DB 에는 시계가 지난 원본만 둔다 — 업로드 적재가 파일째 넣어 둔 이 근무 원본을 비우고 1분 묶음으로 다시 쌓는다.
            db.forget_raw(conn, sec.shift_id)
            db.set_quality(conn, sec.shift_id, None)
            shift = dict(conn.execute("SELECT * FROM shift WHERE id = ?", (sec.shift_id,)).fetchone())
            complete, fill = db.raw_window_complete(conn, _iso(lo), sec.ws_iso)   # 채움률 — 양끝 표본만 보면 양방향으로 틀린다
            if complete and i == 0:     # 뒤 근무는 앞 근무를 재생하며 이미 버퍼에 있다
                self.buffer.load_sorted(conn.execute(
                    "SELECT tag, ts, value FROM raw_sample WHERE ts >= ? AND ts < ? ORDER BY tag, ts", (_iso(lo), sec.ws_iso)))
            baselines = _baselines(conn, sec.shift_id)
        with _lock:
            sec.shift, sec.draft_id, sec.baselines = shift, draft_id, baselines
            sec.prev = dict(fill, shift_id=collect.shift_id_for(sec.ws - timedelta(seconds=1))[0], complete=complete)
            if complete:
                sec.mode = "12h"
            else:
                # 체인 첫 근무·데이터 결손 — (가) 근무 시작 창으로 떨어진다. 조용히 넘기지 않고 몇 점인지까지 드러낸다.
                sec.mode = "shift_start"
                sec.note = (f"앞 근무 데이터가 모자람 — {fill['samples']:,}점 · {fill['buckets']}칸 중 {fill['filled']}칸만 참 · "
                            f"초반 결과가 흔들릴 수 있음" if fill["samples"] else "앞 근무 데이터 없음 — 초반 결과가 흔들릴 수 있음")
                # 경과 60분 틱까지 막는다 — 실측 오탐 44틱은 「경과 60분 안」(60분 틱 포함)이었고, 60분 틱에 뜨는 근무가 있었다(② 08-22 야간)
                sec.stop_q_from = sec.ws + timedelta(minutes=STOP_Q_BAN_MIN) + _TICK
            sec.state, self.cur = "accumulating", i
            if self.base_mono is None:
                self.base_data, self.base_mono, self.data_clock = sec.ws, self._clock(), sec.ws
                self.phase = "running"
        jobs.mark_qa_active()
        _log(f"근무 시작 — {sec.shift_id} · {sec.path.name} · {self.speed:g}배속 · 창 {sec.mode}"
             + (f" · 앞 근무 {fill['samples']:,}점 {fill['filled']}/{fill['buckets']}칸" if complete else f" · {sec.note}"))

    def _loop(self, sec):
        rows = collect.source_for(sec.path, sec.bad).read()
        try:
            self._minutes(sec, rows)
        finally:
            rows.close()        # 근무마다 CSV 를 닫는다 — 체인이 길어도 파일이 열린 채 남지 않게

    def _minutes(self, sec, rows):
        nxt = next(rows, None)
        minute, k = sec.ws + timedelta(minutes=1), 0
        n_ticks = (sec.we - sec.ws) // _TICK
        while True:
            if self.stop_flag.is_set():
                raise _Stopped()
            with _lock:
                now, speed = self._now(), self.speed
            if now < minute:
                time.sleep(min(0.2, max(0.005, (minute - now).total_seconds() / speed)))
                continue
            bundle, lo = [], minute - timedelta(minutes=1)
            while nxt is not None and nxt[0] < minute:
                if nxt[0] < lo:
                    raise ValueError(f"{sec.path.name}: 원본이 시각순이 아닙니다 — {_iso(nxt[0])} {nxt[1]} 행이 "
                                     f"{_iso(lo)} 묶음 뒤에 왔습니다. 실시간 입구는 시각순으로 받습니다.")
                bundle.append(nxt)
                nxt = next(rows, None)
            if nxt is not None and nxt[0] >= sec.we:
                # 근무 끝 뒤 행. 그 뒤에 이 근무 행이 또 오면 시각순이 아닌 것이다 — 마감에서 조용히 빠지지 않게 끝까지 본다.
                while nxt is not None and nxt[0] >= sec.we:
                    nxt = next(rows, None)
                if nxt is not None:
                    raise ValueError(f"{sec.path.name}: 원본이 시각순이 아닙니다 — 근무 끝({sec.we_iso}) 뒤 행 다음에 "
                                     f"{_iso(nxt[0])} {nxt[1]} 행이 왔습니다.")
                sec.rows_after = True
                _log(f"⚠ {sec.path.name} 에 근무 끝({sec.we_iso}) 뒤 행이 있습니다 — 그 행은 이 근무에 쌓지 않습니다")
            self._append(sec, bundle, minute)
            if (minute - sec.ws) % _TICK == timedelta(0):
                k += 1
                self._tick(sec, k, n_ticks, minute, closing=minute >= sec.we)
            if minute >= sec.we:
                return
            minute += timedelta(minutes=1)

    def _append(self, sec, bundle, minute):
        if bundle:
            with self.writing(), db.connect() as conn:
                collect.append(conn, bundle)
        last = {}
        for ts, tag, value in bundle:
            s = _iso(ts)
            self.buffer.add(tag, s, value)
            if tag not in last or s >= last[tag][0]:
                last[tag] = (s, value)
        with _lock:
            for tag, pt in last.items():
                if tag not in self.latest or pt[0] >= self.latest[tag][0]:
                    self.latest[tag] = pt
            sec.rows += len(bundle)
            self.data_clock = minute

    def _tick(self, sec, k, n_ticks, t, closing):
        c0 = time.perf_counter()
        with _lock:
            late = max(0.0, (self._now() - t).total_seconds() / self.speed)
        jobs.mark_qa_active()           # 정시 자동 비움(tools/reset_if_idle.sh)이 재생 도중 DB 를 비우지 않게
        lo = sec.ws if sec.mode == "shift_start" else t - timedelta(hours=WINDOW_H)
        series = self.buffer.slice(_iso(lo), _iso(t))
        baselines = sec.baselines
        if closing:
            # 마감 틱 = 배치: pipeline.run 과 같은 순서로 요약·기준선을 저장하고, 원본 품질을 매긴다(AI 가 그 사실을 알고 쓰게).
            with self.writing(), db.connect() as conn:
                db.save_summaries(conn, sec.shift_id, ports.summarize(series))
                # 품질을 기준선보다 먼저 적는다 — pipeline.run 은 적재 때 적힌 품질을 보고 기준선 재료를 거른다.
                # 뒤에 적으면 지난 근무 요약이 없어 잠정으로 떨어질 때 이번 근무의 깨진 (근무, 태그)가 안 빠진다.
                quality = _record_quality(conn, sec.shift_id, sec.bad)
                baselines = _baselines(conn, sec.shift_id, closing=True)
            with _lock:
                sec.quality = quality
        events = ports.detect(series, baselines)
        # 귀속: 끝 시각 ≥ 구간 시작. 끝 시각이 비면 시작 시각으로, 둘 다 비면 이 근무 것으로 본다(일괄 실행과 같게)
        mine = [e for e in events if (e.get("end_ts") or e.get("start_ts") or sec.ws_iso) >= sec.ws_iso]
        stored = _order_like_db(mine)
        with db.connect() as conn:
            items = ports.compose(sec.shift, stored, lambda tag, query: db.search_precedents(conn, tag, query))
        probs, question = _problems(items, stored, t)
        qprob = None
        if question is not None and not (sec.stop_q_from is not None and t < sec.stop_q_from):
            # (가) 근무 시작 창 경과 60분 전에는 띄우지 않는다 — 초반 동시 드리프트가 정지로 보인다(실측 10/10 근무)
            qprob = _Problem(question, [])
            burst, at, first = ports._stop_burst(stored)
            qprob.stop = {"at": _iso(at), "tags": burst, "first": first}
        if self.stop_flag.is_set():
            raise _Stopped()
        with _lock:
            confirmed, represent, gone_children = sec.tracks.step(k, t, probs, closing)
            q_tr = self._question(sec, k, t, qprob, closing)
            if q_tr is not None:
                confirmed.append(q_tr)
            observing = sum(1 for tr in sec.tracked() if tr.state == "open")
            sec.tick_info = {"t": _iso(t), "n": k, "of": n_ticks, "events": len(mine), "items": len(items),
                             "sec": round(time.perf_counter() - c0, 3), "late_sec": round(late, 2)}
        for tr in confirmed + gone_children:
            # 접기는 「같은 문제가 또 난 것」일 때만이다. 새 tag·kind 를 데려온 재발은 더 크거나 다른 문제라서
            # 옛 항목에 접으면 그 태그·중요도가 초안에서 통째로 사라진다(반증 B) — 별개 항목으로 세우고 제 AI 를 준다.
            # 확정 전에 사라진 자식은 제 항목이 없으므로, 같은 문제가 아니면 그냥 사라진 것으로 둔다.
            if tr.root is not None and not (set(tr.conf_keys or ()) <= set(tr.owner().conf_keys or ())):
                tr.root = None
        folds = [tr for tr in confirmed if tr.root is not None]      # 재발 — 원 항목에 접는다(새 항목·새 AI 없음)
        fresh = [tr for tr in confirmed if tr.root is None]
        for tr in fresh:            # 파형은 버퍼를 고치는 이 스레드에서 떠 둔다
            tr.db_events = [dict(e, waveform=self._wave(sec, e, closing)) for e in tr.prob.members]
        if closing:
            sec.closing = {"t": t, "k": k, "probs": probs, "represent": represent, "question": q_tr,
                           "events": [dict(e, waveform=self._wave(sec, e, True)) for e in mine]}
        for tr in folds + [tr for tr in gone_children if tr.root is not None]:
            self._fold(sec, tr)
        for tr in fresh:
            _enqueue(self, sec, tr)
        if sec.mode == "12h":
            self.buffer.trim(_iso(t + _TICK - timedelta(hours=WINDOW_H) - _WAVE_PAD))
        _log(f"틱 {sec.shift_id} {k}/{n_ticks} {t:%H:%M} · 이벤트 {len(mine)} · 항목 {len(items)} · 관찰 {observing}"
             + (f" · 확정 {len(confirmed)}" if confirmed else "") + f" · {sec.tick_info['sec']:.2f}s"
             + (f" · 늦음 {late:.1f}s" if late >= 1 else ""))

    def _question(self, sec, k, t, prob, closing):
        """정지 질문 — 동일성 하나로 본다. 끝남 확정은 없고 마감 틱에 있으면 마감 확정한다. _lock 아래. → 확정한 추적기|None"""
        q = sec.questions[-1] if sec.questions and sec.questions[-1].state == "open" else None
        if prob is None:
            if q is not None:
                q.miss += 1
                if q.miss >= K_TICKS or closing:
                    q.state, q.gone = "vanished", t
            return None
        if q is None:
            q = _Track(f"q{next(self._q_keys)}", len(sec.questions), prob, k, t, origin="question")
            sec.questions.append(q)
        else:
            q.see(prob)             # 매 틱 엔진의 현재 추정(시각 · 태그 수)으로 카드를 갱신한다
        if closing:
            q.confirm("closed", t, k, sec.tracks.gap)
            return q
        return None

    def _fold(self, sec, tr):
        """재발을 원 항목에 접는다 — 근무자에게 이건 여러 건이 아니라 「근무 중 여러 번 반복된 한 건」이다.
        새 초안 항목도 새 AI 호출도 만들지 않고, 원 항목의 근거 줄과 끝 시각만 갱신한다."""
        root = tr.owner()
        with _lock:
            tr.folded, tr.ai = True, None
            root.recurrences.append(tr)
            sec.tracks.stats["folded"] += 1
        _log(f"{tr.key} 재발 — {root.key} {root.prob.item.get('title')} 에 접음 (재발 {len(root.recurrences)}회)")
        self._sync_recurrences(sec, root)

    def _sync_recurrences(self, sec, root):
        """접힌 재발을 이미 저장된 원 항목에 반영한다. 아직 저장 전이면 저장할 때 함께 들어간다."""
        with _lock:
            if not root.draft_item_id or root.saved_recur == len(root.recurrences):
                return
            item_id, evidence, live = root.draft_item_id, root.evidence_line(), root.live()
            sev, n = root.severity(), len(root.recurrences)
        try:
            with self.writing(), db.connect() as conn:
                db.update_live_item(conn, item_id, evidence, live, sev)
        except _Stopped:
            return
        with _lock:
            root.saved_recur = n

    def _wave(self, sec, e, closing):
        """감지 구간 ±30분 파형(pipeline._waveform). 마감 틱은 배치와 같게 구간 원본 [구간 시작, 끝) 에서만 뜬다."""
        try:
            lo = datetime.fromisoformat(e["start_ts"]) - _WAVE_PAD
            hi = datetime.fromisoformat(e["end_ts"]) + _WAVE_PAD
        except (TypeError, ValueError):
            return None
        pts = self.buffer.around(e["tag"], max(_iso(lo), sec.ws_iso) if closing else _iso(lo), _iso(hi))
        return pipeline._waveform(pts, e.get("start_ts"), e.get("end_ts"))

    def _close(self, sec):
        """마감 동기화의 나머지 — 마감 틱(= 배치) 반영과 마감 확정은 _tick 이 했다. 「마감 검출에 없음」을 달고(내리지 않는다 —
        근무자가 판단), 원본 품질 항목을 마감 확정으로 올린다. 마감 AI 는 뒤에서 돌고, 시계는 다음 근무로 넘어간다."""
        c = sec.closing
        close_keys = [p.keys for p in c["probs"]]
        new = []
        with _lock:
            sec.state = "closing"
            for tr in sec.tracks.all:
                # 접힌 재발은 원 항목의 tag·kind 안에 있으므로(접기 조건이 부분집합) 원 항목 기준으로 보면 된다
                if tr.state == "ended" and not tr.folded and not any(set(tr.conf_keys) & ck for ck in close_keys):
                    tr.not_in_close = True
            false = [tr for tr in sec.tracks.all if tr.not_in_close]
            sec.false_confirm = len(false)          # 화면·세기 기준 — 접힌 재발은 카드가 아니라서 빠진다
            # 틱 실험 대조용(추적기 기준 · 접기 무시) — 로그와 검증에만 쓴다. 화면 값과 다른 수다.
            sec.false_confirm_tracks = sum(1 for tr in sec.tracks.all
                                           if tr.state == "ended" and not any(set(tr.conf_keys) & ck for ck in close_keys))
            for it in pipeline.quality_items(sec.quality):
                tr = _Track(f"g{next(self._g_keys)}", len(sec.quality_tracks), _Problem(it, []), c["k"], c["t"], origin="quality")
                tr.confirm("closed", c["t"], c["k"], sec.tracks.gap)
                sec.quality_tracks.append(tr)
                new.append(tr)
            ended = [tr for tr in sec.tracks.all if tr.state == "ended"]
            lat = sorted((tr.conf_tick - tr.created) * TICK_MIN for tr in ended)
            summary = dict(sec.tracks.stats, shift=sec.shift_id, window=sec.mode, max_observing=sec.tracks.max_open,
                           false_confirm=len(false), false_confirm_tracks=sec.false_confirm_tracks,
                           false_confirm_before_shift=sum(1 for tr in false if tr.prob.minstart < sec.ws),
                           questions=len(sec.questions), quality_items=len(new), ai_jobs=sec.ai_jobs + len(new),
                           confirm_lat_min_median=(statistics.median(lat) if lat else None),
                           confirm_lat_min_p90=(lat[min(len(lat) - 1, int(round(0.9 * (len(lat) - 1))))] if lat else None),
                           false_detail=[[_iso(tr.first_seen)[11:16], _iso(tr.confirmed)[11:16], sorted(tr.conf_keys)[:3]] for tr in false])
            sec.state = "closed"
        for tr in new:
            _enqueue(self, sec, tr)
        _log("마감 " + json.dumps(summary, ensure_ascii=False))
        self._finish(sec)

    def _settled(self):
        """모든 근무의 마감 AI·마감 정리가 끝났나(실패로 멈춘 것은 기다리지 않는다). _lock 아래."""
        for sec in self.sections:
            trs = sec.tracked()
            if sec.finishing or any(tr.ai == "writing" for tr in trs):
                return False
            if sec.state == "closed" and not (sec.finished or sec.finish_error or any(tr.ai == "failed" for tr in trs)):
                return False
        return True

    def _drain_ai(self):
        said = 0
        while True:
            if self.stop_flag.is_set():
                raise _Stopped()
            with _lock:
                if self._settled():
                    return
                waiting = [(sec.shift_id, sum(tr.ai == "writing" for tr in sec.tracked()), sec.finishing)
                           for sec in self.sections if sec.state == "closed" and not sec.finished]
            if time.monotonic() - said >= 60:    # 조용히 기다리지 않는다 — 무엇을 기다리는지(작업 락을 왜 쥐고 있는지) 남긴다
                said = time.monotonic()
                _log("마감 기다리는 중 — " + ", ".join(f"{sid} AI 서술 {n}건{' · 정리 중' if fin else ''}" for sid, n, fin in waiting))
            time.sleep(0.2)

    def _finish(self, sec):
        """근무 하나의 마감 동기화와 AI 서술이 다 끝났으면 초안을 승인 대기로 넘긴다. 재생 스레드와 AI 작업 스레드가 부른다.
        항목 순서 = 원본 품질 → 마감 틱 순서(배치와 같음) → 마감 틱이 대표하지 않는 확정 항목(마감 검출에 없음·재발 앞 문제) 처음 감지순."""
        with _lock:
            trs = sec.tracked()
            if sec.finished or sec.finishing or sec.state != "closed" or any(tr.ai in ("writing", "failed") for tr in trs):
                return
            sec.finishing = True
            c = sec.closing
            first = sec.quality_tracks + ([c["question"]] if c["question"] is not None else []) + [tr.owner() for tr in c["represent"] if tr]
            rest = sorted((tr for tr in trs if tr.draft_item_id), key=lambda tr: (tr.first_seen, tr.order))
            order = list(dict.fromkeys(tr.draft_item_id for tr in first + rest if tr.draft_item_id))
            # 늦게 돌아온 _sync_recurrences 가 앞선 갱신을 되돌렸을 수 있다(경합) — 마감에서 최종본으로 덮는다
            finals = {tr.draft_item_id: {"live": tr.live(), "evidence": tr.evidence_line(), "severity": tr.severity()}
                      for tr in trs if tr.draft_item_id}
        try:
            with self.writing(), db.connect() as conn:
                # 이벤트 표를 마감 집합(= 배치)에 맞춘다 — 관리 화면 채점이 일괄 실행과 같아진다. 확정 항목의 대표는 마감 행을
                # 가리키게 다시 잇고(파형은 더 넓은 마감본이 된다), 마감 검출에 없는 확정 항목의 근거 행만 남긴다.
                rows = db.add_events(conn, sec.shift_id, c["events"], ports.engine_source())
                for pi, tr in enumerate(c["represent"]):
                    head = c["probs"][pi].members[0]
                    item_id = tr.owner().draft_item_id if tr is not None else None
                    if item_id and head.get("id"):
                        conn.execute("UPDATE draft_item SET event_id = ? WHERE id = ?", (rows[head["id"] - 1], item_id))
                dropped = db.drop_unreferenced_events(conn, sec.shift_id, rows)
                db.finish_live_draft(conn, sec.draft_id, order, finals)
        except _Stopped:
            with _lock:
                sec.finishing = False
            return
        except Exception as exc:        # 조용히 넘기지 않는다 — 잠금 문구와 status.error 에 드러난다
            with _lock:
                sec.finishing, sec.finish_error = False, f"{type(exc).__name__}: {exc}"
                self.error = self.error or f"{sec.shift_id} 마감 정리 실패 — {sec.finish_error}"
            _log(f"✗ {sec.shift_id} 마감 정리 실패 — {sec.finish_error}")
            traceback.print_exc()
            return
        with _lock:
            sec.finishing, sec.finished = False, True
        _log(f"승인 대기 — {sec.shift_id} 초안 {len(order)}개 항목 · 이벤트 {len(rows)}건(마감 집합) · 확정 때 근거 {dropped}건 정리")

    # -- AI 작업 스레드 --

    def write(self, sec, tr):
        """확정된 문제 하나를 AI 로 쓰고 그 근무 초안 끝에 넣는다."""
        if self.stopped:                # 줄에서 기다리던 작업 — 버린다
            with _lock:
                tr.ai, tr.error = "failed", _STOP_NOTE
            return
        p = tr.prob
        item = dict(p.item, metrics=dict(p.members[0].get("metrics") or {}) if p.members else {})
        try:
            out, _ai_status = _write_ai(dict(sec.shift, quality=sec.quality), item)
            new = {key: v for key, v in out.items() if key != "metrics"}   # 저장 스키마엔 없는 임시 필드 (pipeline 과 같음)
            with _lock:
                ai_sec = round(time.monotonic() - tr.conf_wall, 2)
                tr.ai_severity = new.get("severity")        # AI 가 판정한 중요도 — 접힌 재발과 견줄 기준
                new["severity"] = tr.severity()
                new["evidence"], new["live"] = tr.evidence_line(), dict(tr.live(), ai_sec=ai_sec)
                n_recur = len(tr.recurrences)
            with self.writing(), db.connect() as conn:
                # 지난 근무에서 제외한 (태그, 종류) 표시는 AI 뒤에 단다 — pipeline 4-2) 와 같은 이유(AI 가 판정을 접지 않게).
                info = db.recent_exclusions(conn, sec.shift_id).get((new.get("tag"), p.kind)) if p.members else None
                if info:
                    new["prev_excluded"] = info
                item_id = db.add_live_item(conn, sec.draft_id, new, tr.db_events, ports.engine_source())
        except _Stopped:
            with _lock:
                tr.ai, tr.error = "failed", _STOP_NOTE
            _log(f"{tr.key} 정지 뒤 돌아온 AI 결과 — DB 에 쓰지 않음")
            return
        except Exception as exc:     # AI·저장 실패는 조용히 넘기지 않는다 — 카드에 드러내고, 남아 있는 동안 승인을 잠근다
            with _lock:
                tr.ai, tr.error = "failed", f"{type(exc).__name__}: {exc}"
            _log(f"✗ AI 서술 실패 {tr.key} {p.item.get('title')} — {tr.error}")
            return
        with _lock:
            tr.ai, tr.ai_sec, tr.draft_item_id, tr.error, tr.saved_recur = "ready", ai_sec, item_id, None, n_recur
        _log(f"{tr.key} 선택 가능 — {sec.shift_id} {p.item.get('title')} · 처음 감지 {_iso(tr.first_seen)[11:16]} · "
             f"확정 {_iso(tr.confirmed)[11:16]}({tr.state}) · AI {ai_sec}s")
        self._sync_recurrences(sec, tr)     # AI 가 도는 사이에 접힌 재발이 있으면 반영한다
        self._finish(sec)

    # -- 화면용 (전부 _lock 아래) --

    def lock_reason(self, sec):
        if sec.finished:
            return None
        if self.phase in ("stopped", "failed"):
            return "재생이 멈춘 구간 — 다시 재생하거나 일괄 실행한 뒤 승인"
        if sec.state != "closed":
            return f"{sec.we:%H:%M} 마감 후 승인"
        trs = sec.tracked()
        writing, failed = sum(tr.ai == "writing" for tr in trs), sum(tr.ai == "failed" for tr in trs)
        if writing:
            return f"마감 AI 서술 {writing}건 진행 중"
        if failed:
            return f"AI 서술 실패 {failed}건 — 다시 시도한 뒤 승인"
        return f"마감 정리 실패 — {sec.finish_error}" if sec.finish_error else "마감 정리 중"

    def summary(self, sec):
        trs = sec.cards()
        views = Counter(tr.view()["state"] for tr in trs)
        st = sec.tracks.stats
        return {
            "counts": {"observing": views["observing"], "writing": views["writing"], "ready": views["ready"],
                       "ai_failed": views["ai_failed"], "vanished": views["vanished"],
                       "not_in_close": sum(1 for tr in trs if tr.not_in_close)},
            "stats": {"created": st["created"], "max_observing": sec.tracks.max_open, "vanished": st["vanished"],
                      "ended": st["ended"], "ended_at_birth": st["ended_at_birth"], "closed": st["closed"],
                      "recurrence": st["recurrence"], "folded": st["folded"], "false_confirm": sec.false_confirm,
                      "ai_jobs": sec.ai_jobs, "ai_failed": views["ai_failed"]},
        }

    def snapshot(self):
        sec = self.sections[self.cur]
        return dict({
            "phase": self.phase, "phase_note": self.phase_note,
            "chain": [s.shift_id for s in self.sections], "files": [s.path.name for s in self.sections],
            "shift_id": sec.shift_id, "window": [sec.ws_iso, sec.we_iso], "file": sec.path.name,
            "clock": _iso(self.data_clock), "speed": self.speed,
            "window_mode": sec.mode, "window_note": sec.note, "stop_question_from": _iso(sec.stop_q_from),
            "prev": dict(sec.prev) if sec.prev else None,
            "tick": dict(sec.tick_info) if sec.tick_info else None,
            "rows": {"appended": sec.rows, "bad": sec.bad.count, "after_window": sec.rows_after},
            "approve_lock": self.lock_reason(sec),
            "closed": [dict({"shift_id": s.shift_id, "draft": "pending" if s.finished else "live",
                             "approve_lock": self.lock_reason(s)}, **self.summary(s))
                       for s in self.sections if s.state == "closed"],
            "error": self.error,
        }, **self.summary(sec))


# --- 공개 함수 ----------------------------------------------------------

_IDLE = {"phase": "idle", "phase_note": None, "chain": [], "files": [], "shift_id": None, "window": None, "file": None,
         "clock": None, "speed": None, "window_mode": None, "window_note": None, "stop_question_from": None, "prev": None,
         "tick": None, "counts": {"observing": 0, "writing": 0, "ready": 0, "ai_failed": 0, "vanished": 0, "not_in_close": 0},
         "stats": None, "rows": None, "approve_lock": None, "closed": [], "error": None}


def start(csv_names, speed=100, prev_csv_name=None, replace_unconfirmed=False):
    global _replay
    speed = _speed(speed)
    if not isinstance(csv_names, (str, list, tuple)):
        raise ValueError(f"CSV 이름 목록이어야 합니다. 받은 것: {type(csv_names).__name__}")
    paths = [_upload_path(n) for n in ([csv_names] if isinstance(csv_names, str) else csv_names)]
    prev_path = _upload_path(prev_csv_name) if prev_csv_name is not None else None
    sections = _plan(paths, prev_path)
    with db.connect() as conn:
        for sec in sections:
            why = db.live_refusal(conn, sec.shift_id, replace_unconfirmed)
            if why:
                raise ValueError(why)
    hold = jobs.hold()
    if hold is None:
        raise ValueError("재생 중이거나 다른 작업(적재·일괄 실행·리셋)이 돌고 있습니다 — 끝난 뒤 다시 시작하세요.")
    jobs.mark_qa_active()
    r = _Replay(sections, speed, prev_path, hold, replace_unconfirmed=bool(replace_unconfirmed))
    with _lock:
        _replay = r
    try:
        threading.Thread(target=r.run, name="live-replay", daemon=True).start()
    except Exception as exc:     # 스레드가 안 뜨면 작업 락이 영구 점유된다 — 놓고 드러낸다
        with _lock:
            r.phase, r.error = "failed", f"재생 스레드를 시작하지 못함 — {type(exc).__name__}: {exc}"
        r.release_hold()
        raise
    return status()


def stop():
    with _lock:
        r = _replay
        if r is None or not (r.phase in ("preparing", "running") or (r.phase == "ended" and r.holding())):
            raise ValueError("재생 중이 아닙니다.")
        r.stop_flag.set()
    try:
        with r.wlock:           # 진행 중인 DB 쓰기 한 번만 기다린다 — 1분 묶음·항목 하나·마감 정리. 앞 근무 적재는 읽는 중에 끊고 되돌린다
            r.stopped = True
        with _lock:
            r.phase = "stopped"
            for sec in r.sections:
                for tr in sec.tracked():
                    if tr.ai == "writing":
                        tr.ai, tr.error = "failed", _STOP_NOTE
        r._drop_partial()   # 쌓다 만 원본은 여기서 비운다(작업 락을 쥔 채) — 반쪽 원본이 「온전」으로 보이지 않게
    finally:
        r.release_hold()        # 어떤 경우에도 여기서 놓는다 — 재생 스레드는 정지일 때 놓지 않는다
    _log(f"정지 — 근무 시각 {_iso(r.data_clock)}. 마감·AI 가 덜 끝난 근무의 초안은 'live' 로 남는다")
    return status()


def set_speed(speed):
    speed = _speed(speed)
    with _lock:
        r = _replay
        if r is None or r.phase not in ("preparing", "running"):
            raise ValueError("재생 중이 아닙니다.")
        if r.base_mono is not None:
            r.base_data, r.base_mono = r._now(), r._clock()
        r.speed = speed
    return status()


def retry(key):
    with _lock:
        r = _replay
        found = next(((s, tr) for s in (r.sections if r else []) for tr in s.tracked() if tr.key == key), None)
        if found is None:
            raise ValueError(f"항목 {key} 가 없습니다.")
        sec, tr = found
        if tr.ai != "failed":
            raise ValueError(f"{key} 는 AI 서술 실패 상태가 아닙니다.")
        if r.phase in ("stopped", "failed"):
            raise ValueError("멈춘 재생입니다 — 다시 재생하세요.")
        tr.ai, tr.error = "writing", None
    _enqueue(r, sec, tr, count=False)
    return status()


def status():
    with _lock:
        r = _replay
        snap = r.snapshot() if r is not None else dict(_IDLE, counts=dict(_IDLE["counts"]), chain=[], files=[], closed=[])
    try:
        with db.connect() as conn:
            live = db.live_drafts(conn)
    except Exception as exc:
        snap["error"] = snap["error"] or f"DB 를 읽지 못함 — {type(exc).__name__}: {exc}"
        live = []
    handled = set(snap["chain"]) if snap["phase"] in ("preparing", "running", "ended") else set()
    snap["interrupted"] = [s for s in live if s not in handled]
    return snap


def items(shift_id=None):
    with _lock:
        r = _replay
        if r is None:
            return []
        sec = r.sections[r.cur] if shift_id is None else next((s for s in r.sections if s.shift_id == shift_id), None)
        if sec is None:
            return []
        return [tr.view() for tr in sorted(sec.cards(), key=lambda tr: (tr.first_seen, tr.order))]


def forget():
    """지난 재생 기억을 지운다 — 리셋·재시작 뒤 화면이 옛 재생 상태를 계속 보여 주지 않게. 재생 중이면 거부한다."""
    global _replay
    with _lock:
        r = _replay
        if r is not None and (r.phase in ("preparing", "running") or r.holding()):
            raise ValueError("재생 중입니다 — 정지한 뒤에 지우세요.")
        failed = sum(1 for s in (r.sections if r is not None else []) for tr in s.tracked() if tr.ai == "failed")
        if failed:      # 지우면 retry 할 추적기가 사라지는데 초안은 'live' 라 승인이 영영 잠긴다
            raise ValueError(f"AI 서술 실패 {failed}건 — 다시 시도하거나 일괄 실행한 뒤 지우세요.")
        _replay = None
    return status()


def values():
    with _lock:
        r = _replay
        if r is None or r.data_clock is None:
            return {"clock": None, "values": {}}
        return {"clock": _iso(r.data_clock), "values": {tag: [ts, v] for tag, (ts, v) in r.latest.items()}}


def approve_lock(shift_id):
    try:
        with db.connect() as conn:
            d = conn.execute("SELECT status FROM draft WHERE shift_id = ?", (shift_id,)).fetchone()
            prev = db.unconfirmed_before(conn, shift_id)
        with _lock:
            r = _replay
            sec = next((s for s in r.sections if s.shift_id == shift_id), None) if r is not None else None
            reason = r.lock_reason(sec) if sec is not None else None
            waiting = sec is not None and sec.state != "closed" and r.phase in ("preparing", "running")
        if d is not None and d["status"] == "live":
            return reason or "재생이 중단된 구간 — 다시 재생하거나 일괄 실행한 뒤 승인"
        if waiting:
            return reason           # 이 재생이 곧 (다시) 쌓을 근무
        if prev and prev[1] == "live":
            return f"앞 근무 {prev[0]} 의 실시간 초안이 안 끝났습니다 — 다시 재생하거나 일괄 실행해 확정한 뒤 승인"
        if prev:
            return f"앞 근무 {prev[0]} 승인 대기 — 앞 근무를 먼저 확정"
        return None
    except Exception as exc:     # 못 읽으면 잠근다. 서버 강제(approve.decide)는 따로 있다
        return f"승인 잠금 상태를 읽지 못함 — {type(exc).__name__}: {exc}"
