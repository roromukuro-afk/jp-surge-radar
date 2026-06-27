"""
価格データ取り込み。Yahoo(主) / J-Quants(併用) から日足を取得し SQLite に保存。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd

from . import db
from .sources import jquants, yahoo


def upsert_prices(code: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = [
        (code, r["date"], _f(r.get("open")), _f(r.get("high")), _f(r.get("low")),
         _f(r.get("close")), _f(r.get("volume")), _f(r.get("turnover")))
        for _, r in df.iterrows()
    ]
    with db.cursor() as conn:
        conn.executemany(
            """INSERT INTO prices(code,date,open,high,low,close,volume,turnover)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(code,date) DO UPDATE SET
                 open=excluded.open,high=excluded.high,low=excluded.low,
                 close=excluded.close,volume=excluded.volume,turnover=excluded.turnover""",
            rows,
        )
    return len(rows)


def _f(x):
    try:
        if x is None or pd.isna(x):
            return None
        return float(x)
    except Exception:
        return None


def fetch_one(code: str, range_: str = "2y") -> int:
    """1銘柄を取得して保存。J-Quants優先(あれば)→Yahoo。返り値は保存行数。"""
    df = pd.DataFrame()
    if jquants.is_available():
        df = jquants.fetch_ohlcv(code)
    if df.empty:
        df = yahoo.fetch_ohlcv(code, range_=range_)
    return upsert_prices(code, df)


def fetch_many(codes: list[str], range_: str = "2y", pause: float = 0.25,
               log_every: int = 100, on_progress=None) -> dict:
    """複数銘柄を取得。レート制限に配慮しつつ進捗を返す。"""
    ok = 0; fail = 0; total_rows = 0
    failed_codes = []
    for i, code in enumerate(codes, 1):
        try:
            n = fetch_one(code, range_=range_)
            if n > 0:
                ok += 1; total_rows += n
            else:
                fail += 1; failed_codes.append(code)
        except Exception:
            fail += 1; failed_codes.append(code)
        if on_progress and i % log_every == 0:
            on_progress(i, len(codes), ok, fail)
        time.sleep(pause)
    return {"ok": ok, "fail": fail, "rows": total_rows, "failed_codes": failed_codes[:200]}


def load_history(code: str, min_bars: int = 0) -> pd.DataFrame:
    """DBから日足を昇順で取得。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,turnover FROM prices "
            "WHERE code=%s ORDER BY date ASC", (code,)
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    if min_bars and len(df) < min_bars:
        return df
    return df


def latest_price(code: str) -> dict | None:
    with db.cursor() as conn:
        r = conn.execute(
            "SELECT date,close,volume FROM prices WHERE code=%s ORDER BY date DESC LIMIT 1", (code,)
        ).fetchone()
    return dict(r) if r else None


def available_codes() -> list[str]:
    with db.cursor() as conn:
        rows = conn.execute("SELECT DISTINCT code FROM prices").fetchall()
    return [r["code"] for r in rows]


def load_history_bulk(codes: list[str], chunk: int = 500) -> dict[str, "pd.DataFrame"]:
    """複数銘柄の日足をまとめて取得 (1クエリ/チャンク)。{code: df(昇順)} を返す。

    predict 等で銘柄ごとに load_history する代わりに使い、DB 往復を激減させる。
    価格データが無い銘柄はキーに含まれない。
    """
    if not codes:
        return {}
    cols = ["date", "open", "high", "low", "close", "volume", "turnover"]
    by_code: dict[str, list] = {}
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        ph = ",".join(["%s"] * len(part))
        with db.cursor() as conn:
            rows = conn.execute(
                f"SELECT code,date,open,high,low,close,volume,turnover FROM prices "
                f"WHERE code IN ({ph}) ORDER BY code, date ASC", tuple(part)
            ).fetchall()
        for r in rows:
            by_code.setdefault(r["code"], []).append(r)
    out: dict[str, pd.DataFrame] = {}
    for c, rs in by_code.items():
        out[c] = pd.DataFrame([{k: r[k] for k in cols} for r in rs])
    return out


def stale_codes(codes: list[str], stale_days: int = 2) -> list[str]:
    """
    直近 stale_days 日以内に価格データがないコードのみ返す。
    既に最新データがある銘柄をスキップするための差分取得支援。
    """
    cutoff = (datetime.now() - timedelta(days=stale_days)).strftime("%Y-%m-%d")
    with db.cursor() as conn:
        fresh = {r["code"] for r in conn.execute(
            "SELECT code FROM prices WHERE date >= %s GROUP BY code", (cutoff,)
        ).fetchall()}
    return [c for c in codes if c not in fresh]
