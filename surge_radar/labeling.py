"""
成否判定 / ラベリング。

予測時点(T0)価格を基準に、20営業日以内の高値ベース+20%到達で成功。
  S: 5営業日以内 / A: 10営業日以内 / B: 20営業日以内
  near: +10%〜+20%未満 / fail: +10%未満 / danger_fail: 大幅下落や天井掴み
過去急騰の教師データ作成と、ライブ予測の追跡の両方で使う。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import (DANGER_DRAWDOWN, NEAR_MISS_THRESHOLD, SUCCESS_THRESHOLD,
                     WINDOW_A, WINDOW_B, WINDOW_S)


def forward_outcome(df: pd.DataFrame, idx: int, *, base: str = "close") -> dict | None:
    """
    df昇順, idx=T0。T0以降の値動きから成否指標を計算。
    十分な先行きが無い(窓未満)場合は bars_tracked を返しつつ partial フラグ。
    """
    if idx is None or idx >= len(df) - 1:
        return None
    base_price = float(df.iloc[idx]["close"])
    if base_price <= 0:
        return None
    fwd = df.iloc[idx + 1: idx + 1 + WINDOW_B].reset_index(drop=True)
    if fwd.empty:
        return None
    bars = len(fwd)

    def max_up(window: int) -> float:
        w = fwd.iloc[:window]
        if w.empty:
            return 0.0
        return float(w["high"].max() / base_price - 1)

    def close_max_up(window: int) -> float:
        w = fwd.iloc[:window]
        if w.empty:
            return 0.0
        return float(w["close"].max() / base_price - 1)

    up5, up10, up20 = max_up(WINDOW_S), max_up(WINDOW_A), max_up(WINDOW_B)
    cup20 = close_max_up(WINDOW_B)

    # +20%到達日数(高値ベース)
    days_to_20 = None
    for i in range(len(fwd)):
        if fwd.iloc[i]["high"] / base_price - 1 >= SUCCESS_THRESHOLD:
            days_to_20 = i + 1
            break

    max_dd = float(fwd["low"].min() / base_price - 1)

    # 高値到達後に失速したか: 期間高値の後で終値が高値*0.92を割った
    hi_idx = int(fwd["high"].idxmax())
    after_hi = fwd.iloc[hi_idx:]
    peak = fwd.iloc[hi_idx]["high"]
    faded = int(len(after_hi) >= 2 and after_hi["close"].iloc[-1] < peak * 0.92)

    # 終値ベースで上昇維持
    close_up_maintained = int(cup20 >= NEAR_MISS_THRESHOLD and fwd["close"].iloc[-1] / base_price - 1 >= 0)

    return {
        "base_price": base_price,
        "bars_tracked": bars,
        "max_up_5d": round(up5, 4),
        "max_up_10d": round(up10, 4),
        "max_up_20d": round(up20, 4),
        "close_max_up_20d": round(cup20, 4),
        "days_to_20pct": days_to_20,
        "max_drawdown": round(max_dd, 4),
        "faded_after_high": faded,
        "close_up_maintained": close_up_maintained,
        "partial": int(bars < WINDOW_B),
    }


def classify_result(oc: dict) -> tuple[str, list[str]]:
    """outcome から結果クラスと失敗タグを判定。"""
    if oc is None:
        return "open", []
    up20 = oc["max_up_20d"]
    d20 = oc["days_to_20pct"]
    dd = oc["max_drawdown"]
    tags: list[str] = []

    if d20 is not None and d20 <= WINDOW_S:
        return "S", []
    if d20 is not None and d20 <= WINDOW_A:
        return "A", []
    if d20 is not None and d20 <= WINDOW_B:
        return "B", []

    # +20%未達
    if up20 >= NEAR_MISS_THRESHOLD:
        tags.append("near_miss")
        # 大幅下落を伴うなら危険
        if dd <= DANGER_DRAWDOWN:
            tags.append("trap_fail")
            return "danger_fail", tags
        return "near", tags

    # fail/danger_fail の切り分けと失敗理由タグ
    if dd <= DANGER_DRAWDOWN:
        tags.append("quick_fail" if dd <= -0.20 else "trap_fail")
        res = "danger_fail"
    else:
        res = "fail"
    return res, tags


def derive_failure_tags(oc: dict, feats: dict | None, *,
                        material_continued: int | None = None,
                        volume_continued: int | None = None,
                        theme_followed: int | None = None,
                        market_score_now: float | None = None) -> list[str]:
    """
    特徴量と事後の継続状況から、より具体的な失敗タグを付与。
    一律に扱わず、材料/チャート/出来高/テーマ/地合い/希薄化/流動性の観点で分類。
    """
    tags: list[str] = []
    if oc is None:
        return tags
    f = feats or {}

    if oc["max_drawdown"] <= -0.10 and (oc["days_to_20pct"] is None):
        if oc["max_up_5d"] < 0.02:
            tags.append("quick_fail")
    if material_continued == 0 and f.get("material_raw", 0) > 0.3:
        tags.append("material_fail")
    if f.get("volume_top_risk", 0) or f.get("high_zone_upper_wick", 0):
        tags.append("volume_fail")
    if f.get("downtrend_risk", 0) >= 0.5 or f.get("rebound_capped", 0):
        tags.append("trend_fail")
    if theme_followed == 0 and f.get("theme_tailwind", 0) > 0.4:
        tags.append("theme_fail")
    if market_score_now is not None and market_score_now < -0.3:
        tags.append("market_fail")
    if f.get("dilution_flag", 0):
        tags.append("dilution_fail")
    if f.get("going_concern_flag", 0):
        tags.append("dilution_fail")
    if not f.get("liquidity_ok", 1):
        tags.append("liquidity_fail")
    if f.get("pct_from_52w_high", -1) > -0.05 and oc["max_drawdown"] <= -0.08:
        tags.append("trap_fail")
    # 重複除去
    seen = set(); uniq = []
    for t in tags:
        if t not in seen:
            uniq.append(t); seen.add(t)
    return uniq


def is_success(oc: dict) -> int:
    """+20%到達したか(教師ラベル用)。"""
    if oc is None:
        return 0
    return int(oc.get("days_to_20pct") is not None)
