"""snapshot.compute(ラベル定義)と track.evaluate(判定)の単体テスト。DB は使わない。"""
from surge_radar.snapshot import compute, price_limit
from surge_radar.track import evaluate


def _bar(d, o, h, l, c, v):
    return {"date": d, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _flat(n, price=500.0, vol=100_000, spread=0.01):
    return [_bar(f"d{i:02d}", price, price * (1 + spread), price * (1 - spread), price, vol)
            for i in range(n)]


def test_price_limit_table():
    assert price_limit(99) == 30
    assert price_limit(100) == 50
    assert price_limit(499) == 80
    assert price_limit(999) == 150
    assert price_limit(2999) == 500
    assert price_limit(3000) == 700


def test_stop_high_close_breakout_volume_spike():
    bars = _flat(25)
    # 前日終値 500 → 値幅 100 → ストップ高 600。出来高は直前 20 日平均の 5 倍。
    bars.append(_bar("d25", 520, 600, 515, 600, 500_000))
    f, L = compute(bars)
    for lb in ("ストップ高引け", "20日高値更新", "10日高値更新", "5日高値更新", "出来高急増",
               "価格上昇＋出来高増加", "1日+10%以上", "大陽線", "高値引け", "20日線上", "20日線回復"):
        assert lb in L, lb
    assert "ストップ高タッチ" not in L
    assert abs(f["ret_1d"] - 0.2) < 1e-9
    assert abs(f["vol_ratio20"] - 5.0) < 1e-9


def test_stop_high_touch_and_long_upper_wick():
    bars = _flat(25)
    bars.append(_bar("d25", 505, 600, 500, 510, 100_000))
    _, L = compute(bars)
    assert "ストップ高タッチ" in L and "ストップ高引け" not in L
    assert "長い上ヒゲ" in L


def test_unknown_when_history_short():
    """足が足りない指標は None のまま残し、ラベルを付けない(不明と該当なしを区別する)。"""
    bars = _flat(8)
    bars.append(_bar("d08", 500, 560, 500, 550, 100_000))
    f, L = compute(bars)
    assert f["ma20"] is None and f["vol_ratio20"] is None and f["new_high_20"] is None
    assert not any(lb.startswith("20日") for lb in L)
    assert "出来高急増" not in L  # 20 日平均が無いので判定しない
    assert "5日高値更新" in L


def test_volume_gradual_increase_vs_spike():
    bars = _flat(20, vol=100_000)
    bars += [_bar(f"e{i}", 500, 505, 495, 500, v) for i, v in enumerate([140_000, 150_000, 160_000, 150_000, 160_000])]
    bars.append(_bar("e5", 500, 505, 495, 500, 170_000))
    _, L = compute(bars)
    assert "出来高漸増" in L
    bars[-1] = _bar("e5", 500, 505, 495, 500, 400_000)  # 直近 5 日に 3 倍超があれば漸増にしない
    _, L = compute(bars)
    assert "出来高漸増" not in L


def test_candles_and_structure():
    bars = _flat(20, price=480.0)
    # 直近 5 日: 陰線 1 本のあと陽線 3 本。高値・安値とも前の 5 日より 1% 超上
    bars += [_bar("x0", 500, 506, 499, 505, 100_000), _bar("x1", 506, 507, 500, 501, 100_000),
             _bar("x2", 501, 510, 500, 509, 100_000), _bar("x3", 509, 514, 507, 513, 100_000),
             _bar("x4", 513, 520, 511, 519, 100_000)]
    _, L = compute(bars)
    assert "高値切り上げ" in L and "安値切り上げ" in L
    assert "陽線3本連続" in L
    assert "陽線2本連続" not in L and "陽線4本以上連続" not in L  # 本数ラベルは該当する最長の 1 つだけ


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


def test_split_adjustment_prevents_false_failure():
    from surge_radar.track import adjust_for_splits
    # 2:1 分割が 3 日目に起きた。分割前の足は旧基準(1000 前後)、分割後は新基準(500 前後)
    bars = [{"date": "d1", "high": 1010, "low": 990}, {"date": "d2", "high": 1020, "low": 1000},
            {"date": "d3", "high": 610, "low": 500}]
    p0, adj, note = adjust_for_splits(1000.0, "d0", bars, [{"date": "d3", "ratio": 2.0}])
    assert p0 == 500.0 and adj[0]["high"] == 505.0 and adj[2]["high"] == 610
    assert note and "d3" in note
    r = evaluate(p0, adj)
    assert r["hit"] is True and r["hit_day"] == 3  # 分割後 610 は 500×1.2 を超える
    # 分割が無ければ何もしない
    p0, adj, note = adjust_for_splits(1000.0, "d0", bars, [{"date": "c9", "ratio": 2.0}])
    assert p0 == 1000.0 and note is None


def test_material_window():
    """Window = 基準日の終値の時刻(15:30) < 公開時刻 <= 分析開始。終値より前の材料は P0 に織り込み済み。"""
    from datetime import datetime
    from surge_radar.vocab import JST, window_start, window_status
    start = window_start("2026-09-25")                  # 金曜の大引け
    assert start == datetime(2026, 9, 25, 15, 30, tzinfo=JST)
    tn = datetime(2026, 9, 27, 10, 0, tzinfo=JST)       # 日曜 10:00 に分析開始
    at = lambda s: datetime.fromisoformat(s + "+09:00")
    assert window_status(at("2026-09-25T18:30:00"), "2026-09-25", start, tn) == "new"          # 金曜引け後の開示
    assert window_status(at("2026-09-25T15:30:00"), "2026-09-25", start, tn) == "background"   # 大引けちょうどは含まない
    assert window_status(at("2026-09-25T11:00:00"), "2026-09-25", start, tn) == "background"   # 場中 → P0 に織り込み済み
    assert window_status(at("2026-09-26T09:00:00"), "2026-09-26", start, tn) == "new"          # 土曜
    assert window_status(at("2026-09-27T10:05:00"), "2026-09-27", start, tn) == "after_t_now"  # 分析開始より後
    # 日付しか分からない見出し
    assert window_status(None, "2026-09-26", start, tn) == "new"
    assert window_status(None, "2026-09-25", start, tn) == "time_unknown"   # 終値の前か後か分からない
    assert window_status(None, "2026-09-24", start, tn) == "background"
    assert window_status(None, "2026-09-28", start, tn) == "after_t_now"


def test_timing_axis():
    from datetime import datetime
    from surge_radar.vocab import JST, timing
    at = lambda s: datetime.fromisoformat(s + "+09:00")
    days = {"2026-09-24", "2026-09-25"}
    assert timing(at("2026-09-25T08:30:00"), days) == "寄り前"
    assert timing(at("2026-09-25T15:30:00"), days) == "市場時間中"
    assert timing(at("2026-09-25T15:31:00"), days) == "引け後"
    assert timing(at("2026-09-23T10:00:00"), days) == "非営業日"   # 平日の祝日(価格データのある期間内)
    assert timing(at("2026-09-26T10:00:00"), days) == "非営業日"   # 土曜
    assert timing(None, days) == "公開時刻不明"


def test_save_deadline_is_next_weekday_open():
    from datetime import datetime
    from surge_radar.vocab import JST, save_deadline
    assert save_deadline("2026-09-25") == datetime(2026, 9, 28, 9, 0, tzinfo=JST)   # 金 → 月 9:00
    assert save_deadline("2026-09-28") == datetime(2026, 9, 29, 9, 0, tzinfo=JST)   # 月 → 火 9:00
