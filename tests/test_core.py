"""snapshot.compute(ラベル定義)と track.evaluate(判定)の単体テスト。DB は使わない。"""
from surge_radar.snapshot import compute, price_limit
from surge_radar.track import evaluate


def _bar(d, o, h, l, c, v):
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _flat(n, price=500.0, vol=100_000):
    return [_bar(f"2026-09-{i+1:02d}", price, price * 1.01, price * 0.99, price, vol) for i in range(n)]


def test_price_limit_table():
    assert price_limit(99) == 30
    assert price_limit(100) == 50
    assert price_limit(499) == 80
    assert price_limit(999) == 150
    assert price_limit(2999) == 500
    assert price_limit(3000) == 700


def test_stop_high_close_and_breakout_and_volume_spike():
    bars = _flat(11)
    # 前日終値 500 → 値幅 100 → ストップ高 600。出来高は直近平均の 5 倍。
    bars.append(_bar("2026-09-12", 520, 600, 515, 600, 500_000))
    f, labels = compute(bars)
    assert "ストップ高引け" in labels
    assert "ストップ高タッチ" not in labels
    assert "ブレイク2週" in labels
    assert "出来高急増" in labels
    assert "急騰" in labels
    assert "ブレイク1か月" not in labels  # 足が 20 本に満たない
    assert abs(f["ret_1d"] - 0.2) < 1e-9


def test_stop_high_touch_only():
    bars = _flat(11)
    bars.append(_bar("2026-09-12", 520, 600, 515, 560, 100_000))
    _, labels = compute(bars)
    assert "ストップ高タッチ" in labels
    assert "ストップ高引け" not in labels


def test_box_and_dryup_and_low_liquidity():
    bars = [_bar(f"2026-09-{i+1:02d}", 100, 100.5, 99.5, 100, 1_000) for i in range(11)]
    bars.append(_bar("2026-09-12", 100, 100.5, 99.5, 100, 300))
    _, labels = compute(bars)
    assert "保ち合い" in labels
    assert "出来高枯れ" in labels
    assert "低流動性" in labels
    assert "値幅小" in labels


def test_one_month_labels_need_20_bars():
    bars = _flat(21)
    bars.append(_bar("2026-09-30", 500, 560, 500, 550, 100_000))
    _, labels = compute(bars)
    assert "ブレイク1か月" in labels


def test_evaluate_hit_day_and_final():
    bars = [{"date": f"d{i}", "high": h, "low": 90} for i, h in
            enumerate([105, 110, 121, 130, 100, 100, 100, 100, 100, 100, 200], 1)]
    r = evaluate(100.0, bars)
    assert r["hit"] is True and r["hit_day"] == 3
    assert r["bars_tracked"] == 10 and r["final"] is True
    assert abs(r["max_ret"] - 0.30) < 1e-9  # 11 本目の 200 は窓の外


def test_evaluate_exact_target_counts_and_partial_window():
    r = evaluate(100.0, [{"date": "d1", "high": 120.0, "low": 95.0}])
    assert r["hit"] is True and r["final"] is False
    r = evaluate(100.0, [{"date": "d1", "high": 119.99, "low": 95.0}])
    assert r["hit"] is False and r["final"] is False


def test_evaluate_no_bars_yet():
    r = evaluate(100.0, [])
    assert r["bars_tracked"] == 0 and r["hit"] is False and r["final"] is False
