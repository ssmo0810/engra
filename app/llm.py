"""초안 서술을 AI 로 쓴다. `compose()` 가 만든 항목의 **문장만** 다시 쓰고 숫자·근거는 건드리지 않는다.

두 가지 원칙을 코드로 강제한다.

1. **AI 는 필수다.** 설정이 없거나 호출이 실패하면 초안 생성을 멈추고 이유를 그대로 올린다.
   조용히 문장 틀로 내려가지 않는다 — 기획서 3-5 가 "AI 없으면 업무 자체가 성립하지 않는다" 고
   주장하는데 코드가 AI 없이 돌면 그 주장이 거짓이 된다. 끄려면 `ENGRA_LLM=off` 를 명시해야
   하고, 그때 화면에 `AI 미연결` 이 뜬다 (엔진의 engine/stub 배지와 같은 관례).

2. **숫자는 모델을 지나가지 않는다.** 모델은 `evidence`·`metrics` 를 **입력**으로 받아 문장만
   만들고, 출력 스키마에 수치 필드가 없다. 지어낼 자리 자체를 없앤다. `tag`·`severity`·
   `evidence`·`precedents` 는 원본 그대로 통과한다.

호출 경로는 둘이고 환경변수 `ENGRA_LLM` 으로 고른다.
  cli — `claude -p --json-schema` 서브프로세스. 개발·프롬프트 튜닝용 (구독, 비용 0)
  api — Anthropic Messages API 직접 호출. 서버 무인 실행용. 키는 ANTHROPIC_API_KEY
  off — AI 끔. 문장 틀 그대로. 화면에 미연결 표시
설정이 없으면 api 를 시도하고, 키가 없으면 실패한다 — 기본값이 "AI 켜짐" 이다.

표준 라이브러리만 쓴다. api 경로도 urllib 로 직접 호출해 SDK 의존을 만들지 않는다.
"""
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

MODEL = os.environ.get("ENGRA_LLM_MODEL", "claude-opus-5")
API_URL = "https://api.anthropic.com/v1/messages"
TIMEOUT_SEC = 300
BATCH = 5          # 한 호출에 넣는 항목 수. 16개 한 번에 넣으면 120초를 넘겼다 (실측). 5개면 ~40초

# 모델이 채울 수 있는 것은 이 세 필드뿐이다. 숫자 필드는 없다.
ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer", "description": "입력 항목의 순번. 그대로 돌려준다"},
                    "title": {"type": "string", "description": "한 줄 제목. 태그와 현상"},
                    "body": {"type": "string", "description": "인수인계 문장 2~4개. 근거의 숫자를 그대로 인용"},
                    "suggested_action": {"type": "string", "description": "다음 근무자에게 권하는 조치 한 문장. 과거 사례가 있으면 그것을 근거로"},
                    "severity": {"type": "string", "enum": ["상", "중", "하"],
                                 "description": "인수인계 관점의 중요도. 통계 크기가 아니라 '놓치면 무엇이 일어나는가'로 판단"},
                    "severity_reason": {"type": "string", "description": "그 중요도로 판단한 이유 한 문장. 근무자가 읽고 동의할 수 있어야 한다"},
                    "handover_worthy": {"type": "boolean", "description": "다음 근무자에게 실제로 전달할 가치가 있는가. 외기 변동처럼 정상 운전의 일부면 false"},
                    "precedent_fit": {"type": "array", "items": {"type": "integer"},
                                      "description": "제시된 과거 조치 중 이번 증상에 실제로 맞는 것의 번호(0부터). 태그만 같고 증상·조치가 다르면 넣지 않는다. 없으면 빈 배열"},
                    "precedent_note": {"type": "string", "description": "과거 조치를 채택/기각한 이유 한 문장. 사례가 없었으면 빈 문자열"},
                },
                "required": ["idx", "title", "body", "suggested_action", "severity", "severity_reason", "handover_worthy", "precedent_fit", "precedent_note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM = """당신은 24시간 연속 공정(공기분리장치, ASU) 교대 근무의 인수인계 초안을 쓰는 보조자다.
통계 검출기가 이번 근무에서 잡아낸 이상 후보와 그 근거 수치, 그리고 과거 확정 일지의 조치를 받아
다음 근무자가 읽을 문장을 쓴다.

지켜야 할 것:
- 숫자는 주어진 근거(evidence, metrics)에 있는 값만 쓴다. 새 숫자를 만들지 않는다.
- 판단하지 않은 것을 단정하지 않는다. 원인이 불확실하면 "의심됨" 으로 쓴다.
- 문장은 현장 근무자가 쓰는 말투로 짧게. 존칭 없이 "~됨", "~확인 필요" 형태.
- 과거 사례가 있으면 그 조치를 참고해 suggested_action 을 쓴다. 없으면 관찰·확인 위주로.
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
맞는 사례만 precedent_fit 에 번호로 적고, suggested_action 은 그 사례에 근거해 쓴다.
맞는 사례가 없으면 precedent_fit 은 빈 배열, suggested_action 은 관찰·확인 위주로 새로 쓴다."""


class LLMUnavailable(RuntimeError):
    """AI 를 쓸 수 없다. 호출한 쪽은 이것을 삼키지 말고 그대로 올린다."""


def mode():
    """off / cli / api. 설정 없으면 api."""
    return (os.environ.get("ENGRA_LLM") or "api").strip().lower()


def status():
    """화면·CLI 에 표시할 상태. engine_source() 와 같은 역할."""
    m = mode()
    if m == "off":
        return "AI 미연결"
    if m == "cli":
        return f"AI cli · {MODEL}"
    return f"AI api · {MODEL}"


def _prompt(shift, items):
    """항목 목록 -> 모델에 줄 사용자 메시지. 숫자는 여기서 전부 넘긴다."""
    lines = [f"근무: {shift.get('id')}  구간: {shift.get('window_start', '')} ~ {shift.get('window_end', '')}",
             f"항목 {len(items)}개. 각 항목의 idx 를 그대로 돌려주고 title·body·suggested_action 만 써라.", ""]
    for i, it in enumerate(items):
        lines.append(f"[{i}] 태그 {it.get('tag')}  중요도 {it.get('severity')}")
        lines.append(f"    근거: {it.get('evidence')}")
        m = it.get("metrics") or {}
        if m:
            keep = {k: v for k, v in m.items() if k in (
                "per_hour", "delta", "k_sigma", "eta_to_limit_h", "limit", "unit",
                "duration_min", "related_tags", "peak", "r")}
            if keep:
                lines.append(f"    수치: {json.dumps(keep, ensure_ascii=False)}")
        pre = it.get("precedents") or []
        if pre:
            lines.append("    과거 조치 (번호로 판정):")
            for k, p in enumerate(pre[:3]):
                lines.append(f"      [{k}] {p.get('shift_id', '')}: {str(p.get('text', ''))[:200]}")
        lines.append("")
    return "\n".join(lines)


def _call_cli(system, user):
    exe = shutil.which("claude")
    if not exe:
        raise LLMUnavailable("ENGRA_LLM=cli 인데 `claude` 실행 파일이 없습니다. 설치하거나 ENGRA_LLM=api 로 바꾸세요.")
    args = [exe, "-p", "--no-session-persistence", "--permission-mode", "dontAsk",
            "--output-format", "json", "--json-schema", json.dumps(ITEM_SCHEMA),
            "--system-prompt", system, user]
    r = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_SEC)
    if r.returncode != 0:
        raise LLMUnavailable(f"claude CLI 실패 (exit {r.returncode}): {(r.stderr or r.stdout)[-400:]}")
    env = json.loads(r.stdout)
    if env.get("is_error"):
        raise LLMUnavailable(f"claude CLI 오류 응답: {str(env.get('result'))[:300]}")
    out = env.get("structured_output") or env.get("result")
    return out if isinstance(out, dict) else json.loads(out)


def _call_api(system, user):
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
        "output_config": {"format": {"type": "json_schema", "schema": ITEM_SCHEMA}},
    }
    req = urllib.request.Request(
        API_URL, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as r:
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


def rewrite(shift, items):
    """항목의 문장을 AI 로 다시 쓴다. 숫자·근거 필드는 원본 그대로.

    실패하면 LLMUnavailable 을 올린다. 호출한 쪽(pipeline)이 이것을 화면에 그대로 띄운다.
    """
    m = mode()
    if m == "off":
        return items, "AI 미연결 (ENGRA_LLM=off) — 문장 틀 그대로"
    if not items:
        return items, status()

    # 배치로 나눠 부른다. 한 번에 다 넣으면 응답이 길어져 타임아웃에 걸린다.
    # 배치 안 idx 는 0부터 다시 매기고, 합칠 때 원래 순번으로 되돌린다.
    by_idx = {}
    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        user = _prompt(shift, chunk)
        out = _call_cli(SYSTEM, user) if m == "cli" else _call_api(SYSTEM, user)
        got = {o["idx"]: o for o in out.get("items", []) if isinstance(o.get("idx"), int)}
        if len(got) != len(chunk):
            raise LLMUnavailable(f"모델이 배치 {start//BATCH+1} 의 {len(chunk)}개 중 {len(got)}개만 돌려줬습니다. 전부 있어야 합니다.")
        for local, o in got.items():
            by_idx[start + local] = o

    rewritten = []
    for i, it in enumerate(items):
        o = by_idx.get(i)
        if o is None:
            raise LLMUnavailable(f"항목 {i} 의 응답이 없습니다.")
        new = dict(it)                          # tag·evidence·precedents·event_id 등 원본 유지
        new["title"] = o["title"].strip() or it["title"]
        new["body"] = o["body"].strip() or it["body"]
        new["suggested_action"] = o["suggested_action"].strip() or it.get("suggested_action")
        # 중요도는 AI 가 다시 판단한다. 규칙이 매긴 값은 severity_rule 로 남겨 대비할 수 있게 한다.
        new["severity_rule"] = it.get("severity")
        new["severity"] = o["severity"] if o.get("severity") in ("상", "중", "하") else it.get("severity")
        new["severity_reason"] = o.get("severity_reason", "").strip()
        new["handover_worthy"] = bool(o.get("handover_worthy", True))
        # 사례 적합성 — 태그만 같은 사례는 걷어낸다. 원본 목록은 precedents_all 에 남겨 감사할 수 있게.
        pre = it.get("precedents") or []
        fit = [k for k in (o.get("precedent_fit") or []) if isinstance(k, int) and 0 <= k < min(len(pre), 3)]
        new["precedents_all"] = pre
        new["precedents"] = [pre[k] for k in fit]
        new["precedent_note"] = (o.get("precedent_note") or "").strip()
        rewritten.append(new)
    return rewritten, status()
