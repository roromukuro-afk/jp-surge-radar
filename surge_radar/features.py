"""
特徴量アセンブリ。

1銘柄・1時点(T0)について、チャート/出来高/材料/テーマ/ファンダ/流動性 を
1つの数値ベクトルにまとめる。過去(教師データ)でもライブでも同じ関数を使う。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators
from .config import LOOKBACK, MIN_AVG_TURNOVER, MIN_AVG_VOLUME, PRICE_CAP

# モデル/類似度に使う数値特徴のキー順序 (安定させる)
FEATURE_KEYS = [
    # チャート
    "ma5_slope", "ma25_slope", "ma75_slope", "price_vs_ma25",
    "price_above_ma5", "price_above_ma25", "price_above_ma75",
    "range_ratio", "downtrend_stopped", "volatility_contraction", "sideways",
    "higher_lows", "lower_highs_stopped", "dist_to_resistance", "dist_to_support",
    "near_breakout", "broke_resistance", "gap_up", "pct_from_52w_high",
    "downtrend_risk", "rebound_capped", "upper_wick_ratio", "high_zone_upper_wick",
    "rsi14", "dev25",
    # 出来高
    "vol_ratio_5_25", "vol_spike", "up_down_vol_bias", "held_after_vol_spike",
    "volume_top_risk", "dry_up", "popularity_loss", "price_change_5d", "turnover_log",
    # 材料
    "material_raw", "pos_impact", "neg_impact", "has_fresh_material",
    "dilution_flag", "going_concern_flag", "n_materials",
    # テーマ/地合い
    "theme_tailwind", "market_score",
    # 価格水準/流動性
    "price_level_norm", "liquidity_ok",
]


def build_features(df: pd.DataFrame, idx: int | None = None, *,
                   material: dict | None = None,
                   theme_tailwind: float = 0.0,
                   market_score: float = 0.0) -> dict | None:
    """
    df: 昇順の日足。idx: T0 のインデックス(None=最終行)。
    戻り値: {numeric features + meta}。データ不足なら None。
    """
    if df is None or df.empty:
        return None
    if idx is None:
        idx = len(df) - 1
    if idx < 30:  # 最低限の足
        return None
    sub = df.iloc[: idx + 1].copy()
    last = sub.iloc[-1]
    close = float(last["close"])
    if not close or close <= 0:
        return None

    cf = indicators.chart_features(sub)
    vf = indicators.volume_features(sub)

    feats: dict = {}
    feats.update({k: cf.get(k, 0.0) for k in [
        "ma5_slope", "ma25_slope", "ma75_slope", "price_vs_ma25",
        "price_above_ma5", "price_above_ma25", "price_above_ma75",
        "range_ratio", "downtrend_stopped", "volatility_contraction", "sideways",
        "higher_lows", "lower_highs_stopped", "dist_to_resistance", "dist_to_support",
        "near_breakout", "broke_resistance", "gap_up", "pct_from_52w_high",
        "downtrend_risk", "rebound_capped", "upper_wick_ratio", "high_zone_upper_wick",
    ]})
    di = indicators.compute_indicators(sub).iloc[-1]
    feats["rsi14"] = float(di["rsi14"]) if pd.notna(di["rsi14"]) else 50.0
    feats["dev25"] = float(di["dev25"]) if pd.notna(di["dev25"]) else 0.0

    feats.update({k: vf.get(k, 0.0) for k in [
        "vol_ratio_5_25", "vol_spike", "up_down_vol_bias", "held_after_vol_spike",
        "volume_top_risk", "dry_up", "popularity_loss", "price_change_5d",
    ]})
    turnover_avg = vf.get("turnover_avg", 0.0) or 0.0
    feats["turnover_log"] = float(np.log10(turnover_avg + 1))

    # 材料
    m = material or {}
    feats["material_raw"] = float(m.get("material_raw", 0.0))
    feats["pos_impact"] = float(m.get("pos_impact", 0.0))
    feats["neg_impact"] = float(m.get("neg_impact", 0.0))
    feats["has_fresh_material"] = int(m.get("has_fresh_material", 0))
    feats["dilution_flag"] = int(m.get("dilution_flag", 0))
    feats["going_concern_flag"] = int(m.get("going_concern_flag", 0))
    feats["n_materials"] = int(m.get("n_materials", 0))

    # テーマ/地合い
    feats["theme_tailwind"] = float(theme_tailwind)
    feats["market_score"] = float(market_score)

    # 価格水準・流動性
    feats["price_level_norm"] = float(min(close / PRICE_CAP, 2.0))
    feats["liquidity_ok"] = int(turnover_avg >= MIN_AVG_TURNOVER)

    # メタ(非特徴)
    feats["_close"] = close
    feats["_date"] = str(last["date"])
    feats["_turnover_avg"] = turnover_avg
    feats["_idx"] = idx
    return feats


def to_vector(feats: dict) -> list[float]:
    """FEATURE_KEYS の順に数値ベクトル化 (モデル/類似度用)。"""
    return [float(feats.get(k, 0.0)) for k in FEATURE_KEYS]


def price_in_range(feats: dict) -> bool:
    return feats.get("_close", 1e9) <= PRICE_CAP
