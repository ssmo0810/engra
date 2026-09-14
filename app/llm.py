"""초안 서술을 AI 로 쓴다. `compose()` 가 만든 항목의 **문장만** 다시 쓰고 숫자·근거는 건드리지 않는다.

두 가지 원칙을 코드로 강제한다.

1. **AI 는 필수다.** 설정이 없거나 호출이 실패하면 초안 생성을 멈추고 이유를 그대로 올린다.
   조용히 문장 틀로 내려가지 않는다 — 기획서 3-5 가 "AI 없으면 업무 자체가 성립하지 않는다" 고
   주장하는데 코드가 AI 없이 돌면 그 주장이 거짓이 된다. 끄려면 `ENGRA_LLM=off` 를 명시해야
   하고, 그때 화면에 `AI 미연결` 이 뜬다 (엔진의 engine/stub 배지와 같은 관례).

2. **숫자는 모델을 지나가지 않는다.** 모델은 `evidence`·`metrics` 를 **입력**으로 받아 문장만
   만들고, 출력 스키마에 수치 필드가 없다. 지어낼 자리 자체를 없앤다. `tag`·`severity`·
   `evidence`·`precedents` 는 원본 그대로 통과한다.

호출은 두 단계다 (2026-09-14 — 5개 묶음 순차가 16항목 5분 42초로 병목이라 갈랐다).
  1단계 서술 — **항목 1개 = 호출 1개**, 동시에 돈다(상한 `ENGRA_LLM_PARALLEL`, 기본 8 — 프로세스 전체가 나눠 쓴다).
           항목 하나라도 끝내 실패하면 전체가 LLMUnavailable 이다(원칙 ①).
  2단계 연속성 — 전 항목의 번호·태그·시각·제목·중요도·근거를 한 번에 넘겨 같은 사건 묶기(related_idx)·
           중복(duplicate_of)을 근무 전체 범위에서 판정한다. 예전엔 묶음 5개 안에서만 봐서 묶음 경계에서 끊겼다.
           이 단계만 실패하면 1단계 결과는 살리되 related 를 비우고 **상태 문구에 실패를 명시**한다.
           응답에서 빠진 항목은 연속성 판정에서 통째로 빠지고(그 항목을 가리킨 묶음도 버림), 그 사실도 상태 문구에 적는다.

호출 경로는 둘이고 환경변수 `ENGRA_LLM` 으로 고른다.
  cli — `claude -p --json-schema` 서브프로세스. 개발·프롬프트 튜닝용 (구독, 비용 0)
  api — Anthropic Messages API 직접 호출. 서버 무인 실행용. 키는 ANTHROPIC_API_KEY
  off — AI 끔. 문장 틀 그대로. 화면에 미연결 표시
설정이 없으면 api 를 시도하고, 키가 없으면 실패한다 — 기본값이 "AI 켜짐" 이다.

표준 라이브러리만 쓴다. api 경로도 urllib 로 직접 호출해 SDK 의존을 만들지 않는다.
"""
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

MODEL = os.environ.get("ENGRA_LLM_MODEL", "claude-opus-5")
API_URL = "https://api.anthropic.com/v1/messages"
TIMEOUT_SEC = 300
ATTEMPTS = 2                               # 항목마다·연속성 판정마다. 두 번 다 실패하면 그때 멈춘다
KEEP_SENTENCES = ("quality", "question")   # AI 가 문장을 고치지 않는 항목 — 이유는 _merge

# 1단계 — 모델이 채울 수 있는 것은 문장과 판정뿐이다. 숫자 필드는 없다. 항목 1개의 응답.
ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "idx": {"type": "integer", "description": "입력 항목의 순번. 그대로 돌려준다"},
        "title": {"type": "string", "description": "한 줄 제목. 태그와 현상"},
        "body": {"type": "string", "description": "인수인계 문장 2~4개. 근거의 숫자를 그대로 인용"},
        "severity": {"type": "string", "enum": ["상", "중", "하"],
                     "description": "인수인계 관점의 중요도. 통계 크기가 아니라 '놓치면 무엇이 일어나는가'로 판단"},
        "severity_reason": {"type": "string", "description": "그 중요도로 판단한 이유 한 문장. 근무자가 읽고 동의할 수 있어야 한다"},
        "handover_worthy": {"type": "boolean", "description": "다음 근무자에게 실제로 전달할 가치가 있는가. 외기 변동처럼 정상 운전의 일부면 false"},
        "precedent_fit": {"type": "array", "items": {"type": "integer"},
                          "description": "제시된 과거 조치 중 이번 증상에 실제로 맞는 것의 번호(0부터). 태그만 같고 증상·조치가 다르면 넣지 않는다. 없으면 빈 배열"},
        "precedent_note": {"type": "string", "description": "과거 조치를 채택/기각한 이유 한 문장. 사례가 없었으면 빈 문자열"},
    },
    "required": ["idx", "title", "body", "severity", "severity_reason", "handover_worthy", "precedent_fit", "precedent_note"],
    "additionalProperties": False,
}

# 2단계 — 근무 전체 인덱스로 묶는다. 문장 필드가 없어 응답이 짧다. tag 는 번호 어긋남을 가려내려고 되돌려 받는다.
LINK_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer", "description": "입력 항목의 순번. 그대로 돌려준다"},
                    "tag": {"type": "string",
                            "description": "그 순번 항목의 태그 표기. 입력 그대로 돌려준다(같은 태그가 여럿이면 TI-301#2 처럼 붙은 표기 그대로, 태그가 없으면 \"None\")"},
                    "related_idx": {"type": "array", "items": {"type": "integer"},
                                    "description": "근무 전체에서 같은 사건의 다른 얼굴이라고 판단되는 항목 번호. 태그 마스터 연결이 없어도 공정 지식으로 판단. 없으면 빈 배열"},
                    "related_note": {"type": "string", "description": "왜 같은 사건으로 보는지 한 문장. related_idx 가 비면 빈 문자열"},
                    "duplicate_of": {"type": "integer",
                                     "description": "이 항목이 같은 사건을 다시 적은 중복이면 대표 항목 번호(더 길거나 더 중요한 쪽). 중복이 아니면 -1"},
                },
                "required": ["idx", "tag", "related_idx", "related_note", "duplicate_of"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

# 「AI 연결 점검」 용 — 빈 배열 하나만 받는다.
CHECK_SCHEMA = {"type": "object", "properties": {"items": {"type": "array"}}, "required": ["items"], "additionalProperties": False}

SYSTEM = """당신은 24시간 연속 공정(공기분리장치, ASU) 교대 근무의 인수인계 초안을 쓰는 보조자다.
통계 검출기가 이번 근무에서 잡아낸 이상 후보와 그 근거 수치, 그리고 과거 확정 일지의 조치를 받아
다음 근무자가 읽을 문장을 쓴다. 한 번에 항목 하나를 받는다.

지켜야 할 것:
- 숫자는 주어진 근거(evidence, metrics)에 있는 값만 쓴다. 새 숫자를 만들지 않는다.
- **통계 기호와 용어를 본문에 쓰지 않는다.** σ·시그마·표준편차·잔차·상관계수·r 값·z 값·「n 시그마」는
  현장 근무자가 읽는 말이 아니다. 대신 주어진 **실제 단위 값**(℃·bar·%)과 **pct_range**(정상범위 폭 대비 몇 %)로 쓴다.
  예) "4.6σ 벗어남"(X) → "정상범위 폭의 47%만큼 벗어남"(O) / "21σ 급등"(X) → "13.3℃ 튐 — 정상범위 폭의 44%"(O)
- 판단하지 않은 것을 단정하지 않는다. 원인이 불확실하면 "의심됨" 으로 쓴다.
- 문장은 현장 근무자가 쓰는 말투로 짧게. 존칭 없이 "~됨", "~확인 필요" 형태.
- **조치를 지어내지 않는다.** 현장 조치는 과거 확정 일지(precedents)에 있는 것만 유효하다 — 당신은 이 공장의 절차를 모른다.
  당신이 할 일은 그 사례가 이번 현상에 맞는지(precedent_fit) 판정하는 것이다.
- 근무 원본에 결측·형식 오류가 있었다는 '원본 품질' 안내가 있으면, 그 태그의 항목 severity_reason 에 신뢰도가 낮다는 점을 적는다.
- 태그명은 그대로 쓴다 (예: TI-205). 설명은 괄호로 붙인다.
- 알람이 아직 울리지 않았지만 추세가 한계로 향하는 경우, 그 점을 반드시 명시한다 — 이것이 인수인계의 핵심이다.

중요도(severity)는 통계 검출기가 매긴 값을 참고만 하고 **다시 판단**한다. 기준은 통계 크기가 아니라
"놓치면 무엇이 일어나는가" 다. 현장이 정한 기준을 따른다:
- 상: 품질·안전·설비에 직결. 예) 순도 헌팅(제품 품질), 흡착탑 수분 파과(콜드박스 결빙), 압축기 서지 전조,
  밸브 고착(레벨 추세로만 판별), 상관 붕괴(고장 공통 신호), 베어링 이상 전조, 다음 근무에 한계 도달하는 드리프트
- 중: 손실·비효율이지만 즉시 위험은 아님. 예) 반복 헌팅으로 벤트 손실, 분석기 순간 이상값(계측 신뢰도),
  필터 차압 상승, 단시간 급증 후 원복(교대 시점엔 정상이라 적지 않으면 다음 근무자가 모름)
- 하: 알람 없이 지나갔고 후속 영향이 작음. 예) 임계 근접 후 복귀

handover_worthy 는 엄격하게 판단한다. **대기 온습도(TI-101, MI-102) 의 하루 주기 변동과 그에 따라 함께
움직인 하류 태그의 완만한 변화는 정상 운전의 일부**라 false 다. 단, 그 태그가 한계에 접근하거나
다른 이상과 겹치면 true. 판단이 갈리면 true 로 두고 severity_reason 에 의심을 적는다 —
놓치는 것이 과잉 표시보다 비싸다.

과거 조치(precedents)는 **같은 태그의 최근 확정 일지**를 기계적으로 가져온 것이다. 태그가 같아도
증상이 다르거나 조치가 이번 현상과 무관하면 채택하지 않는다. 예) 대기 습도 드리프트에
"LP 컬럼 리플럭스 조정" 은 맞지 않는다 — 다른 항목의 조치가 같은 근무 일지에 섞여 들어온 것이다.
맞는 사례만 precedent_fit 에 번호로 적는다. 맞는 사례가 없으면 빈 배열 — 대신 조치를 쓰지 않는다."""

LINK_SYSTEM = """당신은 24시간 연속 공정(공기분리장치, ASU) 교대 근무 인수인계 초안의 **연속성**을 판정한다.
이번 근무의 초안 항목 전체(번호·태그·시각·제목·중요도·근거)를 받는다. 문장은 쓰지 않는다 — 항목 사이의 관계만 판정한다.

항목들은 통계 검출기가 시간 겹침과 태그 마스터의 연결(links)로 이미 한 번 묶은 것이다. 그 묶음이
놓치는 것을 공정 지식으로 찾아라 — 예) A탑·B탑 후단 수분이 동시에 오르면 공통 원인(재생 가스·
전환 밸브), 토출압 상승 + 유량 감소 + 전력 증가면 서지 전조 하나, 순도 하강과 Cold end 온도
상승은 콜드박스 열밸런스 한 사건.
- 같은 사건의 다른 얼굴이면 related_idx 로 서로 가리키고(양쪽 모두 적는다) related_note 에 이유를 한 문장으로 적는다.
- related_note 는 존칭 없이 짧게("~로 보임", "~확인 필요"). 다른 항목은 번호([n])가 아니라 태그로 가리키고, 태그 표기에 붙은 #번호는 뗀다 — 화면에는 둘 다 없다.
- 같은 사건을 **다시 적은** 항목(같은 태그·같은 시간대·같은 현상)이면 duplicate_of 에 대표 항목 번호를 적는다.
  대표는 더 길게 이어졌거나 중요도가 높은 쪽. 대표 자신은 -1. 항목을 지우는 것이 아니라 표시만 한다 — 승인은 항목 단위다.
- 근거 없이 묶지 않는다 — 확신 없으면 빈 배열, -1. 번호는 입력의 [n] 을 그대로 쓴다(근무 전체 번호).
- 모든 항목을 하나씩, 빠짐없이 돌려준다. idx 와 tag 는 입력의 [n]·태그 표기 그대로(예: TI-301#2)."""


class LLMUnavailable(RuntimeError):
    """AI 를 쓸 수 없다. 호출한 쪽은 이것을 삼키지 말고 그대로 올린다."""


class _Stopped(LLMUnavailable):
    """다른 항목이 끝내 실패해 이 항목은 새 시도를 띄우지 않았다. 원인이 아니라 결과라 사용자에게는 처음 실패를 올린다."""


# 다시 보내 볼 실패 — 타임아웃·CLI/API 오류·깨진 JSON·연결 끊김. 항목마다 부르니 묶음 때보다 호출이 몇 배
# 많고(16항목: 4 → 17회) 한 번의 일시 오류가 근무 전체를 날릴 확률도 그만큼 커졌다. 코드 결함(TypeError 등)은 바로 올린다.
# auto 모드는 한 시도 안에서 CLI 가 실패하면 API 로 한 번 더 부른다(2026-08-28 결정) — 항목당 외부 호출은 최대 4회,
# 실행 전체는 최대 4×(항목 수+1)회다. 어느 항목이 끝내 실패하면 멈춤 신호가 켜지고, 그 뒤로는 새 시도도 CLI→API 전환도
# 시작하지 않는다. 다만 멈춤을 확인한 직후 다른 항목이 멈춤을 켜면, 이미 확인을 통과한 외부 호출(CLI 서브프로세스, 또는 auto 의
# API 전환)은 워커마다 하나까지 시작돼 끝까지 간다. 이 틈을 닫으려면 외부 호출의 시작을 잠금 안에서 해야 하는데, 두 호출 모두
# 시작과 완료가 한 블로킹 호출(subprocess.run·urlopen)이라 결국 호출 전체가 직렬화된다 — 그래서 닫지 않고 한도만 둔다 (Codex).
_RETRY = (subprocess.TimeoutExpired, LLMUnavailable, ValueError, OSError)


def mode():
    """off / cli / api / auto. 설정 없으면 api. auto = cli 로 시도하고 실패하면 API 키가 있을 때만 api 로 (경모님 2026-08-28) —
    키가 없으면 대체 경로가 없으니 그대로 크게 실패한다(조용히 문장 틀로 가지 않음)."""
    return (os.environ.get("ENGRA_LLM") or "api").strip().lower()


def parallel():
    """동시에 띄우는 외부 호출(CLI·API) 수 — 프로세스 전체 상한. 숫자 아님·0 이하는 설정 오류라 크게 실패한다."""
    raw = os.environ.get("ENGRA_LLM_PARALLEL", "8")
    try:
        n = int(raw)
    except ValueError:
        raise LLMUnavailable(f"ENGRA_LLM_PARALLEL 은 정수여야 합니다: {raw!r}") from None
    if n < 1:
        raise LLMUnavailable(f"ENGRA_LLM_PARALLEL 은 1 이상이어야 합니다: {raw!r}")
    return n


_gates, _gates_lock = {}, threading.Lock()


def _gate():
    """외부 호출 자리표 — 모든 rewrite 호출이 나눠 쓴다. 실시간 모드는 항목이 확정될 때마다 rewrite 를 겹쳐 부르므로,
    호출마다 따로 상한을 걸면 동시 수가 곱해진다(ENGRA_LLM_PARALLEL=8 인 호출 둘이면 16). 상한 값마다 하나 — 서버에선 값이 고정이다."""
    n = parallel()
    with _gates_lock:
        if n not in _gates:
            _gates[n] = threading.BoundedSemaphore(n)
        return _gates[n]


def _call(system, user, schema, timeout=TIMEOUT_SEC, stop=None):
    """모드에 따라 호출. auto 는 cli → (키 있으면) api. stop 은 1단계 항목 호출이 넘긴다 — 다른 항목이 끝내 실패했으면 API 로 넘어가지 않는다."""
    m = mode()
    if m == "cli":
        return _call_cli(system, user, schema, timeout)
    if m == "api":
        return _call_api(system, user, schema, timeout)
    if m == "auto":
        try:
            return _call_cli(system, user, schema, timeout)
        except Exception as exc:   # TimeoutExpired·JSONDecodeError·OSError 도 "CLI 실패" 다 — LLMUnavailable 만 잡으면 전환이 안 됐다 (Codex)
            if stop is not None and stop.is_set():
                # CLI 가 도는 사이 다른 항목이 끝내 실패했다 — 새 API 호출을 띄우지 않는다(검증 재현: 동시 4 에서 멈춤 뒤 API 3회)
                raise _Stopped(f"다른 항목이 실패해 API 로 전환하지 않음 (CLI {type(exc).__name__})") from exc
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise LLMUnavailable(f"CLI 실패({type(exc).__name__}: {exc}) — API 키가 없어 전환할 곳이 없습니다") from exc
            print(f"  ⚠ CLI 실패 → API 로 전환: {type(exc).__name__}: {exc}", flush=True)
            return _call_api(system, user, schema, timeout)
    raise LLMUnavailable(f"모르는 ENGRA_LLM 모드: {m}")


_check_lock = threading.Lock()   # 「AI 연결 점검」 동시 클릭 방지 — 한 번에 하나만 (Codex)


def check():
    """관리 페이지 「AI 연결 점검」 — 실제로 한 번 부른다(수 초). → (성공 여부, 한 줄 설명)"""
    m = mode()
    if m == "off":
        return False, "ENGRA_LLM=off — AI 미연결(시험용 설정). 초안 생성이 거부됩니다."
    import time
    if not _check_lock.acquire(blocking=False):
        return False, "점검이 이미 진행 중입니다 — 잠시 뒤 새로고침"
    t0 = time.time()
    try:
        out = _call("답은 JSON {\"items\": []} 만.", "연결 점검. items 는 빈 배열로.", CHECK_SCHEMA)
        return True, f"{status()} · 응답 {time.time() - t0:.1f}s · {type(out).__name__}"
    except Exception as exc:   # 점검은 절대 500 을 내지 않는다 — 실패 사유를 문장으로
        return False, f"{status()} · 실패 {time.time() - t0:.1f}s — {type(exc).__name__}: {exc}"
    finally:
        _check_lock.release()


def status():
    """화면·CLI 에 표시할 상태. engine_source() 와 같은 역할."""
    m = mode()
    if m == "off":
        return "AI 미연결"
    if m == "cli":
        return f"AI cli · {MODEL}"
    if m == "auto":
        return f"AI auto(cli→api) · {MODEL}"
    return f"AI api · {MODEL}"


def _head(shift, n):
    """근무 머리 — 1단계 항목 프롬프트·2단계 연속성 프롬프트가 같이 쓴다."""
    lines = [f"근무: {shift.get('id')}  구간: {shift.get('window_start', '')} ~ {shift.get('window_end', '')}"]
    q = shift.get("quality") if isinstance(shift, dict) else None
    if q and (q.get("bad_rows") or q.get("gaps")):
        lines.append(f"원본 품질: 깨진 행 {q.get('bad_rows', 0)}행 건너뜀({q.get('pattern', '')}) — 태그별 {json.dumps(q.get('by_tag') or {}, ensure_ascii=False)}"
                     + (" · 계측 결측 구간: " + "; ".join(f"{g['tag']} {g['start'][11:16]}~{g['end'][11:16]}" for g in q["gaps"][:5]) if q.get("gaps") else "")
                     + ". 이 태그들의 검출은 결측 구간을 모른 채 나온 것이다 — 관련 항목의 severity_reason 에 신뢰도가 낮다고 적어라.")
    lines.append(f"이번 근무 초안 항목 {n}개.")
    return lines


def _block(idx, it):
    """항목 하나의 블록. 번호는 근무 전체 인덱스다 — 응답의 idx 와 대조한다. 숫자는 여기서 전부 넘긴다."""
    lines = [f"[{idx}] 태그 {it.get('tag')}  중요도 {it.get('severity')}",
             f"    근거: {it.get('evidence')}"]
    m = it.get("metrics") or {}
    if m:
        # k_sigma·r 은 넘기지 않는다 — 주지 않으면 쓸 수 없다(숫자 환각을 막은 것과 같은 방법).
        # 대신 pct_range(정상범위 폭 대비 %)와 실제 단위 값을 준다 (임도영 2026-09-14).
        keep = {k: v for k, v in m.items() if k in (
            "per_hour", "delta", "pct_range", "jump", "step", "amplitude",
            "eta_to_limit_h", "limit", "unit",
            "duration_min", "related_tags", "peak")}
        if keep:
            lines.append(f"    수치: {json.dumps(keep, ensure_ascii=False)}")
    pre = it.get("precedents") or []
    if pre:
        lines.append("    과거 조치 (번호로 판정):")
        for k, p in enumerate(pre[:3]):
            lines.append(f"      [{k}] {p.get('shift_id', '')}: {str(p.get('text', ''))[:200]}")
    return lines


def _when(it):
    """대표 이벤트 시각 HH:MM~HH:MM. pipeline 이 start_ts·end_ts 를 metrics 에 얹어 준다. 원본 품질·확인 질문·맥락 항목은 없을 수 있다."""
    m = it.get("metrics") or {}
    return f"{str(m['start_ts'])[11:16]}~{str(m['end_ts'])[11:16]}" if m.get("start_ts") and m.get("end_ts") else "-"


def _item_prompt(shift, items, idx, context=()):
    """1단계 — 근무 머리 + 항목 1개. 맥락이 없으면 예전과 한 글자도 같다.
    맥락(이미 화면에 나간 항목)이 있으면 뒤에 태그·시각·중요도·제목을 한 줄씩 붙인다 — 항목 하나만 보고 중요도를 매기면
    「다른 이상과 겹치면」 같은 교차 판단을 잃는다(5개 묶음 순차 때는 같은 묶음 항목을 함께 봤다)."""
    lines = _head(shift, len(items))
    lines.append(f"이 중 [{idx}] 하나를 서술한다. idx 는 {idx} 를 그대로 돌려주고 title·body 를 쓰고 severity·handover_worthy·precedent_fit 을 판정하라.")
    lines.append("")
    lines.extend(_block(idx, items[idx]))
    if context:
        lines.append("")
        lines.append(f"이미 화면에 나간 항목 {len(context)}개(같은 근무 앞 항목·이월 항목) — 참고만 한다. 서술하지 말고, 겹치는 이상이 있는지 중요도·전달 가치 판단에만 쓴다:")
        for it in context:
            lines.append(f"  - 태그 {it.get('tag')}  시각 {_when(it)}  중요도 {it.get('severity')}  제목: {str(it.get('title') or '')[:80]}")
    return "\n".join(lines)


def _tag_labels(items):
    """연속성 프롬프트와 응답 대조에 쓰는 태그 표기. 같은 태그가 여러 항목이면 TI-301#1·TI-301#2 로 가른다 —
    되돌려 받은 표기로 번호 어긋남을 가려내는데, 같은 태그끼리 어긋나면 태그만으로는 안 잡힌다 (Codex)."""
    tags = [str(it.get("tag")) for it in items]
    total, seen, labels = Counter(tags), Counter(), []
    for t in tags:
        seen[t] += 1
        labels.append(f"{t}#{seen[t]}" if total[t] > 1 else t)
    return labels


def _link_prompt(shift, items, told, labels, context=()):
    """2단계 — 전 항목의 {번호·태그 표기·시각·제목·중요도·근거 요약}. 제목·중요도는 화면에 나갈 값(새 항목은 1단계 결과)을 쓴다.
    맥락(이미 화면에 나간 항목)은 번호 앞쪽 [0]~[c-1] 에 두고 읽기 전용이라고 적는다. 맥락이 없으면 예전 프롬프트와 한 글자도 같다."""
    c = len(context)
    lines = _head(shift, len(items))
    lines.append("항목마다 idx·tag(아래 태그 표기 그대로)를 돌려주고 related_idx·related_note·duplicate_of 를 판정하라. 번호는 아래 [n] 그대로.")
    if c:
        lines.append(f"[0]~[{c - 1}] 은 이미 화면에 나간 항목(맥락)이다 — 읽기 전용이라 그 행은 돌려주지 않는다. "
                     f"새 항목 [{c}]~[{c + len(items) - 1}] 의 행만 빠짐없이 돌려주고, 새 항목이 맥락 항목과 같은 사건이면 그 번호를 related_idx·duplicate_of 에 쓴다.")
    lines.append("")
    for i, it in enumerate(list(context) + list(items)):
        o = told[i - c] if i >= c else {}
        m = it.get("metrics") or {}
        when = _when(it)
        title = it.get("title") if i < c or it.get("origin") in KEEP_SENTENCES else (o.get("title") or it.get("title"))
        lines.append(f"[{i}] 태그 {labels[i]}  시각 {when}  중요도 {o.get('severity') or it.get('severity')}  제목: {str(title or '')[:80]}")
        lines.append(f"    근거: {str(it.get('evidence') or '')[:160]}")
        keep = {k: v for k, v in m.items() if k in ("duration_min", "related_tags", "eta_to_limit_h")}
        if keep:
            lines.append(f"    수치: {json.dumps(keep, ensure_ascii=False)}")
    return "\n".join(lines)


def _call_cli(system, user, schema, timeout=TIMEOUT_SEC):
    exe = shutil.which("claude")
    if not exe:
        raise LLMUnavailable("ENGRA_LLM=cli 인데 `claude` 실행 파일이 없습니다. 설치하거나 ENGRA_LLM=api 로 바꾸세요.")
    args = [exe, "-p", "--no-session-persistence", "--permission-mode", "dontAsk",
            "--output-format", "json", "--json-schema", json.dumps(schema),
            "--system-prompt", system, user]
    # encoding 을 안 주면 text=True 가 윈도우 로케일(cp949)로 디코딩한다. CLI 응답은 UTF-8
    # 한글이라 리더 스레드에서 UnicodeDecodeError 가 나는데, 그 예외가 삼켜져 returncode 는
    # 0 인데 stdout 만 None 으로 온다. 그 다음 json.loads(None) 이 TypeError 로 터진다.
    r = subprocess.run(args, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        raise LLMUnavailable(f"claude CLI 실패 (exit {r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    if not r.stdout:
        raise LLMUnavailable(
            f"claude CLI 가 빈 응답을 돌려줬습니다 (exit {r.returncode}). stderr: {(r.stderr or '')[-300:]}")
    env = json.loads(r.stdout)
    if env.get("is_error"):
        raise LLMUnavailable(f"claude CLI 오류 응답: {str(env.get('result'))[:300]}")
    out = env.get("structured_output") or env.get("result")
    return out if isinstance(out, dict) else json.loads(out)


def _call_api(system, user, schema, timeout=TIMEOUT_SEC):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMUnavailable(
            "ANTHROPIC_API_KEY 가 없습니다. AI 없이는 초안을 만들지 않습니다.\n"
            "서버: /opt/engra/.env 에 키를 넣고 engra.service 를 재시작하세요.\n"
            "개발: ENGRA_LLM=cli 로 claude CLI 를 쓰거나, 명시적으로 ENGRA_LLM=off 로 끄세요.")
    body = {
        "model": MODEL,
        "max_tokens": 16000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"format": {"type": "json_schema", "schema": schema}},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise LLMUnavailable(f"API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise LLMUnavailable(f"API 연결 실패: {exc}") from exc
    if resp.get("stop_reason") == "refusal":
        raise LLMUnavailable(f"모델이 거부했습니다: {resp.get('stop_details')}")
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    return json.loads(text)


def _why(exc):
    """재시도 로그·실패 문구에 쓰는 한 줄."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return f"{TIMEOUT_SEC}초 안에 응답 없음"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def _tell_one(shift, items, idx, stop, context=()):
    """1단계 — 항목 idx 하나를 서술한다. 스레드 안에서 돈다: 입력은 읽기만 하고 결과는 반환값으로만 넘긴다.

    간헐적으로 빈 응답(output_tokens 0)이 온다 — 서버 첫 시드가 그렇게 죽었다(2026-08-27). 같은 입력을
    다시 보내면 성공했으므로 재시도한다. 타임아웃도 같이 재시도한다 — 부하가 걸린 순간 한 번 늦은 것이
    근무 전체 실행을 날렸다(실측 10회 중 2회, 2026-08-29). ATTEMPTS 번 다 실패하면 그때 LLMUnavailable
    (원칙 ① 그대로 — 조용히 문장 틀로 내려가지 않는다). 다른 항목이 이미 끝내 실패했으면(stop) 새 시도를 띄우지 않는다.
    """
    user = _item_prompt(shift, items, idx, context)
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        with _gate():
            # 자리표를 기다리는 사이 다른 항목이 끝내 실패했을 수 있다 — 자리를 얻은 뒤에 확인한다
            if stop.is_set():
                raise _Stopped(f"항목 {idx}: 다른 항목이 실패해 새 시도를 띄우지 않음")
            try:
                out = _call(SYSTEM, user, ITEM_SCHEMA, stop=stop)
            except _Stopped:
                raise
            except _RETRY as exc:
                last = exc
                print(f"  ⚠ 항목 {idx}: {_why(exc)} (시도 {attempt}/{ATTEMPTS})", flush=True)
            else:
                # 빈 제목·본문도 받지 않는다 — 받으면 _merge 가 문장 틀로 채워 AI 가 쓴 것처럼 저장된다 (Codex)
                if (isinstance(out, dict) and out.get("idx") == idx
                        and isinstance(out.get("title"), str) and out["title"].strip()
                        and isinstance(out.get("body"), str) and out["body"].strip()):
                    return out
                last = None
                print(f"  ⚠ 항목 {idx}: 응답이 그 항목이 아니거나 문장이 비었음 (idx={out.get('idx') if isinstance(out, dict) else type(out).__name__}, 시도 {attempt}/{ATTEMPTS})", flush=True)
            if attempt == ATTEMPTS:
                # 마지막 시도도 실패했다 — 자리를 놓기 전에 멈춤 신호를 켠다. 놓은 뒤(work)에서야 켜면 그 틈에 기다리던
                # 같은 호출의 다른 항목이 자리를 얻어 새 외부 호출을 띄울 수 있었다 (Codex)
                stop.set()
    if isinstance(last, subprocess.TimeoutExpired):
        raise LLMUnavailable(
            f"항목 {idx} 가 {TIMEOUT_SEC}초 안에 안 끝났습니다 ({ATTEMPTS}회 시도). "
            f"부하가 걸렸습니다 — 잠시 뒤 다시 실행하거나 ENGRA_LLM=off 로 넘기세요.") from last
    if last is not None:
        raise LLMUnavailable(f"항목 {idx} 서술이 {ATTEMPTS}회 모두 실패했습니다 — 마지막: {_why(last)}") from last
    raise LLMUnavailable(f"모델이 항목 {idx} 의 서술을 {ATTEMPTS}회 시도에도 제대로 돌려주지 않았습니다(다른 항목 번호이거나 빈 문장). 전부 있어야 합니다.")


def _tell_all(shift, items, say, context=()):
    """1단계 전체 — 동시 상한 안에서 항목마다 호출 1개. 하나라도 끝내 실패하면 멈추고 처음 실패를 올린다.

    스레드는 입력을 읽기만 하고 결과는 future 반환값으로만 돌려준다(공유 dict 에 쓰지 않는다).
    진행 콜백(say)은 이 함수를 부른 스레드에서만 부른다.
    """
    n = len(items)
    workers = min(parallel(), n)   # 스레드 수 — 실제 외부 호출 동시 수는 _gate() 가 프로세스 전체로 묶는다
    stop = threading.Event()
    told = [None] * n
    done = 0
    failure = None

    def work(i):
        try:
            return _tell_one(shift, items, i, stop, context)
        except BaseException:
            # 실패를 본 워커가 직접 막는다 — 메인 스레드가 알아채기 전에 이 워커가 대기 항목을 집어 새 호출을 띄울 수 있었다 (Codex)
            stop.set()
            raise

    if say:
        say(f"AI 서술 시작 — 항목 {n}개를 동시 {workers}개씩, 끝나면 연속성 판정 1회")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(work, i): i for i in range(n)}
        try:
            for fut in as_completed(futs):
                told[futs[fut]] = fut.result()      # 끝내 실패한 항목이면 여기서 터진다
                done += 1
                if say and (done % workers == 0 or done == n):
                    say(f"AI 서술 {done}/{n} 항목 끝남")
        except BaseException as exc:
            # 아직 시작 안 한 항목은 취소한다. 이미 도는 호출은 with 를 나오며 끝날 때까지 기다린 뒤 올린다 —
            # 기다리지 않고 올리면 곧바로 다시 실행했을 때 남은 호출과 겹쳐 부하만 커진다.
            stop.set()
            for f in futs:
                f.cancel()
            failure = exc
            if say:
                why = "" if isinstance(exc, _Stopped) else f"{type(exc).__name__}: {str(exc)[:160]} · "
                say(f"AI 서술 실패 — {why}진행 중인 호출이 끝나면 멈춥니다")
    if failure is not None:
        if isinstance(failure, _Stopped):
            # 멈춤 신호로 그만둔 항목이 먼저 넘어올 수 있다(as_completed 는 이미 끝난 것들을 순서 없이 준다) — 원인을 찾아 올린다
            for f in futs:
                if not f.cancelled() and f.exception() is not None and not isinstance(f.exception(), _Stopped):
                    failure = f.exception()
                    break
        raise failure
    return told


def _tag_key(tag):
    """연속성 응답이 되돌려준 태그 표기와 입력 표기를 대조하는 열쇠. 설명을 붙여 돌려준 표기(「TI-301 (A탑 온도)」)는 앞 토막만 본다.
    태그 없는 확인 질문(None)·원본 품질 요약('-')은 모델이 None·빈 값·'-' 어느 쪽으로 돌려줘도 같은 열쇠다 — 그걸로 응답 전체를 버리지 않게."""
    s = re.split(r"[\s(（]", "" if tag is None else str(tag).strip(), maxsplit=1)[0]
    return "" if s in ("", "None", "null", "-") else s


def _screen_note(note):
    """화면에 나갈 이유 문장 — 프롬프트에서 같은 태그를 가르려고 붙인 #번호와 [n] 번호 가리킴을 뗀다. 화면에는 둘 다 없다."""
    s = re.sub(r"(?<=\d)#\d+", "", str(note or ""))
    return re.sub(r"\s*\[\d+\]", "", s).strip()


def _rows_problem(rows, labels):
    """연속성 응답의 번호를 믿을 수 있는지. → (문제 한 줄 또는 None, 어느 항목인지 가릴 수 없어 그 행만 버릴 번호).

    빠진 항목은 문제로 보지 않는다 — 실측(2026-09-14, 9항목)에서 한 항목을 빠뜨린 응답이 세 번 중 세 번 왔고, 그걸 실패로 보면
    재시도(한 번 54초)를 더 쓰거나 근무 전체의 묶음을 잃었다. 빠진 항목은 호출한 쪽이 판정 없음으로 두고 상태 문구에 적는다.
    번호가 범위 밖·중복이거나 되돌려준 태그가 그 번호의 태그와 다르면 번호가 어긋난 것이라 응답 전체를 버린다.
    같은 태그를 가르려고 붙인 #번호만 떼고 돌려준 행은 그 행만 버린다 — 응답 전체를 버리면 호출 2회 뒤 묶음을 통째로 잃었다(검증 재현).
    """
    # 빈 목록은 구조가 맞는 응답이다 — 전 항목이 빠진 것으로 보고(판정 없음 + 상태 문구) 다시 부르지 않는다.
    # 진짜 빈 출력(output_tokens 0 등)은 _call 에서 예외로 오므로 재시도는 그쪽이 맡는다 (Codex)
    if not isinstance(rows, list):
        return "응답에 items 목록이 없음", set()
    seen, unsure = set(), set()
    for o in rows:
        k = o.get("idx") if isinstance(o, dict) else None
        if type(k) is not int or not 0 <= k < len(labels) or k in seen:
            return f"번호 {k!r} 가 범위 밖이거나 중복", set()
        seen.add(k)
        got, want = _tag_key(o.get("tag")), _tag_key(labels[k])
        if got == want:
            continue
        if "#" in want and got == want.split("#", 1)[0]:
            unsure.add(k)
            continue
        return f"[{k}] 태그 {o.get('tag')!r} ≠ 입력 {labels[k]!r} — 번호 어긋남", set()
    return None, unsure


def _link_all(shift, items, told, context=()):
    """2단계 — 연속성 판정 1회(재시도 포함 ATTEMPTS). → (새 항목별 {related_idx, related_note, duplicate_of}, 응답에서 빠진 새 항목 번호).

    번호는 맥락 + 새 항목을 이은 전체 번호다(맥락이 앞). 새 항목이 맥락 항목을 가리키는 것은 받는다 — 맥락 항목은 이미 화면에 있다.
    묶음 번호는 자기 자신·정수 아님·전체 밖·응답에 없는 새 항목을 버린다. 중복의 대표는 태그가 있는 항목만 — 확인 질문은 사건이 아니다.
    """
    whole = list(context) + list(items)
    c, total = len(context), len(context) + len(items)
    labels = _tag_labels(whole)
    user = _link_prompt(shift, items, told, labels, context)
    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with _gate():
                out = _call(LINK_SYSTEM, user, LINK_SCHEMA)
        except _RETRY as exc:
            last = exc
            print(f"  ⚠ 연속성 판정: {_why(exc)} (시도 {attempt}/{ATTEMPTS})", flush=True)
            continue
        rows = out.get("items") if isinstance(out, dict) else None
        bad, unsure = _rows_problem(rows, labels)
        if bad is None:
            break
        last = LLMUnavailable(bad)
        print(f"  ⚠ 연속성 판정: {bad} (시도 {attempt}/{ATTEMPTS})", flush=True)
    else:
        raise LLMUnavailable(f"연속성 판정 {ATTEMPTS}회 실패 — 마지막: {_why(last)}")

    # 맥락 항목의 행은 읽기 전용이라 판정으로 쓰지 않는다(태그 대조는 이미 통과했다). 새 항목의 행만 받는다.
    got = {o["idx"]: o for o in rows if o["idx"] >= c and o["idx"] not in unsure}
    valid = set(range(c)) | set(got)
    links = []
    for i in range(c, total):
        o = got.get(i) or {}
        asked = o.get("related_idx") if isinstance(o.get("related_idx"), list) else []
        # 빠진 항목은 연속성 판정에서 통째로 빠진다 — 다른 항목이 그 항목을 가리킨 것도 버린다(한쪽에만 묶음이 남지 않게, Codex)
        rel = sorted({k for k in asked if type(k) is int and k in valid and k != i})
        dup = o.get("duplicate_of")
        dropped = (any(type(k) is not int or k not in valid for k in asked if k != i)
                   or (type(dup) is int and dup not in (-1, i) and dup not in valid))
        if not (type(dup) is int and dup in valid and dup != i and whole[dup].get("tag")):
            dup = None
        # 없는 항목을 가리킨 참조를 버렸으면 이유 문장도 버린다 — 남기면 화면에 없는 항목을 가리키는 이유가 된다
        note = "" if dropped else _screen_note(o.get("related_note"))
        links.append({"related_idx": rel, "related_note": note, "duplicate_of": dup})
    return links, [i - c for i in range(c, total) if i not in got]


def _merge(items, told, links, context=()):
    """1·2단계 결과를 새 항목에 얹는다. tag·evidence·precedents·event_id 등 원본은 유지. links 가 None 이면 묶음 없음.
    링크 번호는 맥락 + 새 항목 전체 번호다 — 맥락 항목은 태그·제목을 읽기만 한다(고치지도 돌려주지도 않는다)."""
    rewritten = []
    for i, it in enumerate(items):
        o = told[i]
        new = dict(it)
        if it.get("origin") in KEEP_SENTENCES:
            # 사실 기록·확인 질문 항목은 문장을 그대로 둔다 — 판정(중요도·전달 가치)만 AI 가 한다.
            # quality: 실제 모델이 "계측 결측 — TI-205 07:09~09:40 (150분)" 을 고쳐 쓴 적이 있다 — 시각을 지켰지만 보장이 없다.
            # question: 「플랜트를 정지하셨습니까?」 는 근무자에게 묻는 문장이다 — 고쳐 쓰면 정지를 단정하는 문장이 될 수 있다. tag 도 None 이다.
            new["title"], new["body"] = it["title"], it["body"]
        else:
            # 빈 문장은 _tell_one 이 받지 않았다. 여기서 문장 틀로 채우지 않는다 — 채우면 조용한 폴백이다.
            new["title"] = o["title"].strip()
            new["body"] = o["body"].strip()
        # 조치 문장은 AI 가 쓰지 않는다 — 적합 판정된 과거 확정 일지의 문장만 남는다 (경모님 2026-08-27)
        # 중요도는 AI 가 다시 판단한다. 규칙이 매긴 값은 severity_rule 로 남겨 대비할 수 있게 한다.
        new["severity_rule"] = it.get("severity")
        new["severity"] = o["severity"] if o.get("severity") in ("상", "중", "하") else it.get("severity")
        new["severity_reason"] = (o.get("severity_reason") or "").strip()
        new["handover_worthy"] = bool(o.get("handover_worthy", True))
        # 사례 적합성 — 태그만 같은 사례는 걷어낸다. 원본 목록은 precedents_all 에 남겨 감사할 수 있게.
        pre = it.get("precedents") or []
        fit = [k for k in (o.get("precedent_fit") or []) if isinstance(k, int) and 0 <= k < min(len(pre), 3)]
        new["precedents_all"] = pre
        new["precedents"] = [pre[k] for k in fit]
        new["suggested_action"] = (new["precedents"][0].get("text") if new["precedents"] else None)
        new["precedent_note"] = (o.get("precedent_note") or "").strip()
        rewritten.append(new)
    # 의미 묶음 — 근무 전체 인덱스 → 태그 목록. 중복이면 대표 항목도 같은 사건이니 함께 가리킨다.
    whole = list(context) + rewritten   # 링크 번호는 맥락이 앞인 전체 번호 — 태그·대표 제목은 화면에 보이는 값에서 읽는다
    for i, new in enumerate(rewritten):
        lk = links[i] if links else {"related_idx": [], "related_note": "", "duplicate_of": None}
        dup = lk["duplicate_of"]
        idxs = set(lk["related_idx"]) | ({dup} if dup is not None else set())
        # 태그 없는 항목(정지 확인 질문)은 뺀다 — 섞이면 정렬이 TypeError 로 죽는다. 정지 근무에서 실제로 터졌다 (2026-08-29).
        tags = sorted({whole[j].get("tag") for j in idxs if whole[j].get("tag")})
        note = lk["related_note"]
        if dup is not None:
            # 중복이어도 항목은 지우지 않는다 — 승인은 항목 단위다. 저장 스키마에 칸이 없어 이유 문장 앞에 적는다.
            rep = whole[dup]
            note = f"대표 항목과 같은 사건(중복) — {rep.get('tag')} 「{str(rep.get('title') or '')[:40]}」" + (f" · {note}" if note else "")
        new["related_tags_ai"] = tags
        new["related_note"] = note if tags else ""   # 화면은 가리킬 태그가 있을 때만 이유를 보인다
    return rewritten


def rewrite(shift, items, say=None, context=None):
    """항목의 문장을 AI 로 다시 쓴다. 숫자·근거 필드는 원본 그대로.

    → (항목 목록, 상태 문구). 1단계가 실패하면 LLMUnavailable 을 올린다 — 호출한 쪽(pipeline)이 화면에 그대로 띄운다.
    2단계(연속성)만 실패하면 항목은 돌려주되 related_* 를 비우고 상태 문구에 「연속성 판정 실패」를 붙인다.

    context — 이미 써서 화면에 나간 항목(같은 근무의 앞 항목·앞 근무에서 넘어온 이월 항목). 읽기 전용이다: 문장·필드를 다시 쓰지 않고
    돌려주지도 않는다. 연속성 판정만 새 항목을 맥락 항목과 이어 준다 — 새 항목의 related_tags_ai 에 맥락 항목의 태그, 중복이면
    related_note 앞에 대표(맥락) 항목의 태그·제목(지금 저장 구조 그대로, 새 칸 없음). 실시간 모드가 항목이 확정될 때마다
    새 항목 1~k 개와 함께 넘긴다 (경모님 결정 2026-09-14). 주지 않으면 예전 동작 그대로다.
    """
    context = list(context or [])
    m = mode()
    if m == "off":
        return items, "AI 미연결 (ENGRA_LLM=off) — 문장 틀 그대로"
    if not items:
        return items, status()

    told = _tell_all(shift, items, say, context)
    if len(items) < 2 and not context:
        # 묶을 상대가 없다 — 연속성 호출(실측 30~60초)을 띄우지 않는다. 맥락이 있으면 새 항목 1개도 맥락 항목과 대조한다
        return _merge(items, told, None), status()

    if say:
        say(f"연속성 판정 시작 — 항목 {len(items)}개를 한 번에 대조" if not context
            else f"연속성 판정 시작 — 새 항목 {len(items)}개를 이미 나간 항목 {len(context)}개와 한 번에 대조")
    links, note = None, ""
    try:
        links, missing = _link_all(shift, items, told, context)
        if missing:
            # 일부만 판정된 결과를 온전한 판정처럼 보이지 않게 — 빠진 항목은 묶음 없음으로 두고 그 사실을 적는다
            names = ", ".join(str(items[i].get("tag") or "확인 질문") for i in missing[:5]) + (" 외" if len(missing) > 5 else "")
            note = f" · 연속성 판정에서 빠진 항목 {len(missing)}개({names}) — 그 항목은 묶음 없음"
    except Exception as exc:   # 2단계는 부가 판정이다 — 1단계 결과를 살리고 실패를 문구로 남긴다(숨기지 않는다)
        note = f" · 연속성 판정 실패({type(exc).__name__}: {str(exc)[:120]}) — 같은 사건 묶기 없음"
        print(f"  ⚠ 연속성 판정 실패: {type(exc).__name__}: {exc}", flush=True)
        if say:
            say("연속성 판정 실패 — 항목 서술은 유지, 같은 사건 묶기 없음")
    out = _merge(items, told, links, context)
    if say and links is not None:
        say(f"연속성 판정 끝 — 같은 사건으로 묶인 항목 {sum(1 for o in out if o['related_tags_ai'])}개" + note)
    return out, status() + note
