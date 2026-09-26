"""
3000 円以下の全銘柄について、基準日の特徴量とチャート・出来高ラベルを計算する。

ラベルの定義は docs/LABELS.md と一致させること。定義は事前に決めて固定したもので、
過去データへの当てはめで決めていない。変えるときは LABEL_VERSION を上げ、
docs/LABELS.md の変更履歴に書く。
"""
from __future__ import annotations

import math

from . import db

LABEL_VERSION = "chart-v1"
PRICE_CAP = 3000.0
W2 = 10   # 直近 2 週 = 当日を除く直前 10 営業日
W1M = 20  # 直近 1 か月 = 当日を除く直前 20 営業日

# 東証の制限値幅表 (前日終値の上限, 値幅)
_LIMITS = [(100, 30), (200, 50), (500, 80), (700, 100), (1000, 150), (1500, 300),
           (2000, 400), (3000, 500), (5000, 700), (7000, 1000), (10000, 1500)]


def price_limit(prev_close: float) -> float:
    for cap, width in _LIMITS:
        if prev_close < cap:
            return float(width)
    return 3000.0


def compute(bars: list[dict]) -> tuple[dict, list[str]] | None:
    """bars: 日付昇順の日足 (最後が基準日)。特徴量とラベルを返す。足が 2 本未満なら None。"""
    bars = [b for b in bars if b.get("close") and b.get("high") and b.get("low")]
    if len(bars) < 2:
        return None
    t = bars[-1]
    prev = bars[:-1]
    pc = prev[-1]["close"]
    close, high, low, opn = t["close"], t["high"], t["low"], t.get("open") or t["close"]
    vol = t.get("volume") or 0.0

    w2 = prev[-W2:]
    w1m = prev[-W1M:] if len(prev) >= W1M else None

    f: dict = {
        "close": close,
        "n_bars": len(bars),
        "ret_1d": close / pc - 1,
    }
    for n in (3, 5, 10):
        if len(bars) > n:
            f[f"ret_{n}d"] = close / bars[-1 - n]["close"] - 1

    hi2 = max(b["high"] for b in w2)
    lo2 = min(b["low"] for b in w2)
    f["high_2w"] = hi2
    f["low_2w"] = lo2
    f["dist_high_2w"] = close / hi2 - 1
    f["box_2w"] = hi2 / lo2 - 1 if lo2 > 0 else None
    if w1m:
        f["high_1m"] = max(b["high"] for b in w1m)
        f["dist_high_1m"] = close / f["high_1m"] - 1

    vols = [b.get("volume") or 0.0 for b in w2]
    vavg = sum(vols) / len(vols) if vols else 0.0
    f["vol_ratio"] = vol / vavg if vavg > 0 else None

    ranges = []
    for i in range(max(1, len(bars) - 1 - W2), len(bars) - 1):
        p = bars[i - 1]["close"]
        if p:
            ranges.append((bars[i]["high"] - bars[i]["low"]) / p)
    f["range_avg_2w"] = sum(ranges) / len(ranges) if ranges else None

    turns = [(b.get("close") or 0) * (b.get("volume") or 0) for b in w2]
    f["turnover_avg_2w"] = sum(turns) / len(turns) if turns else None
    f["turnover"] = close * vol

    lim = price_limit(pc)
    f["limit_price"] = pc + lim
    f["gap"] = opn / pc - 1
    day_range = high - low
    f["upper_wick_ratio"] = (high - max(opn, close)) / day_range if day_range > 0 else 0.0

    streak = 0
    for i in range(len(bars) - 1, 0, -1):
        if bars[i]["close"] > bars[i - 1]["close"]:
            streak += 1
        else:
            break
    f["up_streak"] = streak

    labels: list[str] = []
    if close > hi2:
        labels.append("ブレイク2週")
    if w1m and close > f["high_1m"]:
        labels.append("ブレイク1か月")
    if -0.03 <= f["dist_high_2w"] <= 0:
        labels.append("2週高値圏")
    if -0.15 <= f["dist_high_2w"] <= -0.05 and close > lo2 * 1.03:
        labels.append("押し目")
    if f["box_2w"] is not None and f["box_2w"] < 0.08:
        labels.append("保ち合い")
    vr = f["vol_ratio"]
    if vr is not None:
        if vr >= 3:
            labels.append("出来高急増")
        elif vr >= 1.5:
            labels.append("出来高増")
        elif vr <= 0.5:
            labels.append("出来高枯れ")
    if close >= pc + lim:
        labels.append("ストップ高引け")
    elif high >= pc + lim:
        labels.append("ストップ高タッチ")
    if close <= pc - lim:
        labels.append("ストップ安")
    if f["gap"] >= 0.03:
        labels.append("ギャップアップ")
    if f["upper_wick_ratio"] >= 0.5 and day_range >= pc * 0.03:
        labels.append("上ヒゲ長")
    if streak >= 3:
        labels.append("連騰3")
    if f["ret_1d"] >= 0.10:
        labels.append("急騰")
    if f["ret_1d"] <= -0.07:
        labels.append("急落")
    ra = f["range_avg_2w"]
    if ra is not None:
        if ra >= 0.06:
            labels.append("値幅大")
        elif ra < 0.02:
            labels.append("値幅小")
    ta = f["turnover_avg_2w"]
    if ta is not None and ta < 30_000_000:
        labels.append("低流動性")

    f = {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in f.items()}
    return f, labels


def build(base_date: str) -> dict:
    """base_date の終値が 3000 円以下の全銘柄についてスナップショットを作り保存する。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT code,date,open,high,low,close,volume FROM prices "
            "WHERE date <= %s ORDER BY code, date", (base_date,)).fetchall()
    by_code: dict[str, list[dict]] = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)

    out = []
    for code, bars in by_code.items():
        if bars[-1]["date"] != base_date:
            continue  # 基準日の足が無い(売買停止など)
        if not bars[-1]["close"] or bars[-1]["close"] > PRICE_CAP:
            continue
        res = compute(bars[-(W1M + 2):])
        if res is None:
            continue
        f, labels = res
        out.append((base_date, code, f["close"], db.j(f), labels, LABEL_VERSION))

    with db.cursor() as conn:
        conn.executemany(
            """INSERT INTO snapshots(date,code,close,features,labels,label_version)
               VALUES(%s,%s,%s,%s,%s,%s)
               ON CONFLICT(date,code) DO NOTHING""", out)
    return {"base_date": base_date, "stocks": len(out)}
