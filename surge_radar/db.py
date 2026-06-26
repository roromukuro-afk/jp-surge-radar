"""
DB接続ヘルパ。

SQLite (ローカル開発) と PostgreSQL (クラウド本番) の両方をサポート。
DATABASE_URL 環境変数が設定されている場合は psycopg2 経由で PostgreSQL を使用。
設定がない場合は SQLite WAL モードを使用 (ローカル開発向け)。

全てのSQL文は %s プレースホルダ形式で記述すること。
SQLite モードでは内部で ? に自動変換される。
"""
from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from typing import Any, Iterable

from .config import DB_PATH

DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

# PostgreSQL の場合 RETURNING id を付与するテーブル (BIGSERIAL PRIMARY KEY 列を持つもの)
_TABLES_WITH_AUTO_ID = {"materials", "predictions", "teacher_samples", "job_logs", "push_subscriptions"}

# ---------- スキーマ定義 ----------

SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS securities (
    code            TEXT PRIMARY KEY,
    name            TEXT,
    market          TEXT,
    sector33        TEXT,
    sector17        TEXT,
    shares_out      REAL,
    listed_date     TEXT,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS prices (
    code     TEXT NOT NULL,
    date     TEXT NOT NULL,
    open     REAL, high REAL, low REAL, close REAL,
    volume   REAL,
    turnover REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

CREATE TABLE IF NOT EXISTS materials (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT,
    date        TEXT NOT NULL,
    source      TEXT,
    category    TEXT,
    title       TEXT,
    url         TEXT,
    body        TEXT,
    sentiment   REAL,
    impact      REAL,
    persistence REAL,
    unpriced    REAL,
    connect     REAL,
    raw         TEXT,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_materials_code_date ON materials(code, date);

CREATE TABLE IF NOT EXISTS theme_regime (
    date     TEXT NOT NULL,
    theme    TEXT NOT NULL,
    trend    REAL,
    above_ma INTEGER,
    vol_up   INTEGER,
    note     TEXT,
    PRIMARY KEY (date, theme)
);

CREATE TABLE IF NOT EXISTS predictions (
    id              BIGSERIAL PRIMARY KEY,
    run_date        TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    base_price      REAL,
    rank            INTEGER,
    score           REAL,
    probability     REAL,
    category        TEXT,
    material_score  REAL,
    chart_score     REAL,
    volume_score    REAL,
    theme_score     REAL,
    fundamental_score REAL,
    similarity_score  REAL,
    reasons         TEXT,
    failure_conditions TEXT,
    features        TEXT,
    flags           TEXT,
    model_version   TEXT,
    status          TEXT DEFAULT 'open',
    origin          TEXT DEFAULT 'live',
    top_material    TEXT,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pred_run ON predictions(run_date);
CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status);

CREATE TABLE IF NOT EXISTS prediction_outcomes (
    prediction_id   BIGINT PRIMARY KEY,
    judged_date     TEXT,
    bars_tracked    INTEGER,
    max_up_5d       REAL,
    max_up_10d      REAL,
    max_up_20d      REAL,
    days_to_20pct   INTEGER,
    max_drawdown    REAL,
    close_up_maintained INTEGER,
    faded_after_high    INTEGER,
    material_continued  INTEGER,
    volume_continued    INTEGER,
    result_class    TEXT,
    failure_tags    TEXT,
    notes           TEXT,
    next_learning   TEXT,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE TABLE IF NOT EXISTS teacher_samples (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT,
    code        TEXT,
    t0_date     TEXT,
    label       INTEGER,
    features    TEXT,
    tags        TEXT,
    prediction_id INTEGER,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (code, t0_date)
);
CREATE INDEX IF NOT EXISTS idx_teacher_source ON teacher_samples(source);
CREATE UNIQUE INDEX IF NOT EXISTS idx_teacher_code_date ON teacher_samples(code, t0_date);

CREATE TABLE IF NOT EXISTS model_meta (
    version     TEXT PRIMARY KEY,
    trained_at  TEXT,
    n_samples   INTEGER,
    n_pos       INTEGER,
    n_neg       INTEGER,
    metrics     TEXT,
    feature_importance TEXT,
    notes       TEXT,
    model_data  BYTEA
);

CREATE TABLE IF NOT EXISTS job_logs (
    id          BIGSERIAL PRIMARY KEY,
    job         TEXT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,
    counts      TEXT,
    message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_joblog_job ON job_logs(job);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    endpoint    TEXT UNIQUE NOT NULL,
    p256dh      TEXT,
    auth        TEXT,
    user_agent  TEXT,
    created_at  TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    last_used   TIMESTAMPTZ
);
"""

SCHEMA_SQLITE = """
CREATE TABLE IF NOT EXISTS securities (
    code            TEXT PRIMARY KEY,
    name            TEXT,
    market          TEXT,
    sector33        TEXT,
    sector17        TEXT,
    shares_out      REAL,
    listed_date     TEXT,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS prices (
    code     TEXT NOT NULL,
    date     TEXT NOT NULL,
    open     REAL, high REAL, low REAL, close REAL,
    volume   REAL,
    turnover REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);

CREATE TABLE IF NOT EXISTS materials (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT,
    date        TEXT NOT NULL,
    source      TEXT,
    category    TEXT,
    title       TEXT,
    url         TEXT,
    body        TEXT,
    sentiment   REAL,
    impact      REAL,
    persistence REAL,
    unpriced    REAL,
    connect     REAL,
    raw         TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_materials_code_date ON materials(code, date);

CREATE TABLE IF NOT EXISTS theme_regime (
    date     TEXT NOT NULL,
    theme    TEXT NOT NULL,
    trend    REAL,
    above_ma INTEGER,
    vol_up   INTEGER,
    note     TEXT,
    PRIMARY KEY (date, theme)
);

CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date        TEXT NOT NULL,
    code            TEXT NOT NULL,
    name            TEXT,
    base_price      REAL,
    rank            INTEGER,
    score           REAL,
    probability     REAL,
    category        TEXT,
    material_score  REAL,
    chart_score     REAL,
    volume_score    REAL,
    theme_score     REAL,
    fundamental_score REAL,
    similarity_score  REAL,
    reasons         TEXT,
    failure_conditions TEXT,
    features        TEXT,
    flags           TEXT,
    model_version   TEXT,
    status          TEXT DEFAULT 'open',
    origin          TEXT DEFAULT 'live',
    top_material    TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_pred_run ON predictions(run_date);
CREATE INDEX IF NOT EXISTS idx_pred_status ON predictions(status);

CREATE TABLE IF NOT EXISTS prediction_outcomes (
    prediction_id   INTEGER PRIMARY KEY,
    judged_date     TEXT,
    bars_tracked    INTEGER,
    max_up_5d       REAL,
    max_up_10d      REAL,
    max_up_20d      REAL,
    days_to_20pct   INTEGER,
    max_drawdown    REAL,
    close_up_maintained INTEGER,
    faded_after_high    INTEGER,
    material_continued  INTEGER,
    volume_continued    INTEGER,
    result_class    TEXT,
    failure_tags    TEXT,
    notes           TEXT,
    next_learning   TEXT,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (prediction_id) REFERENCES predictions(id)
);

CREATE TABLE IF NOT EXISTS teacher_samples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT,
    code        TEXT,
    t0_date     TEXT,
    label       INTEGER,
    features    TEXT,
    tags        TEXT,
    prediction_id INTEGER,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_teacher_source ON teacher_samples(source);

CREATE TABLE IF NOT EXISTS model_meta (
    version     TEXT PRIMARY KEY,
    trained_at  TEXT,
    n_samples   INTEGER,
    n_pos       INTEGER,
    n_neg       INTEGER,
    metrics     TEXT,
    feature_importance TEXT,
    notes       TEXT,
    model_data  BLOB
);

CREATE TABLE IF NOT EXISTS job_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT,
    started_at  TEXT,
    finished_at TEXT,
    status      TEXT,
    counts      TEXT,
    message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_joblog_job ON job_logs(job);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint    TEXT UNIQUE NOT NULL,
    p256dh      TEXT,
    auth        TEXT,
    user_agent  TEXT,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    last_used   TEXT
);
"""


# ---------- SQL 方言アダプタ ----------

def _adapt_pg(sql: str) -> tuple[str, bool]:
    """
    PostgreSQL 用にSQL文を変換。
    返り値: (変換後SQL, id取得フラグ)
    id取得フラグ=True の場合、RETURNING id が付いているので execute 後に fetchone() で id を取得する。
    """
    # json_extract(col, '$.key') → (col::json)->>'key'
    sql = re.sub(
        r"json_extract\(([^,]+),\s*'\$\.([^']+)'\)",
        lambda m: f"({m.group(1).strip()}::json)->>'{m.group(2)}'",
        sql,
    )

    # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    if re.search(r"INSERT\s+OR\s+IGNORE", sql, re.I):
        sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", sql, flags=re.I)
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return sql, False

    # INSERT OR REPLACE → INSERT ... ON CONFLICT (version) DO UPDATE SET ...
    # model_meta 専用 — model.py 側で完全なUPSERT文を使用すること
    if re.search(r"INSERT\s+OR\s+REPLACE", sql, re.I):
        sql = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "INSERT INTO", sql, flags=re.I)
        return sql, False

    # 通常の INSERT: 自動ID列があるテーブルなら RETURNING id を付与
    m = re.search(r"^\s*INSERT\s+INTO\s+(\w+)", sql, re.I)
    if m:
        table = m.group(1).lower()
        if (table in _TABLES_WITH_AUTO_ID
                and "ON CONFLICT" not in sql.upper()
                and "RETURNING" not in sql.upper()):
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            return sql, True

    return sql, False


def _adapt_sqlite(sql: str) -> str:
    """SQLite 用: %s → ? に変換。"""
    return sql.replace("%s", "?")


# ---------- PostgreSQL ラッパー ----------

class _PGCursor:
    """psycopg2 カーソルを sqlite3 互換インターフェースでラップ。"""

    def __init__(self, cur, last_id: int | None = None):
        self._cur = cur
        self._lastrowid = last_id

    def fetchone(self) -> dict | None:
        row = self._cur.fetchone()
        return dict(row) if row else None

    def fetchall(self) -> list[dict]:
        return [dict(r) for r in (self._cur.fetchall() or [])]

    @property
    def lastrowid(self) -> int | None:
        return self._lastrowid

    def __iter__(self):
        return iter(self.fetchall())


class _PGConn:
    """psycopg2 接続を sqlite3.Connection 互換インターフェースでラップ。"""

    def __init__(self, raw_conn):
        import psycopg2.extras
        self._conn = raw_conn
        self._cur = raw_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: str, params=None) -> _PGCursor:
        adapted, needs_id = _adapt_pg(sql)
        self._cur.execute(adapted, params or ())
        last_id = None
        if needs_id:
            row = self._cur.fetchone()
            if row:
                last_id = dict(row).get("id")
        return _PGCursor(self._cur, last_id)

    def executemany(self, sql: str, rows: Iterable[tuple]) -> None:
        import psycopg2.extras
        adapted, _ = _adapt_pg(sql)
        psycopg2.extras.execute_batch(self._cur, adapted, list(rows))

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        try:
            self._cur.close()
        except Exception:
            pass
        self._conn.close()


# ---------- SQLite ラッパー ----------

class _SQLiteCursor:
    """sqlite3.Cursor を dict ベースの結果に正規化。"""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self) -> dict | None:
        row = self._cur.fetchone()
        if row is None:
            return None
        return dict(row) if hasattr(row, "keys") else row

    def fetchall(self) -> list[dict]:
        rows = self._cur.fetchall()
        return [dict(r) if hasattr(r, "keys") else r for r in rows]

    @property
    def lastrowid(self) -> int | None:
        return self._cur.lastrowid

    def __iter__(self):
        return iter(self.fetchall())


class _SQLiteConn:
    """sqlite3.Connection を %s プレースホルダ対応にラップ。"""

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql: str, params=None) -> _SQLiteCursor:
        sql = _adapt_sqlite(sql)
        cur = self._conn.execute(sql, params or ())
        return _SQLiteCursor(cur)

    def executemany(self, sql: str, rows: Iterable[tuple]) -> None:
        sql = _adapt_sqlite(sql)
        self._conn.executemany(sql, list(rows))

    def executescript(self, sql: str) -> None:
        self._conn.executescript(sql)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


# ---------- 接続・初期化 ----------

def _connect_pg() -> _PGConn:
    import psycopg2
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return _PGConn(conn)


def _connect_sqlite() -> _SQLiteConn:
    import sqlite3
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return _SQLiteConn(conn)


def connect() -> _PGConn | _SQLiteConn:
    return _connect_pg() if DATABASE_URL else _connect_sqlite()


def init_db() -> None:
    conn = connect()
    try:
        if DATABASE_URL:
            _create_pg(conn)
            _migrate_pg(conn)
        else:
            conn.executescript(SCHEMA_SQLITE)
            _migrate_sqlite(conn)
        conn.commit()
    finally:
        conn.close()


def _create_pg(conn: _PGConn) -> None:
    """PostgreSQL スキーマを1文ずつ実行 (executescript は存在しないため)。"""
    for stmt in SCHEMA_PG.split(";"):
        stmt = stmt.strip()
        if stmt:
            try:
                conn._cur.execute(stmt)
            except Exception as e:
                # 既存オブジェクトの重複は無視
                if "already exists" not in str(e).lower():
                    conn._conn.rollback()
                    raise
                conn._conn.rollback()
                conn._conn.autocommit = False


def _migrate_pg(conn: _PGConn) -> None:
    """PostgreSQL: 既存 DB にカラム・インデックスを追加する差分マイグレーション。"""
    # predictions: origin, top_material
    r = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='predictions'"
    ).fetchall()
    existing = {row["column_name"] for row in r}
    if "origin" not in existing:
        conn._cur.execute("ALTER TABLE predictions ADD COLUMN origin TEXT DEFAULT 'live'")
    if "top_material" not in existing:
        conn._cur.execute("ALTER TABLE predictions ADD COLUMN top_material TEXT")

    # model_meta: model_data
    r = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='model_meta'"
    ).fetchall()
    existing = {row["column_name"] for row in r}
    if "model_data" not in existing:
        conn._cur.execute("ALTER TABLE model_meta ADD COLUMN model_data BYTEA")

    # teacher_samples: UNIQUE インデックス
    r = conn.execute(
        "SELECT indexname FROM pg_indexes WHERE tablename='teacher_samples' "
        "AND indexname='idx_teacher_code_date'"
    ).fetchall()
    if not r:
        conn._cur.execute("""
            DELETE FROM teacher_samples WHERE id NOT IN (
                SELECT MIN(id) FROM teacher_samples GROUP BY code, t0_date
            )
        """)
        conn._cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_teacher_code_date "
            "ON teacher_samples(code, t0_date)"
        )


def _migrate_sqlite(conn: _SQLiteConn) -> None:
    """SQLite: カラム追加・インデックスマイグレーション。"""
    import sqlite3
    raw = conn._conn
    existing = {r[1] for r in raw.execute("PRAGMA table_info(predictions)").fetchall()}
    if "origin" not in existing:
        raw.execute("ALTER TABLE predictions ADD COLUMN origin TEXT DEFAULT 'live'")
    if "top_material" not in existing:
        raw.execute("ALTER TABLE predictions ADD COLUMN top_material TEXT")

    existing_mm = {r[1] for r in raw.execute("PRAGMA table_info(model_meta)").fetchall()}
    if "model_data" not in existing_mm:
        raw.execute("ALTER TABLE model_meta ADD COLUMN model_data BLOB")

    idx_names = {r[1] for r in raw.execute("SELECT * FROM sqlite_master WHERE type='index'").fetchall()}
    if "idx_teacher_code_date" not in idx_names:
        raw.execute("""
            DELETE FROM teacher_samples WHERE id NOT IN (
                SELECT MIN(id) FROM teacher_samples GROUP BY code, t0_date
            )
        """)
        raw.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_teacher_code_date "
            "ON teacher_samples(code, t0_date)"
        )


@contextmanager
def cursor():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------- ユーティリティ ----------

def j(obj: Any) -> str:
    """JSON シリアライズ (numpy 型対策)。"""
    def default(o):
        try:
            import numpy as np
            if isinstance(o, np.floating):
                return float(o)
            if isinstance(o, np.integer):
                return int(o)
        except Exception:
            pass
        return str(o)
    return json.dumps(obj, ensure_ascii=False, default=default)


def loadj(s: str | None, fallback=None):
    if not s:
        return fallback
    try:
        return json.loads(s)
    except Exception:
        return fallback


def executemany(sql: str, rows: Iterable[tuple]) -> None:
    with cursor() as conn:
        conn.executemany(sql, list(rows))


if __name__ == "__main__":
    init_db()
    print(f"DB initialized (pg={bool(DATABASE_URL)}) at {DB_PATH if not DATABASE_URL else DATABASE_URL[:40]}")
