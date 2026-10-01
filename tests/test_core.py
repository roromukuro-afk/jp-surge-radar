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


DAYS = [f"2026-10-{d:02d}" for d in (1, 2, 5, 6, 7, 8, 9, 13, 14, 15, 16)]  # 営業日(10/12 は祝日)


def _bars(highs, dates=DAYS):
    return [{"date": d, "high": h, "low": 90.0} for d, h in zip(dates, highs)]


def test_evaluate_hit_day_and_final():
    from surge_radar.track import status_of
    r = evaluate(100.0, _bars([105, 110, 121, 130, 100, 100, 100, 100, 100, 100, 200]), DAYS[:10])
    assert r["hit"] is True and r["hit_day"] == 3
    assert r["final"] is True and r["deadline"] == "2026-10-15"
    assert abs(r["max_ret"] - 0.30) < 1e-9          # 11 営業日目の 200 は期間の外
    assert status_of(r) == "hit"


def test_missing_bar_does_not_shift_window_into_day_11():
    """株価が 1 日欠けても、11 営業日目の高値を期間内に数えない(本数ではなく日付で判定する)。"""
    from surge_radar.track import status_of
    bars = _bars([100] * 11)
    bars = [b for b in bars if b["date"] != "2026-10-06"]          # 4 営業日目が欠けた
    bars[-1]["high"] = 150                                          # 11 営業日目(10/16)だけ +50%
    r = evaluate(100.0, bars, DAYS[:10])
    assert r["hit"] is False
    assert r["missing_dates"] == ["2026-10-06"]
    assert status_of(r) == "unverified"                             # 失敗にもしない


def test_miss_needs_complete_window():
    from surge_radar.track import status_of
    r = evaluate(100.0, _bars([110] * 10), DAYS[:10])
    assert r["final"] and not r["hit"] and not r["missing_dates"] and status_of(r) == "miss"


def test_evaluate_exact_target_and_partial_window():
    from surge_radar.track import status_of
    r = evaluate(100.0, _bars([120.0]), DAYS[:1])
    assert r["hit"] is True and r["final"] is False and status_of(r) == "hit"
    r = evaluate(100.0, _bars([119.99]), DAYS[:1])
    assert r["hit"] is False and r["final"] is False and status_of(r) == "tracking"


def test_evaluate_no_bars_yet():
    r = evaluate(100.0, [], [])
    assert r["days_elapsed"] == 0 and r["hit"] is False and r["final"] is False


def test_split_factor():
    from surge_radar.track import split_factor
    # 基準日より後の 2:1 分割 → P0 を 2 で割る。基準日以前の分割は関係ない
    f, note = split_factor("2026-10-01", [{"date": "2026-09-01", "ratio": 5.0}, {"date": "2026-10-06", "ratio": 2.0}])
    assert f == 2.0 and "2026-10-06" in note
    r = evaluate(1000.0 / f, _bars([505, 510, 610]), DAYS[:3])    # 取り直した株価はすべて分割後の基準
    assert r["hit"] is True and r["hit_day"] == 3
    assert split_factor("2026-10-01", []) == (1.0, None)


def test_material_window():
    """Window = 基準日の終値の時刻(15:30) <= 公開時刻 <= 分析開始。終値より前の材料は P0 に織り込み済み。"""
    from datetime import datetime
    from surge_radar.vocab import JST, window_start, window_status
    start = window_start("2026-09-25")                  # 金曜の大引け
    assert start == datetime(2026, 9, 25, 15, 30, tzinfo=JST)
    tn = datetime(2026, 9, 27, 10, 0, tzinfo=JST)       # 日曜 10:00 に分析開始
    at = lambda s: datetime.fromisoformat(s + "+09:00")
    assert window_status(at("2026-09-25T18:30:00"), "2026-09-25", start, tn) == "new"          # 金曜引け後の開示
    assert window_status(at("2026-09-25T15:30:00"), "2026-09-25", start, tn) == "new"          # 大引けちょうども含む(2026-09-28、ユーザー指示「終値の時刻から」)
    assert window_status(at("2026-09-25T15:29:00"), "2026-09-25", start, tn) == "background"   # 大引け前
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


def test_fetch_per_code_records_failures_and_stops_on_block():
    """取れなかった銘柄を 0 件(empty)と区別し、失敗が続いたら残りは取りに行かない(2026-09-28)。"""
    from surge_radar.news import FetchFailed, _fetch_per_code
    calls = []

    def fake(code, session=None):
        calls.append(code)
        if code == "1001":
            return [{"date": "2026-09-28", "title": "t"}]
        if code == "1002":
            return []
        raise FetchFailed("http_403")

    codes = ["1001", "1002"] + [f"2{i:03d}" for i in range(10)]
    by_code, st = _fetch_per_code(codes, fake, "test", pause=0, stop_after=3)
    assert list(by_code) == ["1001"]
    assert st["1001"] == "ok" and st["1002"] == "empty"
    assert [st[c] for c in codes[2:5]] == ["http_403"] * 3
    assert all(st[c] == "skipped_blocked" for c in codes[5:])
    assert calls == codes[:5]                      # 打ち切り後は 1 回も取りに行っていない


def test_fetch_per_code_streak_resets_on_success():
    from surge_radar.news import FetchFailed, _fetch_per_code
    seq = iter(["x", "x", "ok", "x", "x", "ok"])

    def fake(code, session=None):
        if next(seq) == "x":
            raise FetchFailed("http_429")
        return []

    _, st = _fetch_per_code([str(i) for i in range(6)], fake, "test", pause=0, stop_after=3)
    assert "skipped_blocked" not in st.values()


def test_material_checked():
    """「新規材料なし」と書けるのは TDnet が取れて、銘柄別ニュースが 1 サイト以上取れた(0 件を含む)とき。"""
    from surge_radar.news import material_checked
    ok_bulk = {"tdnet": "ok", "edinet": "ok"}
    blocked = {"kabutan": "http_403", "yahoojp": "skipped_blocked", "nikkei": "error_Timeout",
               "minkabu": "not_recorded"}
    assert material_checked(ok_bulk, {**blocked, "nikkei": "empty"}) is True
    assert material_checked(ok_bulk, blocked) is False
    assert material_checked({"tdnet": "error_RuntimeError"}, {**blocked, "kabutan": "ok"}) is False
    assert material_checked({"tdnet": "not_recorded"}, {"kabutan": "ok"}) is False


def test_nikkei_page_without_list_is_failure(monkeypatch):
    """一覧の無いページ(HTTP 200)を「0 件」と記録しない(2026-09-29 に 2,849 銘柄で起きた)。"""
    import pytest
    from surge_radar import news

    class R:
        status_code = 200
        encoding = "utf-8"
        def __init__(self, text): self.text = text

    class S:
        def __init__(self, text): self.t = text
        def get(self, *a, **k): return R(self.t)

    with pytest.raises(news.FetchFailed) as e:
        news.fetch_nikkei_news("6533", session=S("<html><title>x</title><body></body></html>"))
    assert e.value.status == "no_list"
    with pytest.raises(news.FetchFailed):
        news.fetch_kabutan_news("6533", session=S("<html><body>no table</body></html>"))
    # 一覧はあるが保存できる見出しが無い(更新日時しか無い)ときは 0 件
    page = ('<ul><li class="m-listFormat_item"><span class="m-listItem_time">9/15更新</span>'
            '<div class="m-listItem_text_text"><a href="/a">t</a></div></li></ul>')
    assert news.fetch_nikkei_news("6533", session=S(page)) == []


def test_label_groups_clustering():
    """ラベルの重なりだけでまとめる(成否は使わない)。似た集合は同じグループ、離れた集合は別。"""
    import sys, pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
    from label_groups import clusters, jaccard_distance, rate_range
    a = frozenset({"上昇トレンド", "20日高値更新", "出来高急増", "大陽線"})
    b = frozenset({"上昇トレンド", "20日高値更新", "出来高急増", "陽線2本連続"})
    c = frozenset({"横ばい", "材料状態:あり", "材料:M&A・事業再編"})
    assert jaccard_distance(a, a) == 0 and jaccard_distance(a, c) == 1
    g = clusters([a, b, c])
    assert g[0] == g[1] != g[2]
    assert clusters([a]) == [0]
    assert rate_range({"success": 1, "failure": 1, "tracking": 2, "unverified": 5}) == "25%〜75%"


def test_collect_nikkei_only_targets(monkeypatch):
    """日経は渡された銘柄だけ、他の配信元は全銘柄を取りに行く(DB には書かない)。"""
    from surge_radar import news
    seen = {"kabutan": [], "yahoojp": [], "nikkei": []}
    for name, attr in (("kabutan", "fetch_kabutan_news"), ("yahoojp", "fetch_yahoo_jp_news"),
                       ("nikkei", "fetch_nikkei_news")):
        monkeypatch.setattr(news, attr, lambda code, session=None, n=name: seen[n].append(code) or [])
    monkeypatch.setattr(news, "fetch_tdnet_range", lambda s, u: {})
    monkeypatch.setattr(news, "fetch_edinet_docs", lambda d: {})
    monkeypatch.setattr(news, "store", lambda *a, **k: 0)
    monkeypatch.setattr(news, "NIKKEI_PAUSE", 0)
    monkeypatch.setattr(news.time, "sleep", lambda s: None)
    out = news.collect(["1001", "1002", "1003"], "2026-09-30", "2026-09-30", nikkei_codes=["1002"])
    assert seen["kabutan"] == seen["yahoojp"] == ["1001", "1002", "1003"]
    assert seen["nikkei"] == ["1002"]
    assert "minkabu" not in out


def test_calendar_from_prices_sql_shape():
    """1306 が欠けても、1,000 銘柄以上の日足がある日を営業日に加える関数がある(DB は使わない形の確認)。"""
    import inspect
    from surge_radar import market_calendar
    src = inspect.getsource(market_calendar.add_from_prices)
    assert "HAVING COUNT(*) >= %s" in src and "'prices'" in src
    assert market_calendar.MIN_CODES == 1000
