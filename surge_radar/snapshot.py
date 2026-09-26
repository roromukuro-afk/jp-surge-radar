"""
3000 円以下の全銘柄について、基準日の特徴量(数値)とチャート・出来高ラベルを計算する。

ラベルの定義は docs/LABELS.md と一致させること。定義は事前に決めて固定したもので、
過去データへの当てはめで決めていない。ラベル名は観測した形を表す言葉だけを使い、
良い・悪い・健全などの評価語は使わない。変えるときは LABEL_VERSION を上げ、
docs/LABELS.md の変更履歴に書く。

「直前 N 日」は基準日を含まない直前 N 営業日。足が足りない指標は計算せず(None)、
対応するラベルも付けない(不明を「該当なし」と区別するため、features に None を残す)。
"""
from __future__ import annotations

import math

from . import db

LABEL_VERSION = "chart-v2"
PRICE_CAP = 3000.0
LOOKBACK_BARS = 26  # 基準日 + 直前 25 営業日

# 東証の制限値幅表 (前日終値の上限, 値幅)
_LIMITS = [(100, 30), (200, 50), (500, 80), (700, 100), (1000, 150), (1500, 300),
           (2000, 400), (3000, 500), (5000, 700), (7000, 1000), (10000, 1500)]


def price_limit(prev_close: float) -> float:
    for cap, width in _LIMITS:
        if prev_close < cap:
            return float(width)
    return 3000.0


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _ratio(a, b):
    return a / b if (a is not None and b not in (None, 0)) else None


def compute(bars: list[dict]) -> tuple[dict, list[str]] | None:
    """bars: 日付昇順の日足(最後が基準日)。特徴量とラベルを返す。足が 2 本未満なら None。"""
    bars = [b for b in bars if b.get("close") and b.get("high") and b.get("low")]
    if len(bars) < 2:
        return None
    t, y = bars[-1], bars[-2]
    prior = bars[:-1]                      # 基準日を含まない
    n = len(bars)
    c, h, lo = t["close"], t["high"], t["low"]
    o = t.get("open") or c
    pc = y["close"]
    vol = t.get("volume") or 0.0

    def prior_n(k):
        return prior[-k:] if len(prior) >= k else None

    f: dict = {"close": c, "n_bars": n}

    # --- 騰落 ---
    for k in (1, 3, 5, 10, 20):
        f[f"ret_{k}d"] = c / bars[-1 - k]["close"] - 1 if n > k else None

    # --- 高値・安値への距離と更新 (直前 N 日) ---
    for k in (5, 10, 20):
        w = prior_n(k)
        if w:
            hi_k = max(b["high"] for b in w)
            lo_k = min(b["low"] for b in w)
            f[f"high_{k}"], f[f"low_{k}"] = hi_k, lo_k
            f[f"dist_high_{k}"] = c / hi_k - 1
            f[f"dist_low_{k}"] = c / lo_k - 1
            f[f"new_high_{k}"] = h > hi_k
            f[f"new_low_{k}"] = lo < lo_k
        else:
            for key in ("high", "low", "dist_high", "dist_low", "new_high", "new_low"):
                f[f"{key}_{k}"] = None

    # --- 移動平均(終値の単純平均、基準日を含む) ---
    closes = [b["close"] for b in bars]
    f["ma5"] = _mean(closes[-5:]) if n >= 5 else None
    f["ma20"] = _mean(closes[-20:]) if n >= 20 else None
    f["ma20_5ago"] = _mean(closes[-25:-5]) if n >= 25 else None
    f["ma20_slope5"] = _ratio(f["ma20"], f["ma20_5ago"]) - 1 if f["ma20_5ago"] else None
    f["ma20_prev"] = _mean(closes[-21:-1]) if n >= 21 else None

    # --- 値幅・ATR (前日終値比) ---
    trs, rngs = [], []
    for i in range(1, n):
        p = bars[i - 1]["close"]
        b = bars[i]
        trs.append(max(b["high"], p) - min(b["low"], p))
        rngs.append((b["high"] - b["low"]) / p if p else None)
    f["range_1d"] = rngs[-1]
    f["range_avg5"] = _mean(rngs[-5:]) if len(rngs) >= 5 else None
    f["range_avg20"] = _mean(rngs[-20:]) if len(rngs) >= 20 else None
    f["atr14"] = _mean(trs[-14:]) if len(trs) >= 14 else None
    f["atr14_pct"] = _ratio(f["atr14"], c)

    # --- 出来高・売買代金(売買代金は 終値×出来高 の近似) ---
    vols = [b.get("volume") or 0.0 for b in bars]
    f["volume"] = vol
    f["vol_avg5"] = _mean(vols[-6:-1]) if n >= 6 else None
    f["vol_avg20"] = _mean(vols[-21:-1]) if n >= 21 else None
    f["vol_ratio5"] = _ratio(vol, f["vol_avg5"])
    f["vol_ratio20"] = _ratio(vol, f["vol_avg20"])
    f["vol_recent5_avg"] = _mean(vols[-5:]) if n >= 5 else None          # 基準日を含む直近 5 日
    f["vol_base20_avg"] = _mean(vols[-25:-5]) if n >= 25 else None       # その前の 20 日
    f["vol_trend"] = _ratio(f["vol_recent5_avg"], f["vol_base20_avg"])
    turns = [(b.get("close") or 0) * (b.get("volume") or 0) for b in bars]
    f["turnover_approx"] = turns[-1]
    f["turnover_avg5"] = _mean(turns[-6:-1]) if n >= 6 else None
    f["turnover_avg20"] = _mean(turns[-21:-1]) if n >= 21 else None

    # --- ローソク足(前日終値比) ---
    body = c - o
    rng = h - lo
    f["body_pct"] = body / pc
    f["upper_wick_pct"] = (h - max(o, c)) / pc
    f["lower_wick_pct"] = (min(o, c) - lo) / pc
    f["gap_pct"] = o / pc - 1

    def run_len(pred):
        k = 0
        for i in range(n - 1, -1, -1):
            if pred(i):
                k += 1
            else:
                break
        return k
    f["bull_run"] = run_len(lambda i: bars[i]["close"] > (bars[i].get("open") or bars[i]["close"]))
    f["bear_run"] = run_len(lambda i: bars[i]["close"] < (bars[i].get("open") or bars[i]["close"]))

    lim = price_limit(pc)
    f["limit_up_price"] = pc + lim

    # ======== ラベル ========
    L: list[str] = []

    # A. 高値・安値構造: 直近 5 日(基準日含む)と、その前の 5 日の高値・安値を比べる。±1% 以内は横ばい
    if n >= 10:
        h_now = max(b["high"] for b in bars[-5:]); h_prev = max(b["high"] for b in bars[-10:-5])
        l_now = min(b["low"] for b in bars[-5:]); l_prev = min(b["low"] for b in bars[-10:-5])
        f["hh_ratio"], f["ll_ratio"] = h_now / h_prev - 1, l_now / l_prev - 1
        L.append("高値切り上げ" if f["hh_ratio"] > 0.01 else "高値切り下げ" if f["hh_ratio"] < -0.01 else "高値横ばい")
        L.append("安値切り上げ" if f["ll_ratio"] > 0.01 else "安値切り下げ" if f["ll_ratio"] < -0.01 else "安値横ばい")

    # B. 基本トレンド: 20 日線の 5 日間の変化率と、終値の位置
    s = f["ma20_slope5"]
    if s is not None:
        if s > 0.02 and c > f["ma20"]:
            L.append("上昇トレンド")
        elif s < -0.02 and c < f["ma20"]:
            L.append("下降トレンド")
        elif abs(s) <= 0.02:
            L.append("横ばい")

    # F. ローソク足
    if body > 0:
        L.append("大陽線" if f["body_pct"] >= 0.05 else "中陽線" if f["body_pct"] >= 0.02 else "小陽線")
    elif body < 0:
        L.append("大陰線" if f["body_pct"] <= -0.05 else "中陰線" if f["body_pct"] <= -0.02 else "小陰線")
    if rng > 0 and abs(body) <= 0.1 * rng:
        L.append("十字線")
    if rng >= pc * 0.03:
        if (h - max(o, c)) >= 0.5 * rng:
            L.append("長い上ヒゲ")
        if (min(o, c) - lo) >= 0.5 * rng:
            L.append("長い下ヒゲ")
    if c >= h * 0.999:
        L.append("高値引け")
    if c <= lo * 1.001:
        L.append("安値引け")
    if lo > y["high"]:
        L.append("窓開け上昇")
    if h < y["low"]:
        L.append("窓開け下落")
    for k, name in ((4, "陽線4本以上連続"), (3, "陽線3本連続"), (2, "陽線2本連続")):
        if f["bull_run"] >= k:
            L.append(name)
            break
    for k, name in ((4, "陰線4本以上連続"), (3, "陰線3本連続"), (2, "陰線2本連続")):
        if f["bear_run"] >= k:
            L.append(name)
            break
    yo = y.get("open") or y["close"]
    if body > 0 and y["close"] < yo and o <= y["close"] and c >= yo:
        L.append("陽の包み足")
    if body < 0 and y["close"] > yo and o >= y["close"] and c <= yo:
        L.append("陰の包み足")
    if h < y["high"] and lo > y["low"]:
        L.append("はらみ足")

    # G. 高値・安値への距離(接近 = 直前 N 日高値の -3%〜0%、更新 = 基準日の高値が上回った)
    for k in (5, 10, 20):
        if f[f"new_high_{k}"] is None:
            continue
        if f[f"new_high_{k}"]:
            L.append(f"{k}日高値更新")
        elif -0.03 <= f[f"dist_high_{k}"] <= 0:
            L.append(f"{k}日高値接近")
        if f[f"new_low_{k}"]:
            L.append(f"{k}日安値更新")
        elif 0 <= f[f"dist_low_{k}"] <= 0.03:
            L.append(f"{k}日安値接近")
    if f["dist_high_10"] is not None and -0.15 <= f["dist_high_10"] <= -0.05:
        L.append("10日高値から5〜15%下")

    # H. 移動平均
    if f["ma5"] is not None:
        L.append("5日線上" if c > f["ma5"] else "5日線下")
    if f["ma20"] is not None:
        L.append("20日線上" if c > f["ma20"] else "20日線下")
        if f["ma20_prev"] is not None:
            if pc <= f["ma20_prev"] and c > f["ma20"]:
                L.append("20日線回復")
            if pc >= f["ma20_prev"] and c < f["ma20"]:
                L.append("20日線割れ")
        if s is not None:
            L.append("20日線上向き" if s > 0.01 else "20日線下向き" if s < -0.01 else "20日線横ばい")
        if f["ma5"] is not None and abs(f["ma5"] / f["ma20"] - 1) <= 0.01:
            L.append("5日線20日線収束")

    # J. 値幅
    if f["range_avg5"] is not None and f["range_avg20"]:
        rr = f["range_avg5"] / f["range_avg20"]
        f["range_ratio_5_20"] = rr
        if rr <= 0.7:
            L.append("値幅収縮")
        elif rr >= 1.3:
            L.append("値幅拡大")

    # 出来高(直前 20 日平均比)と価格との関係
    v20 = f["vol_ratio20"]
    if v20 is not None:
        if v20 >= 3:
            L.append("出来高急増")
        elif v20 >= 1.5:
            L.append("出来高増加")
        elif v20 <= 0.5:
            L.append("出来高減少")
        r1 = f["ret_1d"]
        if v20 >= 1.5:
            L.append("価格上昇＋出来高増加" if r1 >= 0.01 else "価格下落＋出来高増加" if r1 <= -0.01 else "価格横ばい＋出来高増加")
        elif v20 <= 0.7:
            L.append("価格上昇＋出来高減少" if r1 >= 0.01 else "価格下落＋出来高減少" if r1 <= -0.01 else "価格横ばい＋出来高減少")
    vt = f["vol_trend"]
    if vt is not None:
        spike = f["vol_base20_avg"] and max(vols[-5:]) >= 3 * f["vol_base20_avg"]
        if vt >= 1.3 and not spike:
            L.append("出来高漸増")
        elif vt <= 0.6:
            L.append("出来高収縮")
    ta5, ta20 = f["turnover_avg5"], f["turnover_avg20"]
    if ta20 is not None and ta20 < 30_000_000:
        L.append("20日平均売買代金3000万円未満")
    if ta5 is not None and ta20 and ta5 >= 2 * ta20:
        L.append("売買代金レジーム上昇")

    # 値幅制限・急変
    if c >= pc + lim:
        L.append("ストップ高引け")
    elif h >= pc + lim:
        L.append("ストップ高タッチ")
    if c <= pc - lim:
        L.append("ストップ安")
    if f["ret_1d"] >= 0.10:
        L.append("1日+10%以上")
    if f["ret_1d"] <= -0.07:
        L.append("1日-7%以下")
    if f["ret_5d"] is not None and f["ret_5d"] >= 0.30:
        L.append("5日+30%以上")

    f = {k: (None if isinstance(v, float) and not math.isfinite(v) else v) for k, v in f.items()}
    return f, L


def build(base_date: str, upgrade: bool = False) -> dict:
    """base_date の終値が 3000 円以下の全銘柄についてスナップショットを作り保存する。

    既存の行は変えない(ラベルは後から付け直さない)。upgrade=True のときだけ、
    ラベル定義の版が今と違う行を今の版で置き換える(候補が 1 件も無い段階での
    定義変更用。候補を記録した日のスナップショットには使わないこと)。
    """
    with db.cursor() as conn:
        if upgrade and conn.execute("SELECT 1 FROM candidates WHERE base_date=%s LIMIT 1",
                                    (base_date,)).fetchone():
            raise RuntimeError(f"{base_date} は候補を記録済み。ラベルを付け直さない")
        rows = conn.execute(
            "SELECT code,date,open,high,low,close,volume FROM prices "
            "WHERE date <= %s ORDER BY code, date", (base_date,)).fetchall()
    by_code: dict[str, list[dict]] = {}
    for r in rows:
        by_code.setdefault(r["code"], []).append(r)

    out = []
    for code, bars in by_code.items():
        last = bars[-1]
        if last["date"] != base_date or not last["close"] or last["close"] > PRICE_CAP:
            continue
        if not last.get("volume"):
            continue  # 基準日に売買が成立していない
        res = compute(bars[-LOOKBACK_BARS:])
        if res is None:
            continue
        f, labels = res
        out.append((base_date, code, f["close"], db.j(f), labels, LABEL_VERSION))

    conflict = ("ON CONFLICT(date,code) DO UPDATE SET close=excluded.close, features=excluded.features, "
                "labels=excluded.labels, label_version=excluded.label_version, created_at=now() "
                "WHERE snapshots.label_version <> excluded.label_version") if upgrade \
        else "ON CONFLICT(date,code) DO NOTHING"
    with db.cursor() as conn:
        conn.executemany(
            f"INSERT INTO snapshots(date,code,close,features,labels,label_version) "
            f"VALUES(%s,%s,%s,%s,%s,%s) {conflict}", out)
    return {"base_date": base_date, "stocks": len(out), "label_version": LABEL_VERSION}
