"""저장소.

app/README.md 에 적은 6종을 SQLite 한 파일에 담는다.
논리적으로는 분리돼 있고 물리적으로만 한 파일이다 — 배포 시 원본 시계열만
별도 저장소로 떼어내면 되도록 테이블 경계를 지켰다.

  1) raw_sample     원본 시계열   (단기 회전, RAW_RETENTION_DAYS)
  2) shift_summary  근무별 요약   (영구)
  3) baseline       태그별 기준선 (갱신)
  4) event          검출 이벤트   (영구)
  5) draft/draft_item  초안과 항목별 채택 여부 (영구 — 제외한 항목도 남긴다)
  6) handover       확정 일지 + 전문 색인 (영구)
"""
import json
import sqlite3
from datetime import datetime, timedelta

from config import DB_PATH, RAW_RETENTION_DAYS, SAMPLE_INTERVAL_SEC
import numfmt

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 근무 구간 메타
CREATE TABLE IF NOT EXISTS shift (
    id            TEXT PRIMARY KEY,          -- 2026-08-23-day
    kind          TEXT NOT NULL,             -- day | night
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL,
    source        TEXT,
    ingested_at   TEXT,
    quality_json  TEXT                        -- 원본 품질(깨진 행 건너뜀 등). 없으면 NULL. 초안·AI 에 전달된다
);

-- 1) 원본 시계열. 단기 회전 대상.
CREATE TABLE IF NOT EXISTS raw_sample (
    tag   TEXT NOT NULL,
    ts    TEXT NOT NULL,
    value REAL NOT NULL,
    PRIMARY KEY (tag, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_raw_ts ON raw_sample(ts);

-- 2) 근무별 요약. 원본이 사라져도 이건 남는다.
CREATE TABLE IF NOT EXISTS shift_summary (
    shift_id TEXT NOT NULL,
    tag      TEXT NOT NULL,
    n        INTEGER,
    min      REAL,
    max      REAL,
    mean     REAL,
    median   REAL,
    mad      REAL,
    std      REAL,
    first    REAL,
    last     REAL,
    slope    REAL,                           -- 시간당 변화량
    PRIMARY KEY (shift_id, tag),
    FOREIGN KEY (shift_id) REFERENCES shift(id)
);

-- 3) 태그별 기준선. 근무가 쌓일수록 갱신된다.
CREATE TABLE IF NOT EXISTS baseline (
    tag         TEXT PRIMARY KEY,
    median      REAL,
    mad         REAL,
    std         REAL,
    band_lo     REAL,
    band_hi     REAL,
    n_shifts    INTEGER,
    params_json TEXT,                        -- 엔진이 쓰는 부가 파라미터
    updated_at  TEXT
);

-- 4) 검출 이벤트
CREATE TABLE IF NOT EXISTS event (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id     TEXT NOT NULL,
    tag          TEXT NOT NULL,
    kind         TEXT NOT NULL,              -- 헌팅 / 드리프트 / 임계근접 / ...
    start_ts     TEXT,
    end_ts       TEXT,
    severity     TEXT,                       -- 상 | 중 | 하
    score        REAL,
    waveform_json    TEXT,                   -- 감지 구간 ±30분 파형 (다운샘플, 원본이 폐기돼도 남는다)
    metrics_json TEXT,                       -- 판정 근거 수치
    evidence     TEXT,                       -- 사람이 읽는 한 줄
    detector     TEXT,                       -- 누가 찾았나: stub | engine
    created_at   TEXT,
    FOREIGN KEY (shift_id) REFERENCES shift(id)
);

CREATE INDEX IF NOT EXISTS idx_event_shift ON event(shift_id);

-- 5) 초안
CREATE TABLE IF NOT EXISTS draft (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id     TEXT NOT NULL UNIQUE,
    status       TEXT NOT NULL,              -- live(실시간 누적 중 — 승인 불가) | pending | confirmed
    generator    TEXT,                       -- stub | claude
    model        TEXT,
    generated_at TEXT,
    FOREIGN KEY (shift_id) REFERENCES shift(id)
);

-- 초안 항목. 채택 여부를 항목 단위로 기록하고, 제외한 것도 지우지 않는다.
-- 제외 기록은 감도 조정과 근무자 간 기준 차이를 측정할 유일한 데이터다.
CREATE TABLE IF NOT EXISTS draft_item (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id         INTEGER NOT NULL,
    event_id         INTEGER,                -- 수동 추가면 NULL
    seq              INTEGER,
    origin           TEXT NOT NULL,          -- detected | manual
    tag              TEXT,
    title            TEXT,
    body             TEXT,
    evidence         TEXT,
    severity         TEXT,
    suggested_action TEXT,                   -- 과거 조치 추천
    severity_rule    TEXT,                   -- 통계 검출기가 매긴 중요도 (AI 판정과 대비용)
    severity_reason  TEXT,                   -- AI 가 중요도를 그렇게 판단한 이유
    handover_worthy  INTEGER,                -- AI 판단: 다음 근무에 전달할 가치 (1/0, 미판정 NULL)
    precedent_note   TEXT,                   -- AI 가 과거 사례를 채택/기각한 이유
    related_tags_ai  TEXT,                   -- AI 가 같은 사건으로 묶은 다른 항목의 태그 (JSON 배열)
    related_note     TEXT,                   -- 그렇게 묶은 이유
    precedent_json   TEXT,                   -- 근거로 삼은 과거 일지(AI 가 적합 판정한 것)
    precedents_all_json TEXT,                -- 같은 태그로 찾은 과거 일지 전부 — 기각 사실을 나중에도 보이게 (Codex)
    adopted          INTEGER,                -- NULL 미결정 / 1 채택 / 0 제외
    comment          TEXT,
    decided_at       TEXT,
    status           TEXT,                   -- 채택 항목의 처리 상태: '완료' | '진행중'. 기본값 없음 — 근무자가 골라야 승인된다 (심사평 08)
    carried_from     INTEGER,                -- 이월 항목이면 원 draft_item id. 「진행중」 은 이 사슬로 다음 근무에 이어지고, '완료' 행이 붙으면 닫힌다
    curve_json       TEXT,                   -- 근무 구간 전체 추이(1분 평균). 원본이 회전으로 지워져도 그래프가 남게 저장한다. 옛 기록은 NULL
    live_json        TEXT,                   -- 실시간 항목의 추적 기록(처음 감지·확정 시각·AI 초·마감 검출에 없음). 일괄 경로는 NULL
    FOREIGN KEY (draft_id) REFERENCES draft(id),
    FOREIGN KEY (event_id) REFERENCES event(id)
);

CREATE INDEX IF NOT EXISTS idx_item_draft ON draft_item(draft_id);

-- 6) 확정 일지
CREATE TABLE IF NOT EXISTS handover_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id         TEXT NOT NULL,
    round            INTEGER NOT NULL,       -- 몇 차 확정이었나
    confirmed_by     TEXT,
    confirmed_at     TEXT,
    adopted_count    INTEGER,
    excluded_count   INTEGER,
    body             TEXT,
    reopened_at      TEXT NOT NULL,          -- 언제 재검토로 되돌렸나
    reopened_reason  TEXT
);

CREATE TABLE IF NOT EXISTS handover (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id       TEXT NOT NULL UNIQUE,
    draft_id       INTEGER,
    confirmed_by   TEXT,
    confirmed_at   TEXT,
    adopted_count  INTEGER,
    excluded_count INTEGER,
    body           TEXT,
    FOREIGN KEY (shift_id) REFERENCES shift(id)
);

-- 확정 일지 전문 색인. 과거 사례 검색이 여기서 나온다.
CREATE VIRTUAL TABLE IF NOT EXISTS handover_fts USING fts5(
    shift_id UNINDEXED,
    tag,
    text,
    tokenize = 'unicode61'
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)   # 화면 작업과 교대 타이머가 겹칠 수 있다. 잠금을 30초 기다린다
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate(conn):
    """예전 DB 에 새 컬럼을 더한다. 서버 시드 DB 를 다시 만들지 않아도 되게.

    서버·교대 타이머·명령줄이 같은 옛 DB 에 동시에 init 하면, 칸이 없다고 본 뒤 다른 프로세스가 먼저 더해 ALTER 가
    「duplicate column name」 으로 죽었다(반증 워커 재현: 6개 동시 120회 중 81회). 그 오류만 넘기고 다른 오류는 그대로 올린다.
    """
    def add(table, col, typ):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise

    have = {r[1] for r in conn.execute("PRAGMA table_info(draft_item)")}
    for col, typ in (("severity_rule", "TEXT"), ("severity_reason", "TEXT"), ("handover_worthy", "INTEGER"), ("precedent_note", "TEXT"), ("precedents_all_json", "TEXT"), ("related_tags_ai", "TEXT"), ("related_note", "TEXT"), ("prev_excluded_json", "TEXT"), ("status", "TEXT"), ("carried_from", "INTEGER"), ("curve_json", "TEXT"), ("live_json", "TEXT")):
        if col not in have:
            add("draft_item", col, typ)
    have_ev = {r[1] for r in conn.execute("PRAGMA table_info(event)")}
    if "waveform_json" not in have_ev:
        add("event", "waveform_json", "TEXT")
    if "quality_json" not in {r[1] for r in conn.execute("PRAGMA table_info(shift)")}:
        add("shift", "quality_json", "TEXT")


def reopen_handover(conn, shift_id, reason=None):
    """확정 일지를 재검토 상태로 되돌린다. 이전 확정본은 이력으로 남긴다 — 지우지 않는다.

    기획서 4-3 "확정 일지는 변경 이력이 남는 구조" 가 이것이다. 채택·코멘트는 그대로 두어
    근무자가 고칠 것만 고치게 하고, 색인은 뺀다(재확정 전까지 과거 조치로 회수되면 안 된다).
    """
    h = conn.execute("SELECT * FROM handover WHERE shift_id = ?", (shift_id,)).fetchone()
    if h is None:
        raise ValueError(f"{shift_id} 는 확정된 일지가 아닙니다.")
    prev = conn.execute("SELECT COALESCE(MAX(round), 0) FROM handover_history WHERE shift_id = ?", (shift_id,)).fetchone()[0]
    conn.execute(
        """INSERT INTO handover_history
           (shift_id, round, confirmed_by, confirmed_at, adopted_count, excluded_count, body, reopened_at, reopened_reason)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (shift_id, prev + 1, h["confirmed_by"], h["confirmed_at"], h["adopted_count"], h["excluded_count"], h["body"], now(), reason),
    )
    conn.execute("DELETE FROM handover WHERE shift_id = ?", (shift_id,))
    conn.execute("DELETE FROM handover_fts WHERE shift_id = ?", (shift_id,))
    conn.execute("UPDATE draft SET status = 'pending' WHERE shift_id = ?", (shift_id,))
    # 공개 데모라 승인↔재검토를 무한 반복할 수 있다. 근무당 이력 20회까지만 남긴다.
    conn.execute(
        """DELETE FROM handover_history WHERE shift_id = ? AND id NOT IN
           (SELECT id FROM handover_history WHERE shift_id = ? ORDER BY round DESC LIMIT 20)""",
        (shift_id, shift_id))
    return prev + 1


def handover_rounds(conn, shift_id):
    """이전 확정 이력 (최신 먼저)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM handover_history WHERE shift_id = ? ORDER BY round DESC", (shift_id,)).fetchall()]


def reset(empty=False):
    """데모 상태를 되돌린다. QA 중 시간 맞춰 기다리지 않게 하려는 것이다.

    empty=False: SEED_DB(팀 정본 8근무 + 조치 문구)로 복원. 과거 조치 순환이 첫 화면부터 보인다.
    empty=True : 완전 빈 저장소. "처음부터 쌓기" 를 보고 싶을 때.
    시드 파일이 없으면 empty 만 가능하고, 그 사실을 올린다 — 조용히 빈 상태로 가지 않는다.
    """
    import shutil
    from config import SEED_DB
    for suffix in ("", "-wal", "-shm"):
        p = DB_PATH.parent / (DB_PATH.name + suffix)
        if p.exists():
            p.unlink()
    if empty:
        init()
        return "빈 상태"
    if not SEED_DB.exists():
        raise FileNotFoundError(f"기준선 시드가 없습니다: {SEED_DB}. `python3 tools/seed.py` 로 만들거나 빈 상태 리셋을 쓰세요.")
    shutil.copyfile(SEED_DB, DB_PATH)
    init()          # 스키마 마이그레이션까지
    return "기준선(팀 정본 8근무)"


def init():
    """스키마 생성. 여러 번 실행해도 안전하다."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    return DB_PATH


def now():
    return datetime.now().isoformat(timespec="seconds")


# --- 근무 -------------------------------------------------------------

def raw_coverage(conn, shift_id):
    """이 근무 창에 원본이 얼마나 남았나 → (표본 수, 첫 ts). 3일 회전이 창을 통째로 또는 앞부분만 지운 뒤
    다시 돌리면 "데이터 없음" 이나 반쪽 데이터로 검출하게 되므로, 실행 전에 이걸 보고 파일에서 다시 적재한다."""
    row = conn.execute("SELECT window_start, window_end FROM shift WHERE id = ?", (shift_id,)).fetchone()
    if row is None:
        return 0, None
    n, first = conn.execute("SELECT COUNT(*), MIN(ts) FROM raw_sample WHERE ts >= ? AND ts < ?",
                            (row["window_start"], row["window_end"])).fetchone()
    return n, first


RAW_BUCKET_MIN = 60    # 채움을 보는 칸 — 이 칸이 통째로 비면 창이 성긴 것으로 본다


def raw_fill(conn, start, end):
    """[start, end) 원본이 얼마나 찼나 → {samples, tags, buckets, filled, ratio, first, last, head_gap_min, tail_gap_min}.

    양끝 표본만 보면 가운데가 통째로 비어도 온전해 보이고(중간에 죽은 재생이 남긴 반쪽 원본이 그렇다),
    반대로 앞뒤 2분이 모자란 99.7% 짜리를 통째로 버린다 — 그래서 1시간 칸마다 표본이 있는지로 본다.
    표본 간격에 기대지 않는다(연결 규약은 2초지만 데이터가 성길 수도 있다). ratio 는 규약 간격 기준 참고값이다."""
    n, tags, first, last = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT tag), MIN(ts), MAX(ts) FROM raw_sample WHERE ts >= ? AND ts < ?",
        (start, end)).fetchone()
    s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    span = (e - s).total_seconds()
    buckets = max(1, int(span // (RAW_BUCKET_MIN * 60)))
    filled = 0
    for i in range(buckets):
        b0 = s + timedelta(minutes=RAW_BUCKET_MIN * i)
        b1 = min(e, b0 + timedelta(minutes=RAW_BUCKET_MIN))
        if conn.execute("SELECT 1 FROM raw_sample WHERE ts >= ? AND ts < ? LIMIT 1",
                        (b0.isoformat(timespec="seconds"), b1.isoformat(timespec="seconds"))).fetchone():
            filled += 1
    expected = int(span / SAMPLE_INTERVAL_SEC) * (tags or 0)
    head = (datetime.fromisoformat(first) - s).total_seconds() / 60 if first else None
    tail = (e - datetime.fromisoformat(last)).total_seconds() / 60 if last else None
    return {"samples": n, "tags": tags, "buckets": buckets, "filled": filled,
            "ratio": (round(n / expected, 4) if expected else 0.0),
            "first": first, "last": last, "head_gap_min": head, "tail_gap_min": tail}


def raw_window_complete(conn, start, end):
    """[start, end) 가 실제로 찼나 → (찬 여부, 채움 정보). **실시간 창 판정 전용**이다.

    앞 12시간을 검출에 쓸 수 있는지만 묻는다. 배치의 재적재 판정(raw_complete)과 규칙을 합치면
    전 태그가 한 시간 넘게 멎은 멀쩡한 근무를 「원본 없음」이라 거부한다(3라운드) — 물음이 다르다."""
    f = raw_fill(conn, start, end)
    ok = (f["filled"] == f["buckets"] and f["head_gap_min"] is not None
          and f["head_gap_min"] <= GAP_MIN_MINUTES and f["tail_gap_min"] <= GAP_MIN_MINUTES)
    return ok, f


def raw_complete(conn, shift_id):
    """저장소 회전이 창을 잘랐나 — 첫 표본이 창 시작 1분 안이면 온전한 것으로 본다.

    여기서 묻는 것은 「3일 회전이 원본을 지웠으니 파일에서 다시 적재해야 하나」 하나뿐이다. 창 가운데가
    성긴지는 묻지 않는다 — 계측이 한 시간 멎어도 원본은 멀쩡한데, 그걸 「원본 없음」이라 하면
    /pipeline/run 이 사실과 다른 문구로 거부하고 구멍은 파일에 있으니 다시 적재해도 영영 그대로다(3라운드).
    창이 실제로 찼는지는 raw_window_complete 가 실시간 창 판정에서 따로 본다.

    예외: 그 근무에 'live' 초안이 남아 있으면 False — 재생이 쌓는 원본은 마감 동기화로 초안이 pending 이 되기 전까지
    반쪽일 수 있다. 재생 중 서버가 강제 종료되면 _drop_partial 이 못 돌아 반쪽 원본과 'live' 초안이 함께 남는데,
    첫 표본은 창 시작이라 위 규칙은 그걸 온전으로 봐서 화면이 권하는 복구 경로(일괄 실행)가 반쪽 데이터로 초안을 만들었다."""
    if conn.execute("SELECT 1 FROM draft WHERE shift_id = ? AND status = 'live'", (shift_id,)).fetchone():
        return False
    n, first = raw_coverage(conn, shift_id)
    if not n:           # 쌓다 만 원본은 _drop_partial 이 비우므로 여기서 0 이 되어 「온전 아님」이다 (반증 A6)
        return False
    row = conn.execute("SELECT window_start FROM shift WHERE id = ?", (shift_id,)).fetchone()
    return datetime.fromisoformat(first) <= datetime.fromisoformat(row["window_start"]) + timedelta(minutes=1)


GAP_MIN_MINUTES = 10   # 이보다 오래 값이 없으면 '계측 결측 구간' — 초안에 태그·시간대로 들어간다 (경모님 2026-08-27)


def find_gaps(conn, shift_id, min_minutes=GAP_MIN_MINUTES):
    """근무 창 안에서 태그별로 값이 min_minutes 넘게 없는 구간. 행이 빠졌든 값이 비어 건너뛰었든 같은 결과다.
    창 시작~첫 표본, 마지막 표본~창 끝도 본다. → [{tag, start, end, minutes}] (긴 것부터)
    태그마다 PK(tag, ts) 접두 범위로 읽는다 — 정렬(TEMP B-TREE)·fetchall 없이 창 안 행만 흐른다 (Codex).
    (ORDER BY tag, ts 한 방 쿼리는 플래너가 ts 인덱스를 골라 임시 정렬을 했다 — EXPLAIN 실측.)"""
    row = conn.execute("SELECT window_start, window_end FROM shift WHERE id = ?", (shift_id,)).fetchone()
    if row is None:
        return []
    from datetime import datetime
    ws, we = datetime.fromisoformat(row["window_start"]), datetime.fromisoformat(row["window_end"])
    limit = min_minutes * 60
    gaps = []
    tags = [r[0] for r in conn.execute("SELECT DISTINCT tag FROM raw_sample WHERE ts >= ? AND ts < ?", (row["window_start"], row["window_end"]))]
    # 지난 근무들에 있었는데 이 근무엔 한 점도 없는 태그 — 계측이 통째로 빠진 것. 갭 탐색은 있는 태그만 돌아 조용히 지나갔다 (홀리스틱 Codex).
    # 기대 태그 = 직전 근무 3개 중 2개 이상에서 관측된 태그. 전 이력 합집합을 쓰면 정상적으로 퇴역한 태그가 영원히 결측으로 뜬다 (Codex).
    recent = [r[0] for r in conn.execute("SELECT DISTINCT shift_id FROM shift_summary WHERE shift_id < ? ORDER BY shift_id DESC LIMIT 3", (shift_id,))]
    expected = set()
    if recent:
        need = 2 if len(recent) >= 2 else 1
        for tag, n in conn.execute(f"SELECT tag, COUNT(DISTINCT shift_id) FROM shift_summary WHERE shift_id IN ({','.join('?' * len(recent))}) GROUP BY tag", recent):
            if n >= need:
                expected.add(tag)
    for tag in sorted(expected - set(tags)):
        gaps.append({"tag": tag, "start": row["window_start"], "end": row["window_end"], "minutes": round((we - ws).total_seconds() / 60), "whole": True})
    for tag in tags:
        prev = None
        for (ts,) in conn.execute("SELECT ts FROM raw_sample WHERE tag = ? AND ts >= ? AND ts < ? ORDER BY ts", (tag, row["window_start"], row["window_end"])):
            t = datetime.fromisoformat(ts)
            if prev is None:
                if (t - ws).total_seconds() > limit:
                    gaps.append({"tag": tag, "start": row["window_start"], "end": ts, "minutes": round((t - ws).total_seconds() / 60)})
            elif (t - prev).total_seconds() > limit:
                gaps.append({"tag": tag, "start": prev.isoformat(timespec="seconds"), "end": ts, "minutes": round((t - prev).total_seconds() / 60)})
            prev = t
        if prev is not None and (we - prev).total_seconds() > limit:
            gaps.append({"tag": tag, "start": prev.isoformat(timespec="seconds"), "end": row["window_end"], "minutes": round((we - prev).total_seconds() / 60)})
    gaps.sort(key=lambda g: -g["minutes"])
    return gaps


def quality_bad_pairs(conn):
    """품질 기록이 있는 (근무, 태그) — 깨진 행이 있었거나 결측 구간이 있는 태그. 기준선 입력에서 뺀다 (홀리스틱 Codex)."""
    pairs = set()
    for r in conn.execute("SELECT id, quality_json FROM shift WHERE quality_json IS NOT NULL"):
        q = json.loads(r["quality_json"])
        for t in (q.get("by_tag") or {}):
            pairs.add((r["id"], t))
        for g in (q.get("gaps") or []):
            pairs.add((r["id"], g["tag"]))
    return pairs


def set_quality(conn, shift_id, quality):
    """근무 원본의 품질 기록(dict 또는 None)."""
    conn.execute("UPDATE shift SET quality_json = ? WHERE id = ?", (json.dumps(quality, ensure_ascii=False) if quality else None, shift_id))


def load_quality(conn, shift_id):
    row = conn.execute("SELECT quality_json FROM shift WHERE id = ?", (shift_id,)).fetchone()
    return json.loads(row["quality_json"]) if row and row["quality_json"] else None


def forget_raw(conn, shift_id):
    """이 근무 창의 원본을 지운다 — 같은 근무 ID 로 새 CSV 가 올라오면 옛 원본 위에 겹쳐 쌓이지 않게 (Codex 반증 2026-08-27)."""
    row = conn.execute("SELECT window_start, window_end FROM shift WHERE id = ?", (shift_id,)).fetchone()
    if row is None:
        return 0
    return conn.execute("DELETE FROM raw_sample WHERE ts >= ? AND ts < ?", (row["window_start"], row["window_end"])).rowcount


def upsert_shift(conn, shift_id, kind, window_start, window_end, source):
    conn.execute(
        """INSERT INTO shift (id, kind, window_start, window_end, source, ingested_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             window_start=excluded.window_start,
             window_end=excluded.window_end,
             source=excluded.source,
             ingested_at=excluded.ingested_at""",
        (shift_id, kind, window_start, window_end, source, now()),
    )


def list_shifts(conn):
    return conn.execute(
        """SELECT s.*,
                  (SELECT COUNT(*) FROM event e WHERE e.shift_id = s.id)   AS event_count,
                  (SELECT status FROM draft d WHERE d.shift_id = s.id)     AS draft_status,
                  (SELECT adopted_count FROM handover h WHERE h.shift_id = s.id) AS adopted_count
           FROM shift s ORDER BY s.window_start DESC"""
    ).fetchall()


# --- 원본 회전 --------------------------------------------------------

def rotate_raw(conn, keep_days=RAW_RETENTION_DAYS):
    """보관 기간이 지난 원본을 지운다. 요약·이벤트·일지는 건드리지 않는다."""
    # 오늘이 아니라 적재된 데이터의 마지막 시각 기준 — 생성기 파일은 날짜가 고정돼 오늘 기준이면 첫 실행 직후
    # 전부 지워졌다(경모님 QA 2026-08-27). 데이터가 실시간이면 둘은 같다.
    newest = conn.execute("SELECT MAX(ts) FROM raw_sample").fetchone()[0]
    if not newest:
        return 0
    cutoff = (datetime.fromisoformat(newest) - timedelta(days=keep_days)).isoformat(timespec="seconds")
    cur = conn.execute("DELETE FROM raw_sample WHERE ts < ?", (cutoff,))
    return cur.rowcount


def load_series(conn, shift_id):
    """한 근무 구간의 시계열을 {태그: [(ts, value), ...]} 로 돌려준다."""
    row = conn.execute(
        "SELECT window_start, window_end FROM shift WHERE id = ?", (shift_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"근무 구간이 없습니다: {shift_id}")

    series = {}
    for tag, ts, value in conn.execute(
        "SELECT tag, ts, value FROM raw_sample WHERE ts >= ? AND ts < ? ORDER BY tag, ts",
        (row["window_start"], row["window_end"]),
    ):
        series.setdefault(tag, []).append((ts, value))
    return series


# --- 요약 / 기준선 ----------------------------------------------------

def save_summaries(conn, shift_id, summaries):
    conn.executemany(
        """INSERT OR REPLACE INTO shift_summary
           (shift_id, tag, n, min, max, mean, median, mad, std, first, last, slope)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                shift_id, tag, s.get("n"), s.get("min"), s.get("max"), s.get("mean"),
                s.get("median"), s.get("mad"), s.get("std"), s.get("first"),
                s.get("last"), s.get("slope"),
            )
            for tag, s in summaries.items()
        ],
    )


def load_summaries(conn, exclude_shift=None):
    """기준선 계산용. 특정 근무를 빼고 볼 수 있다(자기 자신 오염 방지)."""
    sql = "SELECT * FROM shift_summary"
    args = ()
    if exclude_shift:
        sql += " WHERE shift_id != ?"
        args = (exclude_shift,)
    out = {}
    for r in conn.execute(sql, args):
        out.setdefault(r["tag"], []).append(dict(r))
    return out


def save_baselines(conn, baselines):
    conn.executemany(
        """INSERT OR REPLACE INTO baseline
           (tag, median, mad, std, band_lo, band_hi, n_shifts, params_json, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            (
                tag, b.get("median"), b.get("mad"), b.get("std"), b.get("band_lo"),
                b.get("band_hi"), b.get("n_shifts"),
                json.dumps(b.get("params", {}), ensure_ascii=False), now(),
            )
            for tag, b in baselines.items()
        ],
    )


def load_baselines(conn):
    out = {}
    for r in conn.execute("SELECT * FROM baseline"):
        b = dict(r)
        b["params"] = json.loads(b.pop("params_json") or "{}")
        out[b.pop("tag")] = b
    return out


# --- 이벤트 -----------------------------------------------------------

def carried_later(conn, after_shift, root_ids):
    """root_ids 중 after_shift **뒤**의 확정된 근무가 이월로 이어받아 판단한 것 {원 항목 id: 그 근무 id(가장 이른 것)}.

    앞 근무에서 그 항목을 닫거나(순서를 거슬러 승인) 원 항목의 진행중을 풀거나(원 근무 재승인)
    원 항목을 지우면(초안 재생성) 뒤 근무의 판단이 근거를 잃고 화면·본문·색인이 서로 어긋난다(codex 반증).
    그런 쓰기는 이것으로 확인하고 거부한다 — 뒤 근무를 재검토로 되돌린 뒤 하면 된다.
    """
    ids = sorted(set(root_ids))
    row = conn.execute("SELECT window_start FROM shift WHERE id = ?", (after_shift,)).fetchone()
    if not ids or row is None or row["window_start"] is None:
        return {}
    marks = ",".join("?" * len(ids))
    out = {}
    for r in conn.execute(
        f"""SELECT c.carried_from, cd.shift_id
              FROM draft_item c
              JOIN draft cd    ON cd.id = c.draft_id
              JOIN shift cs    ON cs.id = cd.shift_id
              JOIN handover ch ON ch.shift_id = cd.shift_id
             WHERE c.carried_from IN ({marks}) AND cs.window_start > ?
             ORDER BY cs.window_start""",
        (*ids, row["window_start"]),
    ):
        out.setdefault(r["carried_from"], r["shift_id"])
    return out


def _refuse_if_carried_later(conn, shift_id):
    """이 근무의 항목을 뒤 근무가 이어받아 확정했으면 지우지 않는다(carried_later)."""
    own = [r[0] for r in conn.execute(
        "SELECT di.id FROM draft_item di JOIN draft d ON d.id = di.draft_id "
        "WHERE d.shift_id = ? AND di.origin != 'carried'", (shift_id,))]
    later = carried_later(conn, shift_id, own)
    if later:
        rid, sid = next(iter(later.items()))
        raise ValueError(f"뒤 근무 {sid} 가 이 근무의 항목 #{rid} 를 이월로 이어받았습니다 — "
                         f"그 근무를 재검토로 되돌린 뒤 다시 만드세요. 지우면 그 판단이 가리킬 항목이 사라집니다.")


def clear_outputs(conn, shift_id, include_handover=False):
    """한 근무의 산출물을 지운다. 참조 순서를 지켜야 외래키가 깨지지 않는다.

    이벤트가 바뀌면 그 이벤트로 만든 초안도 더는 유효하지 않다. 그래서
    이벤트를 다시 쓸 때 초안을 함께 비운다.
    """
    _refuse_if_carried_later(conn, shift_id)
    if include_handover:
        conn.execute("DELETE FROM handover_fts WHERE shift_id = ?", (shift_id,))
        conn.execute("DELETE FROM handover WHERE shift_id = ?", (shift_id,))
    conn.execute(
        "DELETE FROM draft_item WHERE draft_id IN (SELECT id FROM draft WHERE shift_id = ?)",
        (shift_id,),
    )
    conn.execute("DELETE FROM draft WHERE shift_id = ?", (shift_id,))
    conn.execute("DELETE FROM event WHERE shift_id = ?", (shift_id,))


_EVENT_INSERT = """INSERT INTO event
           (shift_id, tag, kind, start_ts, end_ts, severity, score,
            metrics_json, evidence, detector, created_at, waveform_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""


def _event_row(shift_id, e, detector):
    return (
        shift_id, e["tag"], e["kind"], e.get("start_ts"), e.get("end_ts"),
        e.get("severity"), e.get("score"),
        json.dumps(e.get("metrics", {}), ensure_ascii=False),
        e.get("evidence"), detector, now(),
        json.dumps(e.get("waveform"), ensure_ascii=False) if e.get("waveform") else None,
    )


def save_events(conn, shift_id, events, detector):
    clear_outputs(conn, shift_id)
    conn.executemany(_EVENT_INSERT, [_event_row(shift_id, e, detector) for e in events])


def load_events(conn, shift_id):
    rows = conn.execute(
        "SELECT * FROM event WHERE shift_id = ? ORDER BY start_ts, tag", (shift_id,)
    ).fetchall()
    out = []
    for r in rows:
        e = dict(r)
        e["metrics"] = json.loads(e.pop("metrics_json") or "{}")
        out.append(e)
    return out


# --- 초안 -------------------------------------------------------------

def save_draft(conn, shift_id, items, generator, model=None):
    # 이벤트는 건드리지 않는다. 이미 저장된 이벤트를 항목이 가리키고 있다.
    _refuse_if_carried_later(conn, shift_id)
    conn.execute("DELETE FROM draft_item WHERE draft_id IN "
                 "(SELECT id FROM draft WHERE shift_id = ?)", (shift_id,))
    conn.execute("DELETE FROM draft WHERE shift_id = ?", (shift_id,))
    cur = conn.execute(
        """INSERT INTO draft (shift_id, status, generator, model, generated_at)
           VALUES (?, 'pending', ?, ?, ?)""",
        (shift_id, generator, model, now()),
    )
    draft_id = cur.lastrowid
    conn.executemany(_ITEM_INSERT, [_item_row(draft_id, i, it) for i, it in enumerate(items, start=1)])
    return draft_id


_ITEM_INSERT = """INSERT INTO draft_item
           (draft_id, event_id, seq, origin, tag, title, body, evidence,
            severity, suggested_action, severity_rule, severity_reason, handover_worthy, precedent_note, related_tags_ai, related_note, precedent_json, precedents_all_json, prev_excluded_json, live_json, adopted)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)"""


def _item_row(draft_id, seq, it):
    return (
        draft_id, it.get("event_id"), seq, it.get("origin", "detected"),
        it.get("tag"), it.get("title"), it.get("body"), it.get("evidence"),
        it.get("severity"), it.get("suggested_action"),
        it.get("severity_rule"), it.get("severity_reason"),
        (None if it.get("handover_worthy") is None else int(bool(it["handover_worthy"]))),
        it.get("precedent_note"),
        json.dumps(it.get("related_tags_ai") or [], ensure_ascii=False), it.get("related_note"),
        json.dumps(it.get("precedents", []), ensure_ascii=False),
        json.dumps(it.get("precedents_all", []), ensure_ascii=False),   # 기각한 사례도 남긴다 — 라벨이 "없음" 과 "맞지 않음" 을 구분 (Codex)
        (json.dumps(it["prev_excluded"], ensure_ascii=False) if it.get("prev_excluded") else None),
        (json.dumps(it["live"], ensure_ascii=False) if it.get("live") else None),
    )


def load_draft(conn, shift_id):
    d = conn.execute("SELECT * FROM draft WHERE shift_id = ?", (shift_id,)).fetchone()
    if d is None:
        return None
    draft = dict(d)
    draft["items"] = []
    for r in conn.execute(
        "SELECT * FROM draft_item WHERE draft_id = ? ORDER BY seq", (draft["id"],)
    ):
        it = dict(r)
        it["precedents"] = json.loads(it.pop("precedent_json") or "[]")
        it["precedents_all"] = json.loads(it.pop("precedents_all_json", None) or "[]")
        try:
            it["prev_excluded"] = json.loads(it.pop("prev_excluded_json", None) or "null")
        except (TypeError, ValueError):
            it["prev_excluded"] = None
        try:
            it["related_tags_ai"] = json.loads(it.get("related_tags_ai") or "[]")
        except (TypeError, ValueError):
            it["related_tags_ai"] = []
        try:
            it["curve"] = json.loads(it.pop("curve_json", None) or "null")
        except (TypeError, ValueError):
            it["curve"] = None
        try:
            it["live"] = json.loads(it.pop("live_json", None) or "null")
        except (TypeError, ValueError):
            it["live"] = None
        draft["items"].append(it)
    return draft


def raw_curve(conn, shift_id, tag):
    """근무 구간 전체의 태그 곡선 — 원본 표본을 1분 평균으로 줄인다(12시간이면 720점). 원본이 2점도 없거나 값 폭이 무한대면 None.

    {"t0": 구간 시작, "t1": 구간 끝, "m": [구간 시작부터 몇 분], "v": [1분 평균]}.
    값은 곡선 모양이 남을 만큼만 자리수를 줄인다 — 화면 자리수로 자르면 99.6±0.3 같은 좁은 폭의 곡선이 계단이 된다.
    """
    import math
    from datetime import datetime
    row = conn.execute("SELECT window_start, window_end FROM shift WHERE id = ?", (shift_id,)).fetchone()
    if row is None:
        return None
    rows = conn.execute(
        "SELECT substr(ts, 1, 16) AS m, AVG(value) FROM raw_sample "
        "WHERE tag = ? AND ts >= ? AND ts < ? GROUP BY m ORDER BY m",
        (tag, row["window_start"], row["window_end"])).fetchall()
    pts = [(m, float(v)) for m, v in rows if v is not None]
    if len(pts) < 2:
        return None
    vals = [v for _, v in pts]
    span = max(vals) - min(vals)
    if not math.isfinite(span):
        # ±1e308 처럼 폭이 부동소수 범위를 넘으면 자리수를 못 정한다 — 아래 계산이 OverflowError 를 내 승인 전체가 되돌아갔다(반증 워커 재현).
        # 그릴 수 없는 곡선은 원본이 없을 때처럼 없는 것으로 둔다.
        return None
    d = numfmt.decimals(sorted(abs(v) for v in vals)[len(vals) // 2])
    if span / 200 > 0:      # 폭이 비정규 소수만큼 작으면 200 으로 나눈 값이 0 이 되어 log10 이 ValueError 를 냈다(탐침 재현) — 그때는 값 크기 자리수만 쓴다
        d = max(d, min(12, math.ceil(-math.log10(span / 200))))   # 폭을 200단계 이상으로 — 그래프 높이보다 촘촘하면 충분하다
    t0 = datetime.fromisoformat(row["window_start"])
    return {"t0": row["window_start"], "t1": row["window_end"],
            "m": [int((datetime.fromisoformat(m + ":00") - t0).total_seconds() // 60) for m, _ in pts],
            "v": [round(v, d) for v in vals]}


def item_starts(conn, shift_id):
    """이 근무의 이벤트 id → 처음 감지된 시각. 화면과 확정 일지 본문이 같은 순서를 쓰게 한다."""
    return {e["id"]: e["start_ts"] for e in load_events(conn, shift_id)}


def by_time(items, starts):
    """처음 감지된 시각 순으로 세운다. 엔진은 항목을 중요도 순으로 정렬해 넘기는데(engine/api.py compose),
    중요도를 화면에서 뺐으니(경모님 2026-09-14) 그 순서는 근거 없이 뒤섞여 보인다. 원본 품질 항목은 맨 앞,
    직접 추가는 맨 뒤, 시각을 모르는 것(이벤트 없는 항목)은 감지 항목 뒤, 같은 시각이면 원래 순서. DB 의 seq 는 그대로 둔다."""
    def key(pair):
        seq, it = pair
        group = {"quality": 0, "manual": 2}.get(it.get("origin"), 1)
        return (group, starts.get(it.get("event_id")) or "9999", seq)
    return [it for _, it in sorted(enumerate(items), key=key)]


def save_curves(conn, shift_id):
    """그 근무 초안 항목 가운데 곡선이 빈 것에 원본 1분 평균 곡선을 저장한다. (채운 항목 수, 원본이 없어 못 채운 항목 수).

    원본은 보관 기간 뒤 회전으로 지워지는데 확정 일지는 오래 남는 기록이라, 그래프도 함께 남아야 한다.
    초안을 만들 때(pipeline.run)와 확정할 때(approve.decide) 부르고, 그 전에 확정된 기록은 tools/backfill_curves.py 로 채운다.
    이월 판단 행은 원 근무의 항목이라 이 근무 구간의 곡선을 붙이지 않는다.
    """
    rows = conn.execute(
        "SELECT i.id, i.tag FROM draft_item i JOIN draft d ON d.id = i.draft_id "
        "WHERE d.shift_id = ? AND i.tag IS NOT NULL AND i.origin != 'carried' AND i.curve_json IS NULL",
        (shift_id,)).fetchall()
    curves, filled, missing = {}, 0, 0
    for iid, tag in rows:
        if tag not in curves:
            curves[tag] = raw_curve(conn, shift_id, tag)
        if curves[tag] is None:
            missing += 1
            continue
        conn.execute("UPDATE draft_item SET curve_json = ? WHERE id = ?",
                     (json.dumps(curves[tag], separators=(",", ":")), iid))
        filled += 1
    return filled, missing


# --- 실시간 누적 초안 (app/live.py) ------------------------------------
# 초안 표·승인·이월은 일괄 경로와 같은 것을 쓴다. 다른 점은 둘 — 초안이 status 'live' 로 열려 확정·AI 완료된
# 문제가 하나씩 들어오고, 마감 동기화와 AI 서술이 다 끝나야 'pending'(승인 대기)이 된다. 'live' 는 DB 에
# 있으므로 서버를 다시 켜도 부분 초안이 승인되지 않는다(approve.decide 가 거부).

def live_refusal(conn, shift_id, replace_unconfirmed=False):
    """이 근무에 실시간 재생을 쌓을 수 없으면 그 이유(없으면 None). 재생이 근무자가 보던 초안이나 확정 기록을 조용히 덮지 않게 한다.
    'live' 초안(멈춘·중단된 재생)은 다시 쌓는다. 승인 대기 초안은 replace_unconfirmed(데모 재촬영)일 때만 지우고,
    확정 초안은 어떤 경우에도 거부한다."""
    d = conn.execute("SELECT status FROM draft WHERE shift_id = ?", (shift_id,)).fetchone()
    if d is None or d["status"] == "live":
        return None
    if d["status"] == "confirmed":
        return f"{shift_id} 에는 이미 확정된 일지가 있습니다 — 실시간 재생은 확정 기록을 지우지 않습니다."
    if replace_unconfirmed:
        return None
    return f"{shift_id} 에는 이미 승인 대기 초안이 있습니다 — 다시 쌓으려면 확정 안 된 초안 교체를 켜세요."


def open_live_draft(conn, shift_id, generator, model=None, replace_unconfirmed=False):
    """실시간 초안을 연다 → draft id. 지울 수 있는 기존 초안(live · 교체 허락된 pending)은 산출물째 지우고 새로 연다."""
    why = live_refusal(conn, shift_id, replace_unconfirmed)
    if why:
        raise ValueError(why)
    clear_outputs(conn, shift_id)
    return conn.execute(
        "INSERT INTO draft (shift_id, status, generator, model, generated_at) VALUES (?, 'live', ?, ?, ?)",
        (shift_id, generator, model, now())).lastrowid


def _live_draft(conn, draft_id):
    """이 재생이 연 초안이 아직 'live' 인지. id 로 본다 — 멈춘 재생의 늦은 AI 결과가 같은 근무의 새 재생 초안에 섞이지 않게."""
    d = conn.execute("SELECT shift_id, status FROM draft WHERE id = ?", (draft_id,)).fetchone()
    if d is None or d["status"] != "live":
        raise ValueError(f"실시간 초안 #{draft_id} 가 없거나 'live' 가 아닙니다 — 그 사이 다시 재생·일괄 실행·리셋으로 바뀌었습니다.")
    return d["shift_id"]


def add_events(conn, shift_id, events, detector):
    """이벤트를 지우지 않고 더한다 → 넣은 id(순서 그대로). 일괄 경로의 save_events 는 근무 산출물을 비우고 쓴다."""
    return [conn.execute(_EVENT_INSERT, _event_row(shift_id, e, detector)).lastrowid for e in events]


def add_live_item(conn, draft_id, item, events, detector):
    """확정·AI 완료된 문제 하나를 실시간 초안 끝에 넣는다 → draft_item id.
    events = 그 문제의 멤버 이벤트(대표가 먼저). 확정 때 근거 그대로 남기고, 항목은 대표 이벤트를 가리킨다."""
    shift_id = _live_draft(conn, draft_id)
    ids = add_events(conn, shift_id, events, detector)
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM draft_item WHERE draft_id = ?", (draft_id,)).fetchone()[0]
    return conn.execute(_ITEM_INSERT, _item_row(draft_id, seq, dict(item, event_id=ids[0] if ids else None))).lastrowid


def update_live_item(conn, item_id, evidence, live):
    """이미 들어간 실시간 항목의 근거 줄·추적 기록만 고친다 — 재발이 원 항목에 접힐 때.
    AI 가 쓴 제목·본문·중요도는 건드리지 않는다(확정 때 그대로)."""
    conn.execute("UPDATE draft_item SET evidence = ?, live_json = ? WHERE id = ?",
                 (evidence, json.dumps(live, ensure_ascii=False), item_id))


def drop_unreferenced_events(conn, shift_id, keep_ids):
    """마감 집합(keep_ids)에도 없고 초안 항목이 가리키지도 않는 이벤트를 지운다 → 지운 수.
    확정 때 근거로 넣었다가 마감 집합으로 대체된 행을 치워, 채점·화면이 일괄 실행과 같은 이벤트를 본다."""
    # 빈 집합은 절을 아예 뺀다. `NOT IN (SELECT NULL)` 은 SQL 에서 참이 아니라 UNKNOWN 이라 한 행도 안 지운다 —
    # 마감 틱에 검출이 0건인 조용한 근무가 통째로 정리를 건너뛰었다(3라운드).
    where = f"AND id NOT IN ({','.join('?' * len(keep_ids))}) " if keep_ids else ""
    return conn.execute(
        f"DELETE FROM event WHERE shift_id = ? {where}"
        f"AND id NOT IN (SELECT event_id FROM draft_item WHERE event_id IS NOT NULL)",
        (shift_id, *keep_ids)).rowcount


def finish_live_draft(conn, draft_id, order, finals):
    """마감 동기화와 AI 서술이 끝난 실시간 초안을 승인 대기로 넘긴다.
    order = 항목 id 를 보일 순서대로(초안의 항목 전부),
    finals = {항목 id: {"live", "evidence"}} — 재발 갱신이 경합으로 되돌아갔을 수 있어 최종본으로 덮는다. 중요도는 건드리지 않는다."""
    _live_draft(conn, draft_id)
    have = {r[0] for r in conn.execute("SELECT id FROM draft_item WHERE draft_id = ?", (draft_id,))}
    if set(order) != have or len(order) != len(have):
        raise ValueError(f"실시간 초안 #{draft_id} 의 항목 순서가 초안과 다릅니다 — 순서 {len(order)}개 / 초안 {len(have)}개")
    conn.executemany("UPDATE draft_item SET seq = ? WHERE id = ?", [(i, iid) for i, iid in enumerate(order, start=1)])
    conn.executemany("UPDATE draft_item SET live_json = ?, evidence = ? WHERE id = ?",
                     [(json.dumps(f["live"], ensure_ascii=False), f["evidence"], iid) for iid, f in finals.items()])
    conn.execute("UPDATE draft SET status = 'pending', generated_at = ? WHERE id = ?", (now(), draft_id))


def live_drafts(conn):
    """아직 'live' 인 초안의 근무 id — 재시작·정지로 재생이 끊긴 구간을 화면이 드러내게."""
    return [r[0] for r in conn.execute("SELECT shift_id FROM draft WHERE status = 'live' ORDER BY shift_id")]


def unconfirmed_before(conn, shift_id):
    """이 근무보다 구간 시작이 이른 근무 중 초안이 확정되지 않은 가장 이른 것 → (근무 id, 초안 상태 pending|live). 없으면 None."""
    row = conn.execute(
        """SELECT d.shift_id, d.status FROM draft d JOIN shift s ON s.id = d.shift_id
            WHERE d.status != 'confirmed'
              AND s.window_start < (SELECT window_start FROM shift WHERE id = ?)
            ORDER BY s.window_start LIMIT 1""", (shift_id,)).fetchone()
    return (row[0], row[1]) if row else None



def recent_exclusions(conn, before_shift):
    """확정된 지난 근무에서 근무자가 **제외한** 항목을 (태그, 종류) 별로 모은다.

    쓰는 곳은 화면과 초안 조립뿐이다. 검출 엔진도 AI 도 이 값을 보지 않는다 —
    보면 "전에 제외했으니 이번에도 아니다" 로 판정을 접는 경로가 생긴다
    (침묵 사고, QA 6차 2026-08-29 실측과 같은 계열).

    억제가 아니라 **자리 옮김**이다. 항목은 지워지지 않고 초안 최하단
    「이전에 제외한 것」 칸으로 내려갈 뿐이다 (#30, 경모님 결정 2026-08-30 —
    승격 조건·만료 시간은 넣지 않는다).
    """
    row = conn.execute(
        "SELECT window_start FROM shift WHERE id = ?", (before_shift,)
    ).fetchone()
    if row is None or row["window_start"] is None:
        return {}
    out = {}
    for r in conn.execute(
        """SELECT e.tag AS tag, e.kind AS kind, d.shift_id AS shift_id,
                  di.comment AS comment, h.confirmed_by AS by_who,
                  h.confirmed_at AS at_when
             FROM draft_item di
             JOIN draft d    ON d.id = di.draft_id
             JOIN event e    ON e.id = di.event_id
             JOIN shift s    ON s.id = d.shift_id
             JOIN handover h ON h.shift_id = d.shift_id
            WHERE di.adopted = 0
              AND s.window_start < ?
            ORDER BY s.window_start""",
        (row["window_start"],),
    ):
        key = (r["tag"], r["kind"])
        prev = out.get(key)
        out[key] = {
            "shift_id": r["shift_id"],
            "by": r["by_who"],
            "at": r["at_when"],
            "comment": r["comment"],
            # 몇 근무 연속으로 제외됐는지. 숨기지는 않지만 반복은 보인다 (#21 과 같은 뿌리)
            "times": (prev["times"] + 1) if prev else 1,
        }
    return out


ITEM_STATUSES = ("완료", "진행중")


def open_items(conn, before_shift):
    """이전 근무들의 확정 일지에서 아직 닫히지 않은 「진행중」 항목 (원 근무 시각순).

    열림의 정의: 채택돼 '진행중' 으로 확정된 원 항목(carried_from IS NULL)이고, 그 뒤·이 근무 앞의
    **확정된** 근무 어디에도 이 항목을 가리키는(carried_from = 원 id) '완료' 행이 없다.
    확정되지 않은 초안의 완료 표시는 닫힘이 아니다 — 아직 아무도 승인하지 않았다.
    이 근무 이후 근무의 판단은 세지 않는다(뒤 근무가 앞 근무 초안에 새면 안 된다).

    돌려주는 것: [{id(원 draft_item), tag, title, body, comment(마지막), shift_id(원 근무),
                  shifts_ago(몇 근무 전), last_shift_id(마지막으로 다룬 근무), confirmed_by}]
    초안 조립(pipeline)과 화면이 같은 함수를 쓴다.
    """
    row = conn.execute("SELECT window_start FROM shift WHERE id = ?", (before_shift,)).fetchone()
    if row is None or row["window_start"] is None:
        return []
    before = row["window_start"]
    out = []
    for r in conn.execute(
        """SELECT di.id, di.tag, di.title, di.body, di.comment, d.shift_id, s.window_start, h.confirmed_by
             FROM draft_item di
             JOIN draft d    ON d.id = di.draft_id
             JOIN shift s    ON s.id = d.shift_id
             JOIN handover h ON h.shift_id = d.shift_id
            WHERE di.adopted = 1 AND di.status = '진행중' AND di.carried_from IS NULL
              AND s.window_start < ?
              AND NOT EXISTS (
                    SELECT 1 FROM draft_item c
                      JOIN draft cd    ON cd.id = c.draft_id
                      JOIN shift cs    ON cs.id = cd.shift_id
                      JOIN handover ch ON ch.shift_id = cd.shift_id
                     WHERE c.carried_from = di.id AND c.adopted = 1 AND c.status = '완료'
                       AND cs.window_start < ?)
            ORDER BY s.window_start, di.seq""",
        (before, before),
    ):
        it = dict(r)
        # 마지막으로 다룬 확정 근무의 코멘트가 최신이다. 없으면 원 항목의 코멘트.
        last = conn.execute(
            """SELECT c.comment, cd.shift_id
                 FROM draft_item c
                 JOIN draft cd    ON cd.id = c.draft_id
                 JOIN shift cs    ON cs.id = cd.shift_id
                 JOIN handover ch ON ch.shift_id = cd.shift_id
                WHERE c.carried_from = ? AND c.adopted = 1 AND cs.window_start < ?
                ORDER BY cs.window_start DESC LIMIT 1""",
            (it["id"], before),
        ).fetchone()
        it["last_shift_id"] = last["shift_id"] if last else it["shift_id"]
        if last and last["comment"]:
            it["comment"] = last["comment"]
        it["shifts_ago"] = conn.execute(
            "SELECT COUNT(*) FROM shift WHERE window_start > ? AND window_start <= ?",
            (it.pop("window_start"), before),
        ).fetchone()[0]
        out.append(it)
    return out


def carried_roots_in_review(conn, draft_id):
    """이 초안의 이월 판단이 가리키는 원 항목 중 원 근무가 지금 확정이 아닌 것 {원 항목 id: 원 근무 id}.

    원 근무를 재검토로 되돌린 동안 그 원 항목은 open_items 에서 빠진다. 그때 이 근무를 재승인하면 save_carried 가
    기존 판단(예: 「완료로 닫음」)을 조용히 지운다(반증 워커 실측) — approve.decide 가 이것으로 확인하고 거부한다.
    원 항목이 지워져 없으면(초안 재생성) 넣지 않는다 — 가리킬 곳이 없는 판단은 지워지는 것이 맞다.
    """
    return {r["carried_from"]: r["shift_id"] for r in conn.execute(
        """SELECT c.carried_from, rd.shift_id
             FROM draft_item c
             JOIN draft_item r    ON r.id = c.carried_from
             JOIN draft rd        ON rd.id = r.draft_id
             LEFT JOIN handover h ON h.shift_id = rd.shift_id
            WHERE c.draft_id = ? AND c.origin = 'carried' AND h.id IS NULL""",
        (draft_id,))}


def carried_choices(conn, draft_id):
    """이 초안에 기록된 이월 판단 {원 항목 id: {status, comment}}. 재검토 뒤 화면이 앞 선택을 되살릴 때 쓴다."""
    return {
        r["carried_from"]: {"status": r["status"], "comment": r["comment"]}
        for r in conn.execute(
            "SELECT carried_from, status, comment FROM draft_item WHERE draft_id = ? AND origin = 'carried'",
            (draft_id,))
    }


def save_carried(conn, draft_id, choices, open_by_id):
    """이번 근무의 이월 판단을 항목 행으로 기록한다 (origin='carried', carried_from=원 id).

    지난 판단 행은 지우고 다시 쓴다 — 재검토 후 재승인이 같은 원 항목을 두 번 가리키면
    안 된다. 원 항목의 태그·제목·본문을 복사해 두어 일지 본문과 색인에 그대로 실린다.
    """
    conn.execute("DELETE FROM draft_item WHERE draft_id = ? AND origin = 'carried'", (draft_id,))
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM draft_item WHERE draft_id = ?", (draft_id,)).fetchone()[0]
    for i, (root_id, ch) in enumerate(sorted(choices.items()), start=1):
        src = open_by_id[root_id]
        conn.execute(
            """INSERT INTO draft_item
               (draft_id, event_id, seq, origin, tag, title, body, adopted, comment, decided_at, status, carried_from)
               VALUES (?, NULL, ?, 'carried', ?, ?, ?, 1, ?, ?, ?, ?)""",
            (draft_id, seq + i, src["tag"], src["title"], src["body"], ch.get("comment"), now(), ch["status"], root_id),
        )


# --- 확정 일지와 색인 -------------------------------------------------

def index_handover(conn, shift_id, items):
    """확정된 항목만 색인한다. 제외된 항목은 검색에 나오면 안 된다."""
    conn.execute("DELETE FROM handover_fts WHERE shift_id = ?", (shift_id,))
    conn.executemany(
        "INSERT INTO handover_fts (shift_id, tag, text) VALUES (?,?,?)",
        [
            (
                shift_id,
                it.get("tag") or "",
                " ".join(filter(None, [it.get("title"), it.get("body"), it.get("comment")])),
            )
            for it in items if (it.get("origin") or "detected") != "quality"
        ],
    )


def search_precedents(conn, tag, query, limit=3):
    """과거 확정 일지에서 비슷한 사례를 찾는다. 같은 태그를 우선한다.

    임베딩 없이 전문 검색으로 간다. 태그 53점·일지 수백 건 규모에서는
    벡터 DB를 도입할 이유가 없다 — 태그명이 이미 강한 키다.
    """
    hits = []
    if tag:
        hits = conn.execute(
            """SELECT f.shift_id, f.tag, f.text, h.confirmed_at
               FROM handover_fts f JOIN handover h ON h.shift_id = f.shift_id
               WHERE f.tag = ? ORDER BY h.confirmed_at DESC LIMIT ?""",
            (tag, limit),
        ).fetchall()
    if len(hits) < limit and query:
        # FTS 특수문자를 피하려고 따옴표로 감싼다.
        safe = '"' + query.replace('"', " ") + '"'
        try:
            more = conn.execute(
                """SELECT f.shift_id, f.tag, f.text, h.confirmed_at
                   FROM handover_fts f JOIN handover h ON h.shift_id = f.shift_id
                   WHERE handover_fts MATCH ? ORDER BY rank LIMIT ?""",
                (safe, limit),
            ).fetchall()
            seen = {h["shift_id"] for h in hits}
            hits += [m for m in more if m["shift_id"] not in seen]
        except sqlite3.OperationalError:
            pass
    return [dict(h) for h in hits[:limit]]


def confirm_handover(conn, shift_id, confirmed_by, body, adopted, excluded):
    d = conn.execute("SELECT id FROM draft WHERE shift_id = ?", (shift_id,)).fetchone()
    draft_id = d["id"] if d else None
    conn.execute("DELETE FROM handover WHERE shift_id = ?", (shift_id,))
    conn.execute(
        """INSERT INTO handover
           (shift_id, draft_id, confirmed_by, confirmed_at,
            adopted_count, excluded_count, body)
           VALUES (?,?,?,?,?,?,?)""",
        (shift_id, draft_id, confirmed_by, now(), adopted, excluded, body),
    )
    conn.execute("UPDATE draft SET status = 'confirmed' WHERE shift_id = ?", (shift_id,))


def load_handover(conn, shift_id):
    h = conn.execute("SELECT * FROM handover WHERE shift_id = ?", (shift_id,)).fetchone()
    return dict(h) if h else None
