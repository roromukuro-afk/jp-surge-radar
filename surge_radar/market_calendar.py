"""
東証の営業日カレンダー。

判定期間(基準日の後の 10 営業日)を「保存されている株価の本数」ではなく営業日の日付で決めるために使う。
保存している株価に欠け(実行しなかった日・取得失敗)があっても、期間がずれて 11 営業日目以降を
数えることがないようにする。

営業日は TOPIX 連動 ETF(1306)の日足がある日とする。1306 は毎営業日売買がある。
"""
from __future__ import annotations

from . import db

REFERENCE_CODE = "1306"


def refresh() -> dict:
    """1306 の直近 3 か月の日足から営業日を記録する(追加のみ)。"""
    from .sources import yahoo
    df = yahoo.fetch_ohlcv(REFERENCE_CODE, range_="3mo")
    if df.empty:
        raise RuntimeError("営業日カレンダーの取得に失敗(1306 の日足が空)")
    dates = sorted(set(df["date"]))
    with db.cursor() as conn:
        before = conn.execute("SELECT COUNT(*) n FROM market_days").fetchone()["n"]
        conn.executemany("INSERT INTO market_days(date, source) VALUES(%s, %s) ON CONFLICT DO NOTHING",
                         [(d, REFERENCE_CODE) for d in dates])
        after = conn.execute("SELECT COUNT(*) n FROM market_days").fetchone()["n"]
    return {"latest": dates[-1], "added": after - before}


def days() -> list[str]:
    with db.cursor() as conn:
        return [r["date"] for r in conn.execute("SELECT date FROM market_days ORDER BY date").fetchall()]


def window(base_date: str, n: int, all_days: list[str] | None = None) -> list[str]:
    """基準日の後の最初の n 営業日のうち、すでに来た日(カレンダーにある日)。"""
    d = all_days if all_days is not None else days()
    return [x for x in d if x > base_date][:n]
