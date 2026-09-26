"""
Neon(PostgreSQL) 接続とスキーマ。

DATABASE_URL は import 時に os.environ から読む。ローカルで使うときは import 前に
.env を読み込むこと(読み込まずに import すると DATABASE_URL が無いまま起動して
即座に失敗する。旧版のような SQLite への黙ったフォールバックはしない)。
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Iterable

import psycopg2
import psycopg2.extras

DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS securities (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    market      TEXT,
    sector33    TEXT,
    sector17    TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS prices (
    code      TEXT NOT NULL,
    date      TEXT NOT NULL,
    open      DOUBLE PRECISION,
    high      DOUBLE PRECISION,
    low       DOUBLE PRECISION,
    close     DOUBLE PRECISION,
    volume    DOUBLE PRECISION,
    turnover  DOUBLE PRECISION,
    PRIMARY KEY (code, date)
);

CREATE TABLE IF NOT EXISTS snapshots (
    date        TEXT NOT NULL,
    code        TEXT NOT NULL,
    close       DOUBLE PRECISION,
    features    JSONB NOT NULL,
    labels      TEXT[] NOT NULL DEFAULT '{}',
    label_version TEXT NOT NULL,
    created_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (date, code)
);

CREATE TABLE IF NOT EXISTS news (
    id          BIGSERIAL PRIMARY KEY,
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,
    source      TEXT NOT NULL,
    title       TEXT NOT NULL,
    title_key   TEXT NOT NULL,
    url         TEXT,
    published_at TIMESTAMPTZ,
    fetched_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (code, source, title, date)
);
CREATE INDEX IF NOT EXISTS news_title_key ON news (title_key);
CREATE INDEX IF NOT EXISTS news_code_date ON news (code, date);

CREATE TABLE IF NOT EXISTS news_reviews (
    title_key   TEXT PRIMARY KEY,
    subjects    TEXT[] NOT NULL DEFAULT '{}',
    n_events    SMALLINT NOT NULL,
    note        TEXT,
    procedure   TEXT NOT NULL,
    model       TEXT,
    reviewed_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS material_events (
    id          BIGSERIAL PRIMARY KEY,
    title_key   TEXT NOT NULL REFERENCES news_reviews(title_key),
    idx         SMALLINT NOT NULL,
    subjects    TEXT[] NOT NULL,
    event_type  TEXT NOT NULL,
    actor       TEXT NOT NULL,
    pathways    TEXT[] NOT NULL,
    scope       TEXT NOT NULL,
    stage       TEXT NOT NULL,
    facts       TEXT NOT NULL,
    amount      TEXT,
    procedure   TEXT NOT NULL,
    model       TEXT,
    labeled_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (title_key, idx)
);

CREATE TABLE IF NOT EXISTS candidates (
    id          BIGSERIAL PRIMARY KEY,
    base_date   TEXT NOT NULL,
    code        TEXT NOT NULL,
    base_close  DOUBLE PRECISION NOT NULL,
    target      DOUBLE PRECISION NOT NULL,
    rank        SMALLINT,
    conviction  SMALLINT,
    thesis      TEXT,
    trigger     TEXT,
    risk        TEXT,
    chart_view  TEXT,
    labels      TEXT[] NOT NULL DEFAULT '{}',
    features    JSONB,
    procedure   TEXT NOT NULL,
    model       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE (base_date, code)
);

CREATE TABLE IF NOT EXISTS selection_runs (
    base_date   TEXT PRIMARY KEY,
    procedure   TEXT NOT NULL,
    pool_size   INTEGER,
    n_selected  INTEGER,
    notes       TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS outcomes (
    candidate_id  BIGINT PRIMARY KEY REFERENCES candidates(id),
    bars_tracked  SMALLINT NOT NULL DEFAULT 0,
    max_high      DOUBLE PRECISION,
    max_ret       DOUBLE PRECISION,
    min_low       DOUBLE PRECISION,
    min_ret       DOUBLE PRECISION,
    hit           BOOLEAN,
    hit_day       SMALLINT,
    final         BOOLEAN NOT NULL DEFAULT FALSE,
    last_date     TEXT,
    updated_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS market_days (
    date        TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    added_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS runs (
    id          BIGSERIAL PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TIMESTAMPTZ DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',
    counts      JSONB,
    message     TEXT
);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id          BIGSERIAL PRIMARY KEY,
    endpoint    TEXT UNIQUE NOT NULL,
    p256dh      TEXT,
    auth        TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
"""


# 既存テーブルへの追加(2026-09-26 v5.3 対応)。何度流しても同じ結果になる
MIGRATIONS = """
ALTER TABLE news ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS t_now TIMESTAMPTZ;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS chart_patterns JSONB;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS supply JSONB;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS material_status TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS material_event_ids BIGINT[];
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS material_analysis TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS teacher_match TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS post_surge_check TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS dilution TEXT;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS path JSONB;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS falsifiers JSONB;
ALTER TABLE candidates ADD COLUMN IF NOT EXISTS routes TEXT[];
ALTER TABLE selection_runs ADD COLUMN IF NOT EXISTS t_prev TIMESTAMPTZ;
ALTER TABLE selection_runs ADD COLUMN IF NOT EXISTS t_now TIMESTAMPTZ;
ALTER TABLE selection_runs ADD COLUMN IF NOT EXISTS funnel JSONB;
ALTER TABLE selection_runs ADD COLUMN IF NOT EXISTS exclusions JSONB;
ALTER TABLE selection_runs ADD COLUMN IF NOT EXISTS report TEXT;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS status TEXT;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS split_note TEXT;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS deadline TEXT;
ALTER TABLE outcomes ADD COLUMN IF NOT EXISTS missing_dates TEXT[];
CREATE TABLE IF NOT EXISTS market_days (date TEXT PRIMARY KEY, source TEXT NOT NULL,
                                        added_at TIMESTAMPTZ DEFAULT now());
"""


class _Cursor:
    def __init__(self, cur):
        self._cur = cur

    def fetchone(self) -> dict | None:
        r = self._cur.fetchone()
        return dict(r) if r is not None else None

    def fetchall(self) -> list[dict]:
        return [dict(r) for r in self._cur.fetchall()]

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


class _Conn:
    def __init__(self, raw):
        self.raw = raw
        self._cur = raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql: str, params=None) -> _Cursor:
        self._cur.execute(sql, params or ())
        return _Cursor(self._cur)

    def executemany(self, sql: str, rows: Iterable[tuple]) -> None:
        rows = list(rows)
        if rows:
            psycopg2.extras.execute_batch(self._cur, sql, rows, page_size=500)


def connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL が未設定です(.env を読み込んでから import すること)")
    return psycopg2.connect(DATABASE_URL)


@contextmanager
def cursor():
    """1 ブロック = 1 トランザクション。例外で rollback、正常終了で commit。"""
    raw = connect()
    try:
        yield _Conn(raw)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def init_db() -> None:
    with cursor() as conn:
        conn.execute(SCHEMA)
        conn.execute(MIGRATIONS)
        # v1 の材料ラベル表(評価語 direction を持つ)は使わない。1 件も入っていない場合だけ消す
        if conn.execute("SELECT to_regclass('news_labels') r").fetchone()["r"]:
            if conn.execute("SELECT COUNT(*) n FROM news_labels").fetchone()["n"] == 0:
                conn.execute("DROP TABLE news_labels")


def j(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
