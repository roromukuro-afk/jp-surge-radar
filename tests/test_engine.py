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


# --- 材料の誤紐付けフィルタ (2026-09-17 深掘り分析で実データから検出) ---------
# 実際に materials テーブルに保存されていた行をそのまま使う。除外すべき他社
# 記事・市場全体ダイジェストと、残すべき自社記事の両方を固定しておく。

_DROP_CASES = [
    # (title, own_name) — 除外されるべき
    ("16日前引け(速報）：上海総合指数は0.57％高", "マクアケ"),
    ("本日の【均衡表｜3役好転／逆転】引け　好転＝ 53 銘柄　逆転＝ 55 銘柄　(9月16日)", "イオン九州"),
    ("本日の【ＭＡＣＤ｜買い／売りサイン】引け　買い＝ 20 銘柄　売り＝ 106 銘柄　(9月11日)", "井関農機"),
    ("本日の【ボリンジャー｜±３σブレイク】引け　上抜け＝ 40 銘柄　下抜け＝ 23 銘柄　(8月28日)", "イオン九州"),
    ("本日の【25日線｜上抜き／下抜き】引け　上抜け＝ 234 銘柄　下抜け＝ 200 銘柄　(9月15日)", "ユニシアホールディングス"),
    ("【高配当利回り銘柄】ベスト30 ＜割安株特集＞　9月16日版", "ＧＭＯペパボ"),
    ("【明日の好悪材料】を開示情報でチェック！ (9月16日発表分)", "エス・エム・エス"),
    ("9月7日／本日の投資戦略", "マクアケ"),
    ("転換銘柄一覧（その２）／パラボリック・シグナル転換銘柄一覧", "井関農機"),
    ("【アナリスト予想】東鉄工業、27年3月期経常予想。対前週0.4%下降。", "井関農機"),
    ("【アナリスト予想】ライフＣ、27年2月期経常予想。対前週1%下降。",
     "ユナイテッド・スーパーマーケット・ホールディングス"),
    ("【FISCO銘柄コメント】蝶理---繊維、化学品、機械の専門商社", "熊谷組"),
    ("前日に動いた銘柄 part1エアトリ、マネーフォワード、リクルートHDなど", "ユアテック"),
    ("動いた株・出来た株（前場）part1：インフォMT、さくらなど17社", "イートアンドホールディングス"),
    ("今週の【上場来高値銘柄】東計電算、エプソン、イチネンＨＤなど73銘柄", "共同ピーアール"),
    ("新興市場銘柄ダイジェスト:テラドローンがストップ高、Defコンが大幅反落",
     "Ｏｒｃｈｅｓｔｒａ　Ｈｏｌｄｉｎｇｓ"),
    # ＧＭＯＰＧ は3769 GMOペイメントゲートウェイ固有の略称。2文字スタブ「ＧＭ」が
    # 一致して 3633 GMOペパボ に7件残っていた。
    ("【アナリスト評価】ＧＭＯＰＧ、レーティング強気を継続、目標株価12,300円に引上げ（日系中堅証券）",
     "ＧＭＯペパボ"),
    ("【アナリスト予想】ＧＭＯＰＧ、26年9月期経常予想。対前週0.2%下降。", "ＧＭＯペパボ"),
    # 2026-09-17 第2弾: 当日候補の材料検証で追加検出
    ("配当利回り“4％超”の【最高益】リスト〔第2弾〕26社選出 ＜成長株特集＞", "アルプス技研"),
    ("レーティング日報【最上位を継続＋目標株価を増額】　(9月14日)", "Ａｐｐｉｅｒ　Ｇｒｏｕｐ"),
    ("「滬港通」：香港株の取引上位10銘柄（8月31日）", "Ａｐｐｉｅｒ　Ｇｒｏｕｐ"),
    ("株式明日の戦略-一時4桁下落も急速に値を戻す、25日線より下での買い意欲は強い",
     "Ａｐｐｉｅｒ　Ｇｒｏｕｐ"),
    ("採れたて株価材料　Ｈｍｃｏｍｍ　社会インフラ監視技術に関する特許出願", "エス・エム・エス"),
    ("前場コメント No.4　オンコリス、アインＨＤ、ＱＰＳＨＤ、ＳＭＳ、トレンド、三菱重",
     "富士フイルムホールディングス"),
    ("【ゲームエンタメ株前場(9/7)】『鬼武者WS』好調のカプコンが年初来高値更新",
     "Ａｐｐｉｅｒ　Ｇｒｏｕｐ"),
    # 2026-09-25: 4052/5721の材料検証で見つかった海外・新興市況コラム
    ("16日前引け(速報）：ハンセン指数は0.12％高", "フィーチャ"),
    ("新興市場展望：調整色強まりテーマ株物色へ", "フィーチャ"),
    ("東証グロ－ス指数は3日続落、上値の重い展開／グロース市況", "フィーチャ"),
    ("〔東南アジア株式〕まちまち（26日）", "エスクリプトエナジー"),
    ("アジア・新興市場の主要株価指数一覧（20日）", "原田工業"),
    ("前場に注目すべき3つのポイント～売り一巡後の底堅さを見極め～", "村田製作所"),
    # 2026-09-23: 3823の材料検証で見つかった他社まとめ枠
    ("「深港通」：香港株の取引上位10銘柄（9月17日）", "ＴＨＥ　ＷＨＹ　ＨＯＷ　ＤＯ　ＣＯＭＰＡＮＹ"),
    ("★本日の【イチオシ決算】 北川鉄、ノースサンド、ＢＲＡＮＵ (9月11日)", "ＴＨＥ　ＷＨＹ　ＨＯＷ　ＤＯ　ＣＯＭＰＡＮＹ"),
    ("明日の決算発表予定　テラドローン、サンバイオなど80社 (9月11日)", "ＴＨＥ　ＷＨＹ　ＨＯＷ　ＤＯ　ＣＯＭＰＡＮＹ"),
    ("決算プラス・インパクト銘柄 【東証スタンダード・グロース】 … アストロＨＤ、サンバイオ、ビーエイブル　(9月11日～17日発表分)",
     "ＴＨＥ　ＷＨＹ　ＨＯＷ　ＤＯ　ＣＯＭＰＡＮＹ"),
    # 2026-09-26: 3653/4180の材料検証で見つかった集計枠・他社まとめ枠
    ("明日の【信用規制・解除】銘柄 (25日大引け後 発表分)", "モルフォ"),
    ("本日の【ストップ高／ストップ安】 引け　　S高＝ 7 銘柄　　S安＝ 3 銘柄　(9月17日)", "モルフォ"),
    ("レーティング週報【最上位を継続＋目標株価を増額】(2)　(9月14日－18日)", "Ａｐｐｉｅｒ　Ｇｒｏｕｐ"),
    ("決算マイナス・インパクト銘柄 【東証スタンダード・グロース】 … テラドローン、リベラウェア、ＧＥＮＤＡ　(9月11日～17日発表分)",
     "モルフォ"),
    ("後場コメント No.3　バンダイナム、ヴィッツ、オプティム、ルネサス、インフォマート、ジグザグ", "モルフォ"),
]

_KEEP_CASES = [
    # (title, own_name) — 自社記事なので残すべき
    ("【FISCO銘柄コメント】ＧＭＯペパボ---レンタルサーバーサービス「ロリポップ！」", "ＧＭＯペパボ"),
    ("【FISCO銘柄コメント】井関農機---老舗農業機械メーカー", "井関農機"),
    ("【アナリスト予想】熊谷組、27年3月期経常予想。対前週1%上昇。", "熊谷組"),
    ("人事、井関農機", "井関農機"),
    ("ひらまつ、台湾の百貨店に出店へ　27年に飲食で1号店", "ひらまつ"),
    ("マクアケ株価、下落に転じる　26年９月期上方修正も戻り売り", "マクアケ"),
    # 個別銘柄が主役のテクニカル記事は、集計枠の接頭辞に限定したので残る
    ("テクニカルで選ぶ注目銘柄：エムスリー＝日足一目均衡表の「雲」の上限を突破", "エムスリー"),
    ("村田製---25日線や75日線を射程に入れたリバウンド狙い", "村田製作所"),
    # 2026-09-23: 決算まとめ枠でも自社が主役の回は残す
    ("明日の決算発表予定　大光 (9月18日)", "大光"),
    ("★本日の【イチオシ決算】 ニッカトー、日本インシュ、和田興産 (9月18日)",
     "日本インシュレーション"),
    ("決算プラス・インパクト銘柄 【東証プライム】 … アスクル、日本駐車場、Ｔ－ＢＡＳＥ　(9月11日～17日発表分)",
     "アスクル"),
    # 対象企業がタイトルに並ぶ「銘柄一覧」型は自社名一致で残す
    ("ＴＯＢ銘柄一覧（24日）－ボードルア、アールビバン、カカクコム、フューチャーなど", "ボードルア"),
    # まとめ枠でも自社が主役の回は残す
    ("採れたて株価材料－マツキヨココカラ－ジョンマスターオーガニック運営会社を買収",
     "マツキヨココカラ＆カンパニー"),
    ("【ゲームエンタメ株前場(8/24)】提携発表のサンリオとGENDA買われる　目標株価引き上げのソニーGやコナミG、カバーも高い",
     "カバー"),
    ("ＳＭＳ、オアシス・マネジメントの保有割合が２６．９７％に上昇", "エス・エム・エス"),
    ("SMS、AI作成の看護記録をクラウドに反映　エンベースと提携", "エス・エム・エス"),
    # 2026-09-26: 後場コメント/決算マイナス・インパクトでも自社が並ぶ回は残す
    ("後場コメント No.4　三菱電、アイル、ＭＳ＆ＡＤ、武田、カプコン、村田製", "アイル"),
    ("決算マイナス・インパクト銘柄・引け後 … バリューＣ、東和フード、キタック　(9月17日発表分)", "キタック"),
]


def _excluded(title, own_name):
    from surge_radar.materials import _is_generic_market_digest, _title_company_mismatch
    return _is_generic_market_digest(title) or _title_company_mismatch(title, own_name)


def test_material_misattribution_dropped():
    for title, own in _DROP_CASES:
        assert _excluded(title, own), f"除外されるべき: {own} <- {title}"


def test_material_own_article_kept():
    for title, own in _KEEP_CASES:
        assert not _excluded(title, own), f"残すべき自社記事が除外された: {own} <- {title}"
