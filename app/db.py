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

from config import DB_PATH, RAW_RETENTION_DAYS

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
    ingested_at   TEXT
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
    status       TEXT NOT NULL,              -- pending | confirmed
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
    precedent_json   TEXT,                   -- 근거로 삼은 과거 일지
    adopted          INTEGER,                -- NULL 미결정 / 1 채택 / 0 제외
    comment          TEXT,
    decided_at       TEXT,
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
    """예전 DB 에 새 컬럼을 더한다. 서버 시드 DB 를 다시 만들지 않아도 되게."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(draft_item)")}
    for col, typ in (("severity_rule", "TEXT"), ("severity_reason", "TEXT"), ("handover_worthy", "INTEGER"), ("precedent_note", "TEXT"), ("related_tags_ai", "TEXT"), ("related_note", "TEXT")):
        if col not in have:
            conn.execute(f"ALTER TABLE draft_item ADD COLUMN {col} {typ}")
    have_ev = {r[1] for r in conn.execute("PRAGMA table_info(event)")}
    if "waveform_json" not in have_ev:
        conn.execute("ALTER TABLE event ADD COLUMN waveform_json TEXT")


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


def raw_complete(conn, shift_id):
    """창 전체가 남아 있나 — 첫 표본이 창 시작 1분 안이면 온전한 것으로 본다."""
    n, first = raw_coverage(conn, shift_id)
    if not n:
        return False
    row = conn.execute("SELECT window_start FROM shift WHERE id = ?", (shift_id,)).fetchone()
    from datetime import datetime, timedelta
    return datetime.fromisoformat(first) <= datetime.fromisoformat(row["window_start"]) + timedelta(minutes=1)


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

def clear_outputs(conn, shift_id, include_handover=False):
    """한 근무의 산출물을 지운다. 참조 순서를 지켜야 외래키가 깨지지 않는다.

    이벤트가 바뀌면 그 이벤트로 만든 초안도 더는 유효하지 않다. 그래서
    이벤트를 다시 쓸 때 초안을 함께 비운다.
    """
    if include_handover:
        conn.execute("DELETE FROM handover_fts WHERE shift_id = ?", (shift_id,))
        conn.execute("DELETE FROM handover WHERE shift_id = ?", (shift_id,))
    conn.execute(
        "DELETE FROM draft_item WHERE draft_id IN (SELECT id FROM draft WHERE shift_id = ?)",
        (shift_id,),
    )
    conn.execute("DELETE FROM draft WHERE shift_id = ?", (shift_id,))
    conn.execute("DELETE FROM event WHERE shift_id = ?", (shift_id,))


def save_events(conn, shift_id, events, detector):
    clear_outputs(conn, shift_id)
    conn.executemany(
        """INSERT INTO event
           (shift_id, tag, kind, start_ts, end_ts, severity, score,
            metrics_json, evidence, detector, created_at, waveform_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                shift_id, e["tag"], e["kind"], e.get("start_ts"), e.get("end_ts"),
                e.get("severity"), e.get("score"),
                json.dumps(e.get("metrics", {}), ensure_ascii=False),
                e.get("evidence"), detector, now(),
                json.dumps(e.get("waveform"), ensure_ascii=False) if e.get("waveform") else None,
            )
            for e in events
        ],
    )


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
    conn.execute("DELETE FROM draft_item WHERE draft_id IN "
                 "(SELECT id FROM draft WHERE shift_id = ?)", (shift_id,))
    conn.execute("DELETE FROM draft WHERE shift_id = ?", (shift_id,))
    cur = conn.execute(
        """INSERT INTO draft (shift_id, status, generator, model, generated_at)
           VALUES (?, 'pending', ?, ?, ?)""",
        (shift_id, generator, model, now()),
    )
    draft_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO draft_item
           (draft_id, event_id, seq, origin, tag, title, body, evidence,
            severity, suggested_action, severity_rule, severity_reason, handover_worthy, precedent_note, related_tags_ai, related_note, precedent_json, adopted)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)""",
        [
            (
                draft_id, it.get("event_id"), i, it.get("origin", "detected"),
                it.get("tag"), it.get("title"), it.get("body"), it.get("evidence"),
                it.get("severity"), it.get("suggested_action"),
                it.get("severity_rule"), it.get("severity_reason"),
                (None if it.get("handover_worthy") is None else int(bool(it["handover_worthy"]))),
                it.get("precedent_note"),
                json.dumps(it.get("related_tags_ai") or [], ensure_ascii=False), it.get("related_note"),
                json.dumps(it.get("precedents", []), ensure_ascii=False),
            )
            for i, it in enumerate(items, start=1)
        ],
    )
    return draft_id


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
        try:
            it["related_tags_ai"] = json.loads(it.get("related_tags_ai") or "[]")
        except (TypeError, ValueError):
            it["related_tags_ai"] = []
        draft["items"].append(it)
    return draft


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
            for it in items
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
