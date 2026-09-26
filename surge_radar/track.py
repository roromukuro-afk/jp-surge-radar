"""
候補の成否を追跡する。

判定: 基準日の終値(P0)× 1.2 に、基準日の後の最初の 10 営業日の場中高値が届けば成功。
一度届いた時点で成功は確定する。11 営業日目以降の到達は数えない。

判定期間は営業日カレンダー(market_calendar)の日付で決める。保存している株価の本数では数えない
(株価が 1 日欠けると 10 本目が 11 営業日目になり、期間外の高値で成功にしてしまうため)。

状態(outcomes.status):
  hit         成功(期間内の場中高値が届いた)
  miss        失敗(10 営業日が過ぎ、期間内の全営業日の株価がそろっていて、届かなかった)
  tracking    判定期間中(未達で、まだ 10 営業日たっていない)
  unverified  判定未確認(10 営業日たったが、期間内に株価の無い営業日がある。失敗にしない)

株式分割・併合: 基準日の後に起きた場合、保存済みの株価(分割前後の基準が混ざる)は使わず、Yahoo から
取り直した株価(すべて分割後の基準)で判定し、P0 を分割比率で割って基準を揃える。
分割の有無を確認できなかったときは判定を確定しない。
"""
from __future__ import annotations

from . import db, market_calendar

WINDOW = 10
TARGET_RET = 0.20


def evaluate(base_close: float, bars: list[dict], window_dates: list[str]) -> dict:
    """window_dates: 基準日の後の営業日のうち、すでに来た日(最大 WINDOW 日)。
    bars: その銘柄の株価(同じ価格基準)。window_dates に入らない日の株価は見ない。"""
    window_dates = window_dates[:WINDOW]
    by_date = {b["date"]: b for b in bars if b.get("high") is not None}
    target = base_close * (1 + TARGET_RET)
    present = [d for d in window_dates if d in by_date]
    missing = [d for d in window_dates if d not in by_date]
    out = {"days_elapsed": len(window_dates), "bars_tracked": len(present), "missing_dates": missing,
           "max_high": None, "max_ret": None, "min_low": None, "min_ret": None,
           "hit": False, "hit_day": None,
           "final": len(window_dates) >= WINDOW,
           "last_date": window_dates[-1] if window_dates else None,
           "deadline": window_dates[WINDOW - 1] if len(window_dates) >= WINDOW else None}
    for i, d in enumerate(window_dates, 1):   # hit_day は営業日の番号(株価の本数ではない)
        b = by_date.get(d)
        if b and out["hit_day"] is None and b["high"] >= target:
            out["hit"], out["hit_day"] = True, i
    if present:
        out["max_high"] = max(by_date[d]["high"] for d in present)
        out["min_low"] = min(by_date[d]["low"] for d in present)
        out["max_ret"] = out["max_high"] / base_close - 1
        out["min_ret"] = out["min_low"] / base_close - 1
    return out


def status_of(r: dict, split_ok: bool = True) -> str:
    if not split_ok:
        return "tracking"          # 分割を確認できるまで確定しない
    if r["hit"]:
        return "hit"
    if r["final"]:
        return "unverified" if r["missing_dates"] else "miss"
    return "tracking"


def split_factor(base_date: str, splits: list[dict]) -> tuple[float, str | None]:
    """基準日より後に起きた分割・併合の比率の積と、その記録。
    Yahoo から取り直した株価は取得時点までの全分割を反映しているので、P0 をこの比率で割れば基準が揃う。"""
    after = [s for s in splits if s["date"] > base_date]
    if not after:
        return 1.0, None
    f = 1.0
    for s in after:
        f *= s["ratio"]
    return f, "; ".join(f"{s['date']} 比率 {s['ratio']:g}" for s in after)


def update_all() -> dict:
    from .sources import yahoo
    all_days = market_calendar.days()
    with db.cursor() as conn:
        cands = conn.execute(
            """SELECT c.id, c.code, c.base_date, c.base_close
               FROM candidates c LEFT JOIN outcomes o ON o.candidate_id = c.id
               WHERE o.status IS NULL OR o.status = 'tracking'
                  OR (o.status = 'hit' AND o.final IS NOT TRUE)""").fetchall()
    counts = {"tracked": 0, "split_adjusted": 0, "split_check_failed": 0}
    for c in cands:
        wdates = market_calendar.window(c["base_date"], WINDOW, all_days)
        with db.cursor() as conn:
            bars = conn.execute(
                "SELECT date, high, low FROM prices WHERE code=%s AND date = ANY(%s) ORDER BY date",
                (c["code"], wdates)).fetchall() if wdates else []
        split_note, split_ok, p0 = None, True, c["base_close"]
        if wdates:
            # 基準日の後に分割があれば、保存済みの行(分割前の基準が混ざる)を使わず、
            # Yahoo から取り直した株価(全部が分割後の基準)で判定し、P0 だけを比率で割る
            try:
                fresh = yahoo.fetch_ohlcv(c["code"], range_="3mo")
                factor, split_note = split_factor(c["base_date"], fresh.attrs.get("splits", []))
                if split_note:
                    p0 = c["base_close"] / factor
                    bars = [{"date": r_["date"], "high": r_["high"], "low": r_["low"]}
                            for r_ in fresh.to_dict("records") if r_["date"] in set(wdates)]
                    counts["split_adjusted"] += 1
                elif fresh.empty:
                    raise RuntimeError("日足が空")
            except Exception as e:  # 分割の有無が分からない → 判定を確定しない
                split_ok = False
                split_note = f"分割の確認に失敗: {type(e).__name__}"
                counts["split_check_failed"] += 1
        r = evaluate(p0, bars, wdates)
        status = status_of(r, split_ok)
        final = r["final"] and split_ok
        with db.cursor() as conn:
            conn.execute(
                """INSERT INTO outcomes(candidate_id,bars_tracked,max_high,max_ret,min_low,min_ret,hit,
                                        hit_day,final,last_date,status,split_note,deadline,
                                        missing_dates,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now())
                   ON CONFLICT(candidate_id) DO UPDATE SET
                     bars_tracked=excluded.bars_tracked, max_high=excluded.max_high,
                     max_ret=excluded.max_ret, min_low=excluded.min_low, min_ret=excluded.min_ret,
                     hit=excluded.hit, hit_day=excluded.hit_day, final=excluded.final,
                     last_date=excluded.last_date, status=excluded.status,
                     split_note=excluded.split_note, deadline=excluded.deadline,
                     missing_dates=excluded.missing_dates, updated_at=now()""",
                (c["id"], r["days_elapsed"], r["max_high"], r["max_ret"], r["min_low"],
                 r["min_ret"], r["hit"] and split_ok, r["hit_day"] if split_ok else None, final,
                 r["last_date"], status, split_note, r["deadline"], r["missing_dates"]))
        counts["tracked"] += 1
    return counts
