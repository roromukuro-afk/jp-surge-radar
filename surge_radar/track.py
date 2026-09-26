"""
候補の成否を追跡する。

判定: 基準日の終値(P0)× 1.2 に、基準日の翌営業日から 10 営業日以内の場中高値が届けば成功。
一度届いた時点で成功は確定する。11 営業日目以降の到達は数えない。

状態(outcomes.status):
  hit         成功(届いた)
  miss        失敗(10 営業日を過ぎても届かなかった)
  tracking    判定期間中(未達で、まだ 10 営業日たっていない)
  unverified  判定未確認(市場は 10 営業日進んだが、その銘柄の日足が揃わない。失敗にしない)

株式分割・併合: 判定期間中に起きた場合、P0 と、分割前に保存した日足を分割後の基準に揃えてから判定する
(見かけの値動きを +20% と誤認しないため)。分割の有無を確認できなかったときは判定を確定しない。
"""
from __future__ import annotations

from . import db

WINDOW = 10
TARGET_RET = 0.20


def evaluate(base_close: float, bars: list[dict]) -> dict:
    """bars: 基準日より後の日足(日付昇順、同じ価格基準)。先頭 WINDOW 本だけを見る。"""
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


def adjust_for_splits(base_close: float, base_date: str, bars: list[dict],
                      splits: list[dict]) -> tuple[float, list[dict], str | None]:
    """判定期間中の分割(基準日より後〜最後の足まで)について、P0 と分割日より前の足を分割後の基準に揃える。
    日足は毎日直近 5 日分だけ取り直しているので、分割日より前に保存した足は分割前の価格のまま残っている。"""
    last = bars[-1]["date"] if bars else base_date
    hits = [s for s in splits if base_date < s["date"] <= last]
    if not hits:
        return base_close, bars, None
    p0 = base_close
    adj = [dict(b) for b in bars]
    for s in hits:
        p0 /= s["ratio"]
        for b in adj:
            if b["date"] < s["date"]:
                b["high"] /= s["ratio"]
                b["low"] /= s["ratio"]
    note = "; ".join(f"{s['date']} 比率 {s['ratio']:g}" for s in hits)
    return p0, adj, note


def update_all() -> dict:
    from .sources import yahoo
    with db.cursor() as conn:
        days = [r["date"] for r in conn.execute(
            "SELECT date FROM prices GROUP BY date HAVING COUNT(*) > 1000 ORDER BY date").fetchall()]
        cands = conn.execute(
            """SELECT c.id, c.code, c.base_date, c.base_close
               FROM candidates c LEFT JOIN outcomes o ON o.candidate_id = c.id
               WHERE o.final IS NOT TRUE AND o.status IS DISTINCT FROM 'unverified'""").fetchall()
    counts = {"tracked": 0, "split_adjusted": 0, "split_check_failed": 0}
    for c in cands:
        with db.cursor() as conn:
            bars = conn.execute(
                "SELECT date, high, low FROM prices WHERE code=%s AND date>%s "
                "ORDER BY date LIMIT %s", (c["code"], c["base_date"], WINDOW)).fetchall()
        split_note, split_ok = None, True
        p0 = c["base_close"]
        if bars:
            try:
                p0, bars, split_note = adjust_for_splits(
                    c["base_close"], c["base_date"], bars, yahoo.fetch_splits(c["code"], "3mo"))
            except Exception as e:  # 分割の有無が分からない → 判定を確定しない
                split_ok = False
                split_note = f"分割の確認に失敗: {type(e).__name__}"
                counts["split_check_failed"] += 1
        r = evaluate(p0, bars)
        if split_note and split_ok:
            counts["split_adjusted"] += 1
        market_days = sum(1 for d in days if d > c["base_date"])
        if r["hit"] and split_ok:
            status = "hit"
        elif r["final"] and split_ok:
            status = "miss"
        elif market_days >= WINDOW and r["bars_tracked"] < WINDOW:
            status = "unverified"
        else:
            status = "tracking"
        final = status in ("hit", "miss") and r["final"]
        with db.cursor() as conn:
            conn.execute(
                """INSERT INTO outcomes(candidate_id,bars_tracked,max_high,max_ret,min_low,min_ret,hit,
                                        hit_day,final,last_date,status,split_note,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                   ON CONFLICT(candidate_id) DO UPDATE SET
                     bars_tracked=excluded.bars_tracked, max_high=excluded.max_high,
                     max_ret=excluded.max_ret, min_low=excluded.min_low, min_ret=excluded.min_ret,
                     hit=excluded.hit, hit_day=excluded.hit_day, final=excluded.final,
                     last_date=excluded.last_date, status=excluded.status,
                     split_note=excluded.split_note, updated_at=now()""",
                (c["id"], r["bars_tracked"], r["max_high"], r["max_ret"], r["min_low"],
                 r["min_ret"], r["hit"] and split_ok, r["hit_day"] if split_ok else None, final,
                 r["last_date"], status, split_note))
        counts["tracked"] += 1
    return counts
