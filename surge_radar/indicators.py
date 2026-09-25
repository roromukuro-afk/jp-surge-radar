"""
テクニカル指標・チャート/出来高分析のプリミティブ。

ここは「パターン名を並べる」のではなく、形が示す意味(下落止まり/横ばい化/安値切り上げ/
売り枯れ/人気離散/高値圏天井 等)を数値特徴として算出する層。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=max(2, n // 2)).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def rsi_dev_last(close: pd.Series, n_rsi: int = 14, n_ma: int = 25) -> tuple[float, float]:
    """最終バーの rsi14 と dev25 だけを安価に計算する。

    compute_indicators は11列を全系列に付与するが、build_features が使うのは
    最終行の rsi14/dev25 のみ。predict を全銘柄で回すとこの無駄が効くため分離。
    値は compute_indicators(...).iloc[-1] と一致する。
    """
    c = np.asarray(close.values if hasattr(close, "values") else close, dtype=float)
    rsi_v = 50.0
    if len(c) >= n_rsi + 1:
        d = np.diff(c[-(n_rsi + 1):])
        up = d[d > 0].sum() / n_rsi
        dn = (-d[d < 0]).sum() / n_rsi
        if dn == 0:
            rsi_v = 100.0 if up > 0 else 50.0
        else:
            rs = up / dn
            rsi_v = 100.0 - 100.0 / (1.0 + rs)
    dev_v = 0.0
    if len(c) >= 12:
        ma = c[-n_ma:].mean()
        if ma:
            dev_v = (c[-1] - ma) / ma
    return float(rsi_v), float(dev_v)


def slope(series: pd.Series, n: int = 10) -> float:
    """直近n本の線形回帰の傾き（価格に対する%/日）。"""
    s = series.dropna().tail(n)
    if len(s) < 3:
        return 0.0
    x = np.arange(len(s))
    a = np.polyfit(x, s.values, 1)[0]
    base = s.mean()
    return float(a / base) if base else 0.0


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """日足DF(date,open,high,low,close,volume,turnover)に指標列を付与。

    既に指標列がある DataFrame を渡された場合は再計算しない(冪等)。
    build_features は chart/volume 両方からこれを呼ぶため、1回計算して使い回す。
    """
    if "ma25" in df.columns and "rsi14" in df.columns and "tvma25" in df.columns:
        return df
    df = df.copy().reset_index(drop=True)
    c, v = df["close"], df["volume"]
    df["ma5"] = sma(c, 5)
    df["ma25"] = sma(c, 25)
    df["ma75"] = sma(c, 75)
    df["ma200"] = sma(c, 200)
    df["dev25"] = (c - df["ma25"]) / df["ma25"]            # 25日線乖離率
    df["vma5"] = sma(v, 5)
    df["vma25"] = sma(v, 25)
    df["rsi14"] = rsi(c, 14)
    df["atr14"] = atr(df, 14)
    df["ret1"] = c.pct_change()
    df["tvma25"] = sma(df["turnover"], 25)                  # 平均売買代金
    return df


def _resistance_support(df: pd.DataFrame, lookback: int = 60) -> tuple[float, float]:
    w = df.tail(lookback)
    return float(w["high"].max()), float(w["low"].min())


def supply_zone_features(df: pd.DataFrame, lookback: int = 250,
                         target: float = 0.20) -> dict:
    """
    +20%到達までの「上値供給(しこり)」と Failure Line を数値化する。

    従来の dist_to_resistance は直近60本の高値までの距離しか見ておらず、
    「その上にどれだけ戻り待ちの売りが溜まっているか」を評価していなかった。
    過去にその価格帯で大量に売買されていれば、そこは戻り売り圧力になる。

    ティックデータが無いため、各日の代表価格を (high+low+close)/3 とみなして
    出来高をその価格に割り当てる近似 (volume-at-price) を使う。

    返す値:
      overhead_supply_days : 現値〜+20%帯の累積出来高 ÷ 直近20日平均出来高。
                             「何日分の出来高を消化すれば+20%に届くか」の目安。
      overhead_ratio       : 上値帯の出来高 / (上値帯 + 下値帯)。0.5超で上値が重い。
      failure_distance     : 直近10本の安値(構造的な支持)までの下落率。
      reward_failure_ratio : target ÷ failure_distance。
    """
    out = {"overhead_supply_days": 0.0, "overhead_ratio": 0.5,
           "failure_distance": 0.0, "reward_failure_ratio": 0.0}
    if df is None or df.empty:
        return out
    d = df.tail(lookback)
    close = float(d["close"].iloc[-1])
    if close <= 0:
        return out
    typ = (d["high"] + d["low"] + d["close"]) / 3.0
    vol = d["volume"].astype(float)
    hi_band = (typ > close) & (typ <= close * (1 + target))
    lo_band = (typ < close) & (typ >= close * (1 - target))
    overhead = float(vol[hi_band].sum())
    below = float(vol[lo_band].sum())
    avg_vol = float(d["volume"].tail(20).mean())
    if avg_vol > 0:
        out["overhead_supply_days"] = overhead / avg_vol
    if overhead + below > 0:
        out["overhead_ratio"] = overhead / (overhead + below)
    tail = d.tail(10)
    swing_idx = tail["low"].idxmin()
    swing_low = float(tail["low"].loc[swing_idx])
    fd = (close - swing_low) / close
    out["_failure_line_price"] = swing_low
    out["_failure_line_date"] = str(tail["date"].loc[swing_idx]) if "date" in tail.columns else ""
    out["failure_distance"] = float(max(fd, 0.0))
    out["reward_failure_ratio"] = float(target / fd) if fd > 0.005 else 0.0
    return out


def chart_features(df: pd.DataFrame) -> dict:
    """
    チャート構造の特徴量を辞書で返す。理想形(下落止まり→横ばい→安値切り上げ→ブレイク)に
    近いほど chart_score が高くなるよう、構成要素を 0..1 で数値化。
    """
    d = compute_indicators(df)
    n = len(d)
    last = d.iloc[-1]
    c = d["close"]
    out: dict = {}

    # --- トレンド ---
    out["ma5_slope"] = slope(d["ma5"], 10)
    out["ma25_slope"] = slope(d["ma25"], 20)
    out["ma75_slope"] = slope(d["ma75"], 40)
    out["price_vs_ma25"] = float(last["dev25"]) if pd.notna(last["dev25"]) else 0.0
    out["price_above_ma5"] = int(last["close"] >= last["ma5"]) if pd.notna(last["ma5"]) else 0
    out["price_above_ma25"] = int(last["close"] >= last["ma25"]) if pd.notna(last["ma25"]) else 0
    out["price_above_ma75"] = int(last["close"] >= last["ma75"]) if pd.notna(last["ma75"]) else 0

    # レンジ/トレンド判定: 直近25本の高安レンジ幅 vs ATR
    w25 = d.tail(25)
    rng = (w25["high"].max() - w25["low"].min())
    out["range_ratio"] = float(rng / last["close"]) if last["close"] else 0.0

    # 1日あたりの値幅(直近20本平均)。range_ratio が「25本の高安の幅」なのに対し、
    # こちらは「1日でどれだけ動く銘柄か」。20営業日で+20%動けるかどうかの
    # 物理的な適性そのもので、2026-09-17の満期4798件の実測では単独で最強クラスの
    # 予測力を持つ(<2%:1.9% / 2-3%:8.1% / 3-4%:11.0% / 4-6%:21.5% / 6%超:45.5%、
    # 判定カテゴリでも52週高値距離でも条件付けして単調性が保たれる)。
    w20 = d.tail(20)
    dr = ((w20["high"] - w20["low"]) / w20["close"]).mean()
    out["daily_range_20"] = float(dr) if pd.notna(dr) else 0.0

    # --- 下落止まり / 横ばい化 ---
    # 直近のドローダウンが止まり、ボラが縮小しているか
    recent_lows = d["low"].tail(20)
    prior_lows = d["low"].tail(40).head(20)
    out["downtrend_stopped"] = float(
        np.clip((recent_lows.min() - prior_lows.min()) / (prior_lows.min() + 1e-9), -1, 1)
    )
    vol_recent = c.pct_change().tail(10).std()
    vol_prior = c.pct_change().tail(30).head(20).std()
    out["volatility_contraction"] = float(np.clip(1 - (vol_recent / (vol_prior + 1e-9)), -1, 1)) if vol_prior else 0.0
    # 横ばい化: 直近10本のMA25傾きがほぼ0 & レンジ内
    out["sideways"] = float(1.0 - min(abs(out["ma25_slope"]) / 0.01, 1.0))

    # --- 安値切り上げ / 高値切り下げ停止 ---
    lows = d["low"].tail(30).values
    out["higher_lows"] = _trend_of_extrema(lows, kind="low")
    highs = d["high"].tail(30).values
    out["lower_highs_stopped"] = float(np.clip(-_trend_of_extrema(highs, kind="high"), -1, 1))

    # --- 抵抗線・支持線・ブレイク ---
    res, sup = _resistance_support(d, 60)
    # 実価格も返す(表示用。FEATURE_KEYS には入れないのでモデルには影響しない)
    out["_resistance_price"] = float(res)
    out["_support_price"] = float(sup)
    out["dist_to_resistance"] = float((res - last["close"]) / last["close"]) if last["close"] else 0.0
    out["dist_to_support"] = float((last["close"] - sup) / last["close"]) if last["close"] else 0.0
    # ブレイクライン接近: 抵抗線まで5%以内
    out["near_breakout"] = float(1.0 - min(max(out["dist_to_resistance"], 0) / 0.05, 1.0))
    out["broke_resistance"] = int(last["close"] >= res * 0.999)

    # --- 窓 ---
    out["gap_up"] = int(last["low"] > d["high"].iloc[-2]) if n >= 2 else 0

    # --- 高値圏下落トラップ / 戻り売り / 右肩下がり (リスク) ---
    # 52週高値からの位置
    hi52 = d["high"].tail(min(n, 250)).max()
    out["pct_from_52w_high"] = float((last["close"] - hi52) / hi52) if hi52 else 0.0
    # 右肩下がり: MA25もMA75も下向き & 価格が両者の下
    out["downtrend_risk"] = float(
        ((out["ma25_slope"] < 0) + (out["ma75_slope"] < 0) +
         (1 - out["price_above_ma25"]) + (1 - out["price_above_ma75"])) / 4.0
    )
    # 戻り売り: MA25/75の下で反発が叩かれている
    out["rebound_capped"] = int(
        (last["close"] < last["ma25"]) and (last["ma25"] < last["ma75"])
        if pd.notna(last["ma25"]) and pd.notna(last["ma75"]) else 0
    )
    # 高値圏上ヒゲ(直近)
    body = abs(last["close"] - last["open"])
    upper_wick = last["high"] - max(last["close"], last["open"])
    out["upper_wick_ratio"] = float(upper_wick / (body + 1e-9))
    out["high_zone_upper_wick"] = int(out["upper_wick_ratio"] > 2 and out["pct_from_52w_high"] > -0.1)

    return out


def unfilled_limit_up(df: pd.DataFrame) -> float:
    """買いが約定しきらないまま高値で引けた日か (0/1)。

    ストップ高に張り付くと買い注文が約定できずに板に残り、その需要が翌日以降へ
    持ち越される。条件は「前日比+10%以上」「高値引け」「出来高が直近5日平均の
    半分未満」の同時成立。出来高が細いことが要件なのが肝で、大商いで上げ切った
    日とは意味が逆になる。

    2026-09-25 実測 (2024-06以降・終値3000円以下・翌20営業日の高値が基準比+20%):
      全体                       9.2% (n=1,435,303)
      前日比+10%以上             39.1% (n=8,927)
      +10%かつ高値引け           49.3% (n=3,522)
      +10%かつ高値引けかつ薄商い 67.8% (n=425)   ← この関数が拾う条件
      +10%かつ高値引けかつ大商い 46.7% (n=3,097)
    出来高の条件だけで 46.7% → 67.8% と21ポイント動く。
    """
    if df is None or len(df) < 7:
        return 0.0
    d = df.tail(7)
    last = d.iloc[-1]
    prev_close = float(d.iloc[-2]["close"])
    close, high, vol = float(last["close"]), float(last["high"]), float(last["volume"])
    v5 = float(d["volume"].iloc[-6:-1].mean())
    if prev_close <= 0 or high <= 0 or v5 <= 0:
        return 0.0
    return float(close >= prev_close * 1.10 and close >= high * 0.999 and vol < 0.5 * v5)


def volume_features(df: pd.DataFrame) -> dict:
    """
    出来高分析。増減だけでなく「どの価格位置で増えたか」「初動か天井か」を数値化。
    売り枯れ = 出来高減少+価格維持 / 人気離散 = 出来高減少+価格下落。
    """
    d = compute_indicators(df)
    last = d.iloc[-1]
    out: dict = {}

    vma5 = last["vma5"]; vma25 = last["vma25"]
    out["vol_ratio_5_25"] = float(vma5 / vma25) if vma25 else 1.0
    out["vol_spike"] = float(last["volume"] / vma25) if vma25 else 1.0
    out["turnover_avg"] = float(last["tvma25"]) if pd.notna(last["tvma25"]) else 0.0

    # 上昇日に出来高が増えたか / 下落日にだけ増えていないか
    d10 = d.tail(10)
    up_vol = d10.loc[d10["ret1"] > 0, "volume"].mean()
    dn_vol = d10.loc[d10["ret1"] < 0, "volume"].mean()
    out["up_down_vol_bias"] = float(np.clip((up_vol - dn_vol) / (up_vol + dn_vol + 1e-9), -1, 1)) \
        if (pd.notna(up_vol) and pd.notna(dn_vol)) else 0.0

    # 出来高急増後に価格を維持したか (直近の出来高ピーク日以降の値持ち)
    d20 = d.tail(20).reset_index(drop=True)
    if len(d20) >= 5:
        peak_i = int(d20["volume"].idxmax())
        peak_close = d20.loc[peak_i, "close"]
        after = d20.loc[peak_i:, "close"]
        out["held_after_vol_spike"] = float(after.min() / peak_close - 1) if peak_close else 0.0
        # 高値圏の天井大商いか: 出来高ピークが期間高値圏 & その後下落
        out["volume_top_risk"] = int(
            (d20.loc[peak_i, "high"] >= d20["high"].max() * 0.98)
            and (last["close"] < peak_close)
        )
    else:
        out["held_after_vol_spike"] = 0.0
        out["volume_top_risk"] = 0

    # 売り枯れ / 人気離散
    vol_declining = out["vol_ratio_5_25"] < 0.8
    price_5d = d["close"].tail(6)
    price_change_5d = float(price_5d.iloc[-1] / price_5d.iloc[0] - 1) if len(price_5d) >= 2 else 0.0
    out["price_change_5d"] = price_change_5d
    out["dry_up"] = int(vol_declining and price_change_5d >= -0.02)        # 売り枯れ(良い)
    out["popularity_loss"] = int(vol_declining and price_change_5d < -0.04)  # 人気離散(悪い)

    return out


def _trend_of_extrema(arr: np.ndarray, kind: str = "low", k: int = 3) -> float:
    """極値(安値/高値)が切り上がっている度合いを -1..1 で返す。"""
    arr = np.asarray(arr, dtype=float)
    arr = arr[~np.isnan(arr)]
    if len(arr) < 6:
        return 0.0
    half = len(arr) // 2
    if kind == "low":
        first = arr[:half].min()
        second = arr[half:].min()
    else:
        first = arr[:half].max()
        second = arr[half:].max()
    return float(np.clip((second - first) / (abs(first) + 1e-9), -1, 1))
