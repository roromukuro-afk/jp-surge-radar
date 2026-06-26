"""エンジン中核ロジックの単体テスト (DB非依存)。"""
import numpy as np
import pandas as pd

from surge_radar import features, indicators, labeling, scoring
from surge_radar.config import SUCCESS_THRESHOLD


def _make_df(closes, vols=None, n_extra_ohlc=0.01):
    closes = np.asarray(closes, dtype=float)
    vols = vols if vols is not None else np.full(len(closes), 100000.0)
    dates = pd.date_range("2024-01-01", periods=len(closes), freq="B").strftime("%Y-%m-%d")
    return pd.DataFrame({
        "date": dates,
        "open": closes * (1 - n_extra_ohlc),
        "high": closes * (1 + n_extra_ohlc),
        "low": closes * (1 - n_extra_ohlc),
        "close": closes,
        "volume": vols,
        "turnover": closes * vols,
    })


def test_indicators_basic():
    df = _make_df(np.linspace(100, 200, 120))
    d = indicators.compute_indicators(df)
    assert d["ma25"].notna().any()
    assert d.iloc[-1]["ma5"] > d.iloc[-1]["ma25"]  # 上昇トレンドで短期>長期


def test_forward_outcome_success():
    # T0以降に+25%急騰する系列
    base = [100] * 60
    surge = [100, 105, 112, 120, 126]  # T0+...
    closes = base + surge
    df = _make_df(closes)
    oc = labeling.forward_outcome(df, idx=59)
    assert oc is not None
    assert oc["days_to_20pct"] is not None
    assert oc["max_up_20d"] >= SUCCESS_THRESHOLD
    result, tags = labeling.classify_result(oc)
    assert result in ("S", "A", "B")


def test_forward_outcome_failure():
    closes = [100] * 60 + [99, 98, 97, 96, 95, 94, 93, 92, 91, 90,
                           89, 88, 87, 86, 85, 84, 83, 82, 81, 80]
    df = _make_df(closes)
    oc = labeling.forward_outcome(df, idx=59)
    result, tags = labeling.classify_result(oc)
    assert result in ("fail", "danger_fail")
    assert labeling.is_success(oc) == 0


def test_features_and_scoring_runs():
    rng = np.random.default_rng(0)
    closes = 100 + np.cumsum(rng.normal(0, 1, 150))
    closes = np.clip(closes, 50, None)
    df = _make_df(closes)
    feats = features.build_features(df, None)
    assert feats is not None
    vec = features.to_vector(feats)
    assert len(vec) == len(features.FEATURE_KEYS)
    res = scoring.score_candidate(feats)
    assert 0.0 <= res["score"] <= 1.0
    assert res["category"] in ("A", "B", "C", "D", "E")
    assert isinstance(res["reasons"], list) and res["reasons"]


def test_exclusion_gate_popularity_loss():
    f = {"popularity_loss": 1, "liquidity_ok": 1, "turnover_log": 8}
    assert "popularity_loss" in scoring.exclusion_gates(f)


def test_realistic_upside_not_using_52w_gap():
    # 52週高値から大きく下落しているだけでは上値余地を高くしない
    f_trap = {"pct_from_52w_high": -0.4, "dist_to_resistance": 0.5,
              "near_breakout": 0, "vol_spike": 1.0, "dev25": 0.0, "broke_resistance": 0}
    up = scoring.realistic_upside(f_trap)
    assert up <= 0.7
