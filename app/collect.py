"""수집부.

지금은 임도영님 생성기가 뽑은 CSV 를 읽는다. 실배포에서는 RTDB 조회로
바뀌지만, **바뀌는 것은 이 파일의 Source 클래스 하나뿐**이다. 나머지 코드는
"구간을 주면 (시각, 태그, 값) 을 흘려준다" 만 알면 된다.

RTDB 는 데이터를 넘겨주는 통로이지 보관하는 곳이 아니다. 값을 쌓고 지난
근무와 비교하는 주체는 ENGRA 다 — 그래서 수집한 것을 여기서 저장소에 적재한다.
"""
import csv
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

class CsvSource:
    """`timestamp,tag,value` 형식 CSV. app/README.md 연결 규약 ①."""

    def __init__(self, path):
        self.path = path
        self.name = f"csv:{path.name}"

    def read(self):
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = {"timestamp", "tag", "value"} - set(reader.fieldnames or [])
            if missing:
                raise ValueError(
                    f"{self.path.name}: 열이 없습니다 {sorted(missing)}. "
                    f"연결 규약은 timestamp,tag,value 입니다. 실제 열: {reader.fieldnames}"
                )
            for lineno, row in enumerate(reader, start=2):
                try:
                    ts = datetime.fromisoformat(row["timestamp"])
                    value = float(row["value"])
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"{self.path.name}:{lineno} 읽기 실패 — {exc}") from exc
                yield ts, row["tag"].strip(), value


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
