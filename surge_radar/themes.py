"""
テーマ地合い / 市場レジーム。

テーマタグ一致だけで候補にしない。ETF/指数の値動きで客観的に「テーマに資金が来ているか」
「地合いが急騰を許す環境か」を確認するための層。
"""
from __future__ import annotations

import pandas as pd

from . import db
from .config import MARKET_REGIME_TICKER, THEME_TICKERS
from .indicators import compute_indicators, slope
from .sources import yahoo


def _regime_from_df(df: pd.DataFrame) -> dict:
    if df.empty or len(df) < 30:
        return {"trend": 0.0, "above_ma": 0, "vol_up": 0}
    d = compute_indicators(df)
    last = d.iloc[-1]
    trend = slope(d["ma25"], 20) * 100  # %/日 を見やすく
    above = int((last["close"] >= last["ma25"]) + (last["close"] >= last["ma75"]))  # 0..2
    vma5 = last["vma5"]; vma25 = last["vma25"]
    vol_up = int(bool(vma25) and vma5 > vma25)
    return {"trend": round(float(trend), 4), "above_ma": above, "vol_up": vol_up}


def update_theme_regime(asof: str) -> dict:
    """全テーマETF/指数のレジームを取得しDBに保存。返り値は theme->regime。"""
    out = {}
    for theme, ticker in THEME_TICKERS.items():
        df = yahoo.fetch_ohlcv(ticker, range_="6mo")
        reg = _regime_from_df(df)
        out[theme] = reg
        with db.cursor() as conn:
            conn.execute(
                "INSERT INTO theme_regime(date,theme,trend,above_ma,vol_up,note) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(date,theme) DO UPDATE SET trend=excluded.trend,"
                "above_ma=excluded.above_ma,vol_up=excluded.vol_up",
                (asof, theme, reg["trend"], reg["above_ma"], reg["vol_up"], ticker),
            )
    return out


def market_regime(asof: str) -> dict:
    """地合い (TOPIX ETF) のレジーム。market_fail 判定や全体の慎重度に使う。"""
    df = yahoo.fetch_ohlcv(MARKET_REGIME_TICKER, range_="6mo")
    reg = _regime_from_df(df)
    # -1(悪い)..+1(良い) に正規化
    score = max(-1.0, min(1.0, reg["trend"] * 5 + (reg["above_ma"] - 1) * 0.3))
    reg["score"] = round(score, 3)
    return reg


def latest_theme_regime() -> dict:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT theme,trend,above_ma,vol_up,date FROM theme_regime "
            "WHERE date=(SELECT MAX(date) FROM theme_regime)"
        ).fetchall()
    return {r["theme"]: {"trend": r["trend"], "above_ma": r["above_ma"],
                         "vol_up": r["vol_up"], "date": r["date"]} for r in rows}


def theme_tailwind_for(sectors: list[str], material_themes: list[str]) -> tuple[float, list[str]]:
    """
    銘柄の業種/材料テーマに対応するテーマ地合いスコア(0..1)と該当テーマ名を返す。
    複数銘柄に波及しているか(above_ma, vol_up)を重視。リーダー株の強さでは加点しない。
    """
    reg = latest_theme_regime()
    if not reg:
        return 0.0, []
    text = " ".join(sectors) + " " + " ".join(material_themes)
    matched = []
    for theme in reg:
        if theme in ("TOPIX", "日経平均"):
            continue
        if theme in text or any(theme in s for s in sectors) or theme in material_themes:
            matched.append(theme)
    if not matched:
        return 0.0, []
    scores = []
    for t in matched:
        r = reg[t]
        s = max(0.0, min(1.0, (r["trend"] * 3 + 0.5) * 0.5 + 0.25 * r["above_ma"] / 2 + 0.25 * r["vol_up"]))
        scores.append(s)
    return round(max(scores), 3), matched
