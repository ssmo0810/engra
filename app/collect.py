"""수집부.

지금은 임도영님 생성기가 뽑은 CSV 를 읽는다. 실배포에서는 RTDB 조회로
바뀌지만, **바뀌는 것은 이 파일의 Source 클래스 하나뿐**이다. 나머지 코드는
"구간을 주면 (시각, 태그, 값) 을 흘려준다" 만 알면 된다.

RTDB 는 데이터를 넘겨주는 통로이지 보관하는 곳이 아니다. 값을 쌓고 지난
근무와 비교하는 주체는 ENGRA 다 — 그래서 수집한 것을 여기서 저장소에 적재한다.
"""
import math
import csv
import io
import urllib.request
from datetime import datetime, timedelta

import db
from config import SAMPLE_INTERVAL_SEC, SHIFT_HOURS, SHIFT_WINDOW_START_HOUR

BATCH = 5000


# --- 근무 구간 --------------------------------------------------------

def window_of(kind, date):
    """근무 종류와 날짜 -> (구간 시작, 구간 끝).

    분석 구간은 교대 1시간 전을 경계로 한다. 주간조(07~19시) 초안은
    06:00~18:00 을 다루고, 야간조는 18:00~06:00 이다. 두 구간이 맞물려
    시간축에 빈틈이 없다.
    """
    if kind not in SHIFT_WINDOW_START_HOUR:
        raise ValueError(f"근무 종류는 day 또는 night 입니다. 받은 것: {kind!r}")
    start = datetime(date.year, date.month, date.day, SHIFT_WINDOW_START_HOUR[kind])
    return start, start + timedelta(hours=SHIFT_HOURS)


def shift_id_for(ts):
    """시각 하나가 어느 근무 구간에 속하는지 판정한다."""
    day_start, day_end = window_of("day", ts.date())
    if day_start <= ts < day_end:
        return f"{ts.date()}-day", "day", day_start, day_end
    # 06시 이전은 전날 야간조에 속한다.
    base = ts.date() if ts.hour >= SHIFT_WINDOW_START_HOUR["night"] else ts.date() - timedelta(days=1)
    start, end = window_of("night", base)
    return f"{base}-night", "night", start, end


# --- 데이터 소스 ------------------------------------------------------

MAX_BAD_RATIO = 0.01   # 이 이상 깨졌으면 노이즈가 아니라 파일이 잘못된 것이다 — 단, 오래 끊긴 태그의 행은 빼고 센다
RUN_MIN_ROWS = 300     # 한 태그가 이만큼(2초 간격 10분) 깨졌으면 '오래 끊긴 태그' 로 본다 — 그 행은 산발 상한에서 뺀다


class BadRows:
    """건너뛴 행의 집계. ingest 가 끝나면 크게 보고한다 — 조용히 넘기지 않는다.
    max_ratio=None 이면 비율 상한 없이 전부 건너뛴다 — 사용자가 화면에서 "깨진 행을 건너뛰고 적재" 를 명시적으로
    누른 경우(경모님 2026-08-27, 생성기 _mix 파일). 그 사실은 근무의 품질 기록(shift.quality_json)에 남는다."""

    def __init__(self, max_ratio=None):
        self.count = 0
        self.total = 0
        self.samples = []
        self.max_ratio = max_ratio          # None = 상한 없음. 기본 호출자는 MAX_BAD_RATIO 를 넘긴다
        self.by_shift = {}       # shift_id -> {tag: n}  — 어느 근무·어느 태그가 얼마나 깨졌나
        self.times = {}          # (shift_id, tag) -> [ts 문자열] — 연속 결측인지 산발인지 가르는 재료
        self.note = None         # 오래 끊긴 태그가 있어 그 행을 상한에서 뺀 사실
        self._ntimes = 0

    def add(self, lineno, raw, exc):
        self.count += 1
        if len(self.samples) < 5:
            self.samples.append(f"{lineno}: {raw!r} — {exc}")
        ts, tag = (raw[0], raw[1]) if isinstance(raw, tuple) and len(raw) >= 2 else (None, None)
        try:
            sid = shift_id_for(datetime.fromisoformat(ts))[0] if ts else "?"
        except (ValueError, TypeError):
            sid = "?"
        self.by_shift.setdefault(sid, {})
        self.by_shift[sid][tag or "?"] = self.by_shift[sid].get(tag or "?", 0) + 1
        if sid != "?" and self._ntimes < 200000:      # 시각은 20만 개까지만 저장(메모리) — 그 뒤엔 집계만 계속 (Codex)
            self.times.setdefault((sid, tag or "?"), []).append(ts)
            self._ntimes += 1

    def dominant_tags(self):
        """오래 끊긴 태그(깨진 행 RUN_MIN_ROWS 이상) → [(tag, n)] 많은 순."""
        agg = {}
        for sid, tags in self.by_shift.items():
            for t, n in tags.items():
                if t != "?":
                    agg[t] = agg.get(t, 0) + n
        return sorted(((t, n) for t, n in agg.items() if n >= RUN_MIN_ROWS), key=lambda x: -x[1])

    def scattered(self):
        """오래 끊긴 태그의 행을 뺀 나머지 깨진 행 — 상한은 여기에 건다."""
        return self.count - sum(n for _, n in self.dominant_tags())

    def pattern(self, gap_min_minutes=10):
        """깨진 행이 어떤 모양인가 — 경모님(2026-08-27) 케이스 구분.
        '연속': 한 태그가 gap_min 분 넘게 이어서 비었다(계측·통신 끊김 → 초안에 결측 구간으로 넣는다).
        '산발': 여러 태그에 흩어져 짧게 비었다(전송 문제 → 다시 내려받기를 권한다).
        → (종류, 연속 구간 목록 [{tag, start, end, minutes}], 영향 태그 수)"""
        runs = []
        for (sid, tag), ts_list in self.times.items():
            if tag == "?":
                continue
            ts_list = sorted(ts_list)
            try:
                dts = [datetime.fromisoformat(t) for t in ts_list]
            except (ValueError, TypeError):
                continue
            start = prev = dts[0]; nrows = 1
            for cur in dts[1:] + [None]:
                if cur is not None and (cur - prev).total_seconds() <= SAMPLE_INTERVAL_SEC * 3:
                    prev = cur; nrows += 1
                    continue
                mins = (prev - start).total_seconds() / 60
                if mins >= gap_min_minutes:
                    runs.append({"shift_id": sid, "tag": tag, "start": start.isoformat(timespec="seconds"), "end": prev.isoformat(timespec="seconds"),
                                 "minutes": round(mins), "rows": nrows})   # rows = 이 연속 구간에 든 깨진 행 수 — 요약에서 정확히 뺄 수 있게 (Codex)
                if cur is not None:
                    start = prev = cur; nrows = 1
        tags = {tag for (_, tag) in self.times if tag != "?"}
        kind = "연속" if runs else ("산발" if len(tags) >= 3 else "소량")
        runs.sort(key=lambda r: -r["minutes"])
        return kind, runs, len(tags)

    def quality(self, shift_id, skipped_by_user):
        """한 근무의 품질 기록. 이 근무의 깨진 행도, 시각이 깨져 근무를 알 수 없는 행(미귀속)도 없으면 None."""
        tags = self.by_shift.get(shift_id) or {}
        n = sum(tags.values())
        unattributed = sum((self.by_shift.get("?") or {}).values())   # 시각 자체가 깨진 행 — 어느 근무인지 몰라 파일의 모든 근무에 알린다 (Codex)
        if not n and not unattributed:
            return None
        kind, runs, ntags = self.pattern()
        return {"bad_rows": n, "unattributed_rows": unattributed, "total_rows_seen": self.total, "pattern": kind, "affected_tags": ntags,
                "runs": [r for r in runs if r["shift_id"] == shift_id][:10],
                "ratio_file": round(self.count / self.total, 4) if self.total else None,
                "by_tag": dict(sorted(tags.items(), key=lambda kv: -kv[1])), "samples": self.samples[:3], "skipped_by_user": bool(skipped_by_user)}


def _rows(fh, label, bad=None):
    """열린 텍스트 스트림 -> (ts, tag, value). CsvSource·UrlSource 가 같이 쓴다.

    기본은 첫 형식 오류에서 줄 번호와 함께 멈춘다 — 어디서 어떻게 깨졌는지가 곧 검증 기록이다.
    `bad` 를 넘기면 깨진 행을 건너뛰고 세어 둔다(--skip-bad-rows). 단 1% 를 넘으면 멈춘다:
    그건 노이즈가 아니라 잘못된 파일이고, 그런 파일로 만든 초안은 신뢰할 수 없다.
    """
    reader = csv.DictReader(fh)
    missing = {"timestamp", "tag", "value"} - set(reader.fieldnames or [])
    if missing:
        raise ValueError(
            f"{label}: 열이 없습니다 {sorted(missing)}. "
            f"연결 규약은 timestamp,tag,value 입니다. 실제 열: {reader.fieldnames}"
        )
    for lineno, row in enumerate(reader, start=2):
        if bad is not None:
            bad.total += 1
        try:
            ts = datetime.fromisoformat(row["timestamp"])
            # 연결 규약은 **무시간대 현지시각**이다. 시간대가 붙어 오면 근무 배정에서
            # offset-naive 와 비교하다 TypeError 로 죽었다. 조용히 떼어내면 UTC 표기를
            # 현지시각으로 오인해 9시간 밀린 근무에 넣게 되므로, 추측하지 않고 이유를
            # 밝혀 거부한다.
            if ts.tzinfo is not None:
                raise ValueError(
                    f"시간대가 붙어 있습니다: {row['timestamp']!r}. "
                    f"연결 규약은 시간대 없는 현지시각입니다 (예: 2026-08-28T06:00:00)")
            value = float(row["value"])
            # float() 은 NaN·Infinity 를 예외 없이 통과시킨다. NaN 은 저장 단계에서 NOT NULL 로
            # 죽고 Infinity 는 조용히 저장되므로, 여기서 깨진 값으로 함께 잡는다 (#20).
            if not math.isfinite(value):
                raise ValueError(f"유한한 값이 아닙니다: {row['value']!r}")
        except (ValueError, TypeError) as exc:
            if bad is None:
                raise ValueError(f"{label}:{lineno} 읽기 실패 — {exc}") from exc
            bad.add(lineno, (row.get("timestamp"), row.get("tag"), row.get("value")), exc)
            # 산발 행이 300행(한 태그 10분 분량)은 돼야 비율을 따진다 — 근무 시작 직후부터 끊긴 태그가 300행이 쌓여 '오래 끊긴 태그' 로
            # 분류되기 전에 1% 를 넘어 거부되는 것을 막는다 (Codex). 300행이 되는 순간 그 태그는 dominant 로 빠져 산발이 0 이 된다.
            if bad.max_ratio is not None and bad.total >= 1000 and bad.scattered() >= RUN_MIN_ROWS and bad.scattered() / bad.total > bad.max_ratio:
                # 한 태그가 오래 끊긴 것(계측·통신 장애)은 파일이 잘못된 게 아니다 — 한 태그는 전체 행의 1/53 이라 150분만
                # 끊겨도 누적 1% 를 넘는다(실측). 그래서 상한은 '산발' 행(오래 끊긴 태그의 행을 뺀 나머지)에만 건다.
                # 연속 결측은 통과해 결측 구간으로 남고, 그 뒤에 산발 오류가 섞이면 여전히 잡힌다 (Codex).
                raise ValueError(
                    f"{label}: {bad.total}행 중 {bad.count}행이 깨졌습니다 (산발 {bad.scattered()}행, {bad.scattered()/bad.total:.1%}). "
                    f"1% 를 넘어 노이즈가 아니라 잘못된 파일로 봅니다. 예: {bad.samples[0]}")
            if bad.count and bad.note is None and bad.count - bad.scattered() >= RUN_MIN_ROWS:
                bad.note = "연속 결측으로 판단해 계속 적재 — " + ", ".join(f"{t} {n:,}행" for t, n in bad.dominant_tags()[:3])
            continue
        yield ts, row["tag"].strip(), value


class CsvSource:
    """`timestamp,tag,value` 형식 CSV. app/README.md 연결 규약 ①."""

    def __init__(self, path, bad=None):
        self.path = path
        self.name = f"csv:{path.name}"
        self.bad = bad

    def read(self):
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            yield from _rows(f, self.path.name, self.bad)


class UrlSource:
    """URL 의 CSV. 형식은 CsvSource 와 같다 (#12, 임도영 제안).

    사람이 파일을 받아 경로를 치는 대신 ENGRA 가 직접 가져간다. 실배포에서 RTDB 조회로
    바뀌어도 바뀌는 것은 이 Source 클래스 하나뿐이라는 설계를 그대로 따른다.
    36MB 를 스트리밍으로 읽으므로 메모리에 다 올리지 않는다. 캐시는 두지 않았다 —
    같은 URL 을 여러 번 적재할 일이 드물고, 두면 "어느 버전을 읽었나" 가 흐려진다.
    """

    def __init__(self, url, bad=None):
        self.url = url
        self.name = f"url:{url.rsplit('/', 1)[-1]}"
        self.bad = bad

    def read(self):
        req = urllib.request.Request(self.url, headers={"User-Agent": "engra-collect/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            if r.status != 200:
                raise ValueError(f"{self.url}: HTTP {r.status}")
            with io.TextIOWrapper(r, encoding="utf-8-sig", newline="") as f:
                yield from _rows(f, self.name, self.bad)


def source_for(arg, bad=None):
    """ingest 인자 -> Source. http(s) 면 URL, 아니면 파일 경로. bad 를 주면 깨진 행을 건너뛴다."""
    from pathlib import Path
    if str(arg).startswith(("http://", "https://")):
        return UrlSource(str(arg), bad)
    return CsvSource(Path(arg), bad)


class RtdbSource:
    """실배포용 자리. 지금은 비어 있다.

    지정 시각에 구간을 통째로 조회하는 방식이라 실시간 연결이 필요 없다.
    """

    def __init__(self, *_, **__):
        raise NotImplementedError(
            "RTDB 연동은 실배포 단계 작업입니다. 지금은 CsvSource 를 씁니다."
        )


# --- 적재 -------------------------------------------------------------

def ingest(source):
    """소스를 읽어 원본 시계열로 적재하고, 걸쳐 있는 근무 구간을 등록한다.

    한 파일에 여러 근무가 섞여 있어도 시각을 보고 알아서 나눈다.
    """
    counts, shifts, buf = {}, {}, []

    with db.connect() as conn:
        for ts, tag, value in source.read():
            sid, kind, start, end = shift_id_for(ts)
            if sid not in shifts:
                shifts[sid] = (kind, start, end)
                # 같은 근무를 다시 올리면 병합이 아니라 교체 — 옛 원본(다른 시각·다른 값)이 섞여 남지 않게 (홀리스틱 Codex).
                # 이 파일의 이 근무 행은 아직 DB 에 없다(buf 에 있음)므로 지워지지 않는다.
                _flush(conn, buf)
                db.forget_raw(conn, sid)
            counts[sid] = counts.get(sid, 0) + 1
            buf.append((tag, ts.isoformat(timespec="seconds"), value))
            if len(buf) >= BATCH:
                _flush(conn, buf)

        _flush(conn, buf)

        for sid, (kind, start, end) in shifts.items():
            db.upsert_shift(
                conn, sid, kind,
                start.isoformat(timespec="seconds"),
                end.isoformat(timespec="seconds"),
                source.name,
            )

    return counts


def _flush(conn, buf):
    if not buf:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO raw_sample (tag, ts, value) VALUES (?,?,?)", buf
    )
    buf.clear()


def coverage(conn, shift_id):
    """구간이 얼마나 채워졌는지. 데이터 유실을 조용히 넘기지 않기 위한 것."""
    row = conn.execute(
        "SELECT window_start, window_end FROM shift WHERE id = ?", (shift_id,)
    ).fetchone()
    if row is None:
        return None
    span = (
        datetime.fromisoformat(row["window_end"]) - datetime.fromisoformat(row["window_start"])
    ).total_seconds()
    expected_per_tag = int(span / SAMPLE_INTERVAL_SEC)
    stat = conn.execute(
        """SELECT COUNT(DISTINCT tag) AS tags, COUNT(*) AS n FROM raw_sample
           WHERE ts >= ? AND ts < ?""",
        (row["window_start"], row["window_end"]),
    ).fetchone()
    expected = expected_per_tag * (stat["tags"] or 0)
    return {
        "tags": stat["tags"],
        "samples": stat["n"],
        "expected": expected,
        "ratio": round(stat["n"] / expected, 4) if expected else 0.0,
    }
