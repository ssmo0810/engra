"""엔진 자리.

정기영님 `engine/` 산출물이 들어올 자리다. 지금은 `stub_engine` 이 대신 돈다.

**엔진이 준비되면 app 코드는 한 줄도 고치지 않는다.**
`engine/api.py` 에 아래 네 함수가 있으면 이 모듈이 자동으로 그쪽을 쓴다.

    summarize(series)                     -> {태그: 요약}
    build_baseline(summaries)             -> {태그: 기준선}
    detect(series, baselines)             -> [이벤트, ...]
    compose(shift, events, find_precedents) -> [초안항목, ...]

경계에서 주고받는 값은 전부 평범한 dict 다. 세 사람이 각자 다른 방식으로
코드를 써도 붙게 하려는 것이다. 대신 **형식이 어긋나면 조용히 넘어가지 않고
바로 멈춘다**(아래 validate_*). 통합이 깨지는 가장 흔한 원인이 "형식이 조금
달랐는데 아무도 몰랐다" 이기 때문이다.
"""
import sys
from pathlib import Path

_APP = Path(__file__).resolve().parent
_ROOT = _APP.parent
for _p in (str(_APP), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

ENGINE_API = _ROOT / "engine" / "api.py"
STUB_API = _APP / "stub_engine.py"
REQUIRED = ("summarize", "build_baseline", "detect", "compose")


def _check_origin(module, expected, label):
    """의도한 파일이 실제로 로드됐는지 확인한다.

    같은 이름의 다른 모듈이 sys.path 나 sys.modules 를 통해 대신 들어오면
    엉뚱한 코드가 조용히 도는 셈이 된다. 양쪽(엔진·스텁)에 똑같이 건다.
    """
    loaded = Path(getattr(module, "__file__", "") or "").resolve()
    if loaded != expected.resolve():
        raise RuntimeError(
            f"{label} 로 의도하지 않은 파일이 로드되었습니다: {loaded or '(경로 없음)'}\n"
            f"기대한 경로: {expected.resolve()}"
        )
    return module


def _load_engine():
    """engine/api.py 가 준비돼 있으면 그것을, 아니면 스텁을 쓴다.

    **파일이 있는데 못 읽으면 멈춘다.** 문법 오류나 import 실패를 삼키고
    스텁으로 넘어가면, 엔진을 작성한 사람은 화면에 `stub` 이 뜨는 것을 보고
    "파일이 아직 안 붙었나 보다" 라고 오해한다. 실제로는 자기 코드가 깨진
    것이다. 있는지 없는지는 파일 존재로 판정하고, 로드 실패는 그대로 드러낸다.
    """
    # 없는 것과 못 읽는 것을 구분한다. Path.exists() 는 권한 오류도 False 로
    # 돌려주므로, 실제로 열어 보고 FileNotFoundError 만 "없음" 으로 친다.
    try:
        ENGINE_API.open("rb").close()
    except FileNotFoundError:
        import stub_engine as impl  # noqa: PLC0415
        return _check_origin(impl, STUB_API, "임시 엔진"), "stub"
    except OSError as exc:
        raise RuntimeError(f"{ENGINE_API} 에 접근할 수 없습니다: {exc}") from exc

    try:
        from engine import api as impl  # noqa: PLC0415
    except Exception as exc:
        raise RuntimeError(
            f"{ENGINE_API} 를 읽지 못했습니다: {type(exc).__name__}: {exc}\n"
            f"파일이 있으므로 임시 엔진으로 넘어가지 않습니다. 위 오류를 고치거나, "
            f"임시 엔진으로 돌리려면 파일 이름을 잠시 바꾸세요."
        ) from exc

    _check_origin(impl, ENGINE_API, "검출 엔진")

    missing = [f for f in REQUIRED if not callable(getattr(impl, f, None))]
    if missing:
        raise RuntimeError(
            f"engine/api.py 에 다음 함수가 없습니다: {', '.join(missing)}\n"
            f"app/README.md 의 '엔진 자리' 절을 확인하세요."
        )
    return impl, "engine"


_impl, SOURCE = _load_engine()


def engine_source():
    """지금 무엇이 돌고 있는지. 화면과 보고서에 그대로 표시한다."""
    return SOURCE


# --- 규약 검증 --------------------------------------------------------
# 크게 실패하게 둔다. 형식이 어긋난 채로 흘러가면 며칠 뒤에야 발견된다.

EVENT_REQUIRED = ("tag", "kind", "evidence")
ITEM_REQUIRED = ("title", "body")
SEVERITIES = {"상", "중", "하"}


def validate_events(events):
    if not isinstance(events, list):
        raise TypeError(f"detect() 는 목록을 돌려줘야 합니다. 받은 것: {type(events).__name__}")
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            raise TypeError(f"이벤트 {i}: dict 여야 합니다. 받은 것: {type(e).__name__}")
        for key in EVENT_REQUIRED:
            if not e.get(key):
                raise ValueError(
                    f"이벤트 {i}({e.get('tag', '?')}): '{key}' 가 비어 있습니다. "
                    f"필수 항목은 {EVENT_REQUIRED} 입니다."
                )
        sev = e.get("severity")
        if sev is not None and sev not in SEVERITIES:
            raise ValueError(
                f"이벤트 {i}({e['tag']}): severity 는 상/중/하 중 하나여야 합니다. 받은 것: {sev!r}"
            )
    return events


def validate_items(items):
    if not isinstance(items, list):
        raise TypeError(f"compose() 는 목록을 돌려줘야 합니다. 받은 것: {type(items).__name__}")
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise TypeError(f"초안 항목 {i}: dict 여야 합니다. 받은 것: {type(it).__name__}")
        for key in ITEM_REQUIRED:
            if not it.get(key):
                raise ValueError(f"초안 항목 {i}: '{key}' 가 비어 있습니다.")
    return items


# --- 포트 -------------------------------------------------------------

def summarize(series):
    """구간 시계열 -> 태그별 요약 통계."""
    out = _impl.summarize(series)
    if not isinstance(out, dict):
        raise TypeError("summarize() 는 {태그: 요약} 형태여야 합니다.")
    return out


def build_baseline(summaries):
    """지난 근무들의 요약 -> 태그별 기준선."""
    out = _impl.build_baseline(summaries)
    if not isinstance(out, dict):
        raise TypeError("build_baseline() 은 {태그: 기준선} 형태여야 합니다.")
    return out


def detect(series, baselines):
    """구간 시계열 + 기준선 -> 이벤트 목록.

    이벤트 한 건: tag, kind, start_ts, end_ts, severity, score, metrics, evidence
    """
    return validate_events(_impl.detect(series, baselines))


# 정지 확인 질문 (프로토타입 · 2026-08-29)
#
# 플랜트가 정지하면 모든 Unit 이 동시에 범위를 벗어나 초안이 22~40항목으로 불어난다.
# 사고 직후에 그걸 다 읽을 사람은 없다. 그렇다고 시스템이 "정지다" 라고 단정해 묶어
# 버리면 틀렸을 때 되돌릴 수가 없다 — 계획 정지인지 Trip 인지 대규모 계측 장애인지는
# 사람만 안다. 그래서 **묻는다**. 답은 코멘트로 일지에 남아 다음 근무의 맥락이 된다.
#
# 문턱은 실측으로 정했다. 근무 전체의 이상 태그 비율로는 안 갈린다 — 이상 12종을 넣은
# 정상 근무가 32%, 정지가 87% 로 겹친다. **10분 창 안에 동시에 시작된 태그 비율**로
# 보면 정지 69.8~90.6% 대 이상 주입 7.5% 로 62%p 가 벌어진다.
#
# 검출 엔진은 건드리지 않는다. 여기는 초안을 조립하는 자리다.
STOP_WINDOW_MIN = 10
STOP_TAG_RATIO = 0.30


def _stop_burst(events):
    """10분 창 안에 함께 시작된 태그가 가장 많았던 지점. (태그 수, 시각, 먼저 움직인 태그)"""
    from datetime import datetime, timedelta
    rows = []
    for e in events:
        ts = e.get("start_ts")
        if ts:
            rows.append((e["tag"], datetime.fromisoformat(ts)))
    rows.sort(key=lambda r: r[1])
    best, at = 0, None
    for _, t0 in rows:
        tags = {t for t, ts in rows if t0 <= ts < t0 + timedelta(minutes=STOP_WINDOW_MIN)}
        if len(tags) > best:
            best, at = len(tags), t0
    if at is None:
        return 0, None, []
    return best, at, [t for t, ts in rows if ts < at + timedelta(seconds=30)][:4]


def _tag_count():
    """태그 마스터 점수. 엔진이 있으면 그쪽 것을 쓴다."""
    tags = getattr(_impl, "TAGS", None)
    if tags is None:
        try:
            from engine.core import TAGS as tags
        except Exception:
            return 0
    return len(tags)


def _stop_question(events):
    n_tags = _tag_count()
    burst, at, first = _stop_burst(events)
    if not n_tags or burst < n_tags * STOP_TAG_RATIO:
        return None
    pct = 100.0 * burst / n_tags
    when = at.strftime("%H:%M")
    head = ", ".join(first) if first else "확인 필요"
    body = (
        "{w} 부터 10분 안에 태그 {b}개(전체의 {p:.0f}%)가 한꺼번에 정상 범위를 벗어났습니다. "
        "계획 정지·Trip·대규모 계측 장애 중 어느 것인지는 데이터만으로 판단할 수 없어 여쭙니다.\n"
        "가장 먼저 움직인 태그: {h}\n"
        "· 정지가 맞다면 — 아래 항목들은 정지에 따른 결과입니다. 「정지 사건」으로 한 번에 "
        "처리하시고 정지 사유와 first-out 을 코멘트에 남겨 주십시오.\n"
        "· 정지가 아니라면 — 동시 다발 이탈이라 계측·통신 계통을 먼저 확인해 주십시오."
    ).format(w=when, b=burst, p=pct, h=head)
    return {
        "event_id": None,
        "origin": "question",
        "tag": None,
        "severity": "상",
        "title": "플랜트를 정지하셨습니까? — {} 경 {}개 태그가 동시에 이탈".format(when, burst),
        "body": body,
        "evidence": "10분 창 동시 이탈 {}/{} 태그 ({:.0f}%) · 문턱 {:.0f}%".format(
            burst, n_tags, pct, STOP_TAG_RATIO * 100),
        "precedents": [],
    }


def compose(shift, events, find_precedents):
    """이벤트 + 과거 사례 -> 초안 항목 목록.

    find_precedents(tag, query) 를 넘겨 준다. 엔진은 저장소를 몰라도 된다.
    """
    items = validate_items(_impl.compose(shift, events, find_precedents))
    q = _stop_question(events)
    if q:
        items = [q] + items      # 40항목을 읽기 전에 이 한 줄을 먼저 본다
    return items
