"""
候補の成否を追跡する。

判定: 基準日の終値 × 1.2 に、基準日の翌営業日から 10 営業日以内の場中高値が届けば成功。
一度届いた時点で成功は確定する(hit=True)。最高値・最安値は 10 営業日ぶん記録し続け、
10 営業日を過ぎたら final=True にする。IR などの理由を問わず、届いたかどうかだけで判定する。
"""
from __future__ import annotations

from . import db

WINDOW = 10
TARGET_RET = 0.20


def evaluate(base_close: float, bars: list[dict]) -> dict:
    """bars: 基準日より後の日足(日付昇順)。先頭 WINDOW 本だけを見る。"""
    bars = [b for b in bars if b.get("high") is not None][:WINDOW]
    target = base_close * (1 + TARGET_RET)
    out = {"bars_tracked": len(bars), "max_high": None, "max_ret": None,
           "min_low": None, "min_ret": None, "hit": False, "hit_day": None,
           "final": len(bars) >= WINDOW, "last_date": bars[-1]["date"] if bars else None}
    if not bars:
        return out
    for i, b in enumerate(bars, 1):
        if out["hit_day"] is None and b["high"] >= target:
            out["hit"], out["hit_day"] = True, i
    out["max_high"] = max(b["high"] for b in bars)
    out["min_low"] = min(b["low"] for b in bars)
    out["max_ret"] = out["max_high"] / base_close - 1
    out["min_ret"] = out["min_low"] / base_close - 1
    return out


def update_all() -> dict:
    with db.cursor() as conn:
        cands = conn.execute(
            """SELECT c.id, c.code, c.base_date, c.base_close
               FROM candidates c LEFT JOIN outcomes o ON o.candidate_id = c.id
               WHERE o.final IS NOT TRUE""").fetchall()
    updated = 0
    for c in cands:
        with db.cursor() as conn:
            bars = conn.execute(
                "SELECT date, high, low FROM prices WHERE code=%s AND date>%s "
                "ORDER BY date LIMIT %s", (c["code"], c["base_date"], WINDOW)).fetchall()
            r = evaluate(c["base_close"], bars)
            conn.execute(
                """INSERT INTO outcomes(candidate_id,bars_tracked,max_high,max_ret,min_low,
                                        min_ret,hit,hit_day,final,last_date,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                   ON CONFLICT(candidate_id) DO UPDATE SET
                     bars_tracked=excluded.bars_tracked, max_high=excluded.max_high,
                     max_ret=excluded.max_ret, min_low=excluded.min_low,
                     min_ret=excluded.min_ret, hit=excluded.hit, hit_day=excluded.hit_day,
                     final=excluded.final, last_date=excluded.last_date, updated_at=now()""",
                (c["id"], r["bars_tracked"], r["max_high"], r["max_ret"], r["min_low"],
                 r["min_ret"], r["hit"], r["hit_day"], r["final"], r["last_date"]))
            updated += 1
    return {"tracked": updated}
