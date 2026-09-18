"""
総合急騰スコアリング & 候補分類。

設計の肝(要件):
- 平均点ランキングにしない。「短期急騰の火種ランキング」にする。
- 絶対評価 + 相対評価 + 除外ゲート + 不確実性 + AI総合判断 を組み合わせる。
- どれか一つが突出して火種になるケースも拾うが、他要素が完全破綻なら上位にしない。
- 52週高値までの値幅を上値余地にしない。右肩下がり/需要切れ/高値圏トラップを除外。
"""
from __future__ import annotations

from .config import MIN_AVG_TURNOVER

# サブスコア重み (相対評価の基礎)。
#
# 2026-09-17: 満期済み4,798件で各成分の top10 成功率(ベースライン14.8%)と
# danger_fail率を実測し、run_date を8日/8日に分けたホールドアウトで配分を選んだ。
#
#   成分         成功率   danger   備考
#   volatility   62.5%    20.6%   最強だがリスクも最大
#   chart        42.5%    11.9%   是正後。リスク調整後で最良
#   prob(ML)     40.0%    23.8%   composite側で別枠
#   theme        23.8%     7.5%
#   fundamental  22.5%     7.5%
#   volume       19.4%    18.1%   リターンの割にリスクが高い
#   similarity   18.1%     4.4%   最低リスク。分散に効く
#   material     14.4%    17.5%   予測力なし(ベースライン並み)
#
# 旧配分は material 0.26 / chart 0.22 / volume 0.22 と、予測力のない3成分に
# 全体の70%を割り当てていた。検証側 top10 は 35.0%(danger 32.5%)。
# 採用した配分の検証側は 48.8%(danger 18.8%)。
# なお全期間グリッドの最良配分は material に 0.20 を割り当てるが、単独で
# 予測力のない成分に重みを置くのは過学習と判断し、danger が9pt低く成功率が
# 同じこちらを採った。
WEIGHTS = {
    "chart": 0.33,
    "similarity": 0.33,
    "volatility": 0.22,
    "volume": 0.05,
    "theme": 0.05,
    "fundamental": 0.02,
    "material": 0.00,
}


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def chart_score(f: dict) -> float:
    """安値切り上げ・下落止まりを評価し、動かない形と上値が詰まった形を減点する。

    2026-09-17に満期済み4,798件で構成要素を個別測定したところ、旧実装
    (下落止まり→ボラ縮小→横ばい→安値切り上げ→ブレイク という底固めの型を
    高く評価する設計)は合成すると逆指標になっていた:
    chart_score上位10件の成功率6.9%に対し、下位10件は15.0%(ベースライン14.8%)。

    各要素の実測 (top10成功率、高い順 vs 低い順):
      higher_lows            39.4% / 16.2%  → +23.2pt  最も強い正の要素
      downtrend_stopped      28.7% / 15.6%  → +13.1pt
      volatility_contraction 21.2% / 15.0%  →  +6.2pt
      sideways                9.4% / 45.0%  → -35.6pt  ★旧実装は+0.12で加点
      high_zone_upper_wick    5.0% / 25.6%  → -20.6pt  減点で正しい
      near_breakout          16.9% / 33.1%  → -16.2pt  ★旧実装は+0.16で加点
      downtrend_risk         26.9% / 18.8%  →  +8.1pt  ★旧実装は-0.30で最大の減点
      lower_highs_stopped    30.6% / 35.0%  →  -4.4pt  誤差範囲
      broke_resistance       20.0% / 23.1%  →  -3.1pt  誤差範囲
      price_above_ma25       23.8% / 25.0%  →  -1.2pt  誤差範囲
      rebound_capped         23.8% / 23.8%  →   0.0pt  効果なし

    sideways(横ばい)と near_breakout(抵抗線に近い=上値が詰まっている)は
    「20営業日で+20%動く」という目的に対して明確に逆方向だったため符号を反転。
    効果量が5pt未満の4要素は削除した(16 run_date しかない標本で微小な差に
    重みを付けても過学習になる)。

    downtrend_risk は実測では正(右肩下がりのほうが成功率が高い)だが、これは
    pct_from_52w_high で既に realistic_upside 側が評価している「高値から離れて
    いるほど戻し余地が大きい」と同じ現象なので、ここで加点すると二重計上に
    なる。符号を反転させるのではなく除外する。
    """
    s = 0.15
    s += 0.30 * _clip01(f.get("higher_lows", 0) + 0.3)          # 安値切り上げ
    s += 0.24 * _clip01(f.get("downtrend_stopped", 0) + 0.5)    # 下落止まり
    s += 0.16 * _clip01(f.get("volatility_contraction", 0))     # ボラ縮小
    s -= 0.30 * _clip01(f.get("sideways", 0))                   # 横ばい=動かない
    s -= 0.20 * _clip01(f.get("near_breakout", 0))              # 上値が詰まっている
    s -= 0.25 * f.get("high_zone_upper_wick", 0)                # 高値圏上ヒゲ
    return _clip01(s)


def volatility_score(f: dict) -> float:
    """20営業日で+20%動ける物理的な適性。

    2026-09-17の実測(満期4,798件、ベースライン14.8%)で、直近20本の日中値幅
    平均が単独で最も強い予測因子だった:
      <2%:1.9%(n=365) / 2-3%:8.1%(1473) / 3-4%:11.0%(1430)
      / 4-6%:21.6%(1143) / 6%超:45.5%(387)
    判定カテゴリでも52週高値距離でも条件付けして単調性が保たれる。

    8%で1.0に飽和させる。それ以上は上下の振れが対称に近づき(6%超帯は
    +20%到達45.5%に対し-20%到達15.5%で比2.93と、2-3%帯の10.91より低い)、
    青天井に加点する根拠がないため。
    """
    dr = f.get("daily_range_20", 0.0)
    return _clip01(dr / 0.08)


def volume_score(f: dict) -> float:
    s = 0.0
    spike = f.get("vol_spike", 1.0)
    # 出来高急増(初動)。3倍前後を上限に評価。天井大商いは別途減点。
    s += 0.30 * _clip01((spike - 1.0) / 2.0)
    s += 0.18 * _clip01(f.get("up_down_vol_bias", 0) * 0.5 + 0.5)   # 上昇日に出来高
    # 出来高急増後に価格を維持したか(-0.1で0, +0.1で満点)
    s += 0.16 * _clip01((f.get("held_after_vol_spike", 0) + 0.1) / 0.2)
    s += 0.14 * f.get("dry_up", 0)                                  # 売り枯れ
    # 平均売買代金(流動性の底上げ)
    if f.get("liquidity_ok", 0):
        s += 0.10
    # リスク減点
    s -= 0.35 * f.get("volume_top_risk", 0)                         # 天井大商い
    s -= 0.40 * f.get("popularity_loss", 0)                         # 人気離散
    return _clip01(s)


def material_score(f: dict) -> float:
    raw = f.get("material_raw", 0.0)
    s = 0.70 * raw
    if f.get("has_fresh_material", 0):
        s += 0.15                                   # T0/T-1の新鮮な材料
    # 材料に出来高/チャートが反応しているか(接続確認)
    if raw > 0.2 and f.get("vol_spike", 1) > 1.3:
        s += 0.10
    if raw > 0.2 and f.get("near_breakout", 0) > 0.5:
        s += 0.05
    # ネガ材料減点
    s -= 0.40 * f.get("neg_impact", 0.0)
    s -= 0.25 * f.get("dilution_flag", 0)
    s -= 0.30 * f.get("going_concern_flag", 0)
    return _clip01(s)


def theme_score(f: dict) -> float:
    return _clip01(f.get("theme_tailwind", 0.0))


def fundamental_score(f: dict) -> float:
    """短期狙いのため中立0.5基準。リスク要因のみ減点(機械的に落としすぎない)。"""
    s = 0.5
    s -= 0.25 * f.get("going_concern_flag", 0)
    s -= 0.15 * f.get("dilution_flag", 0)
    return _clip01(s)


def exclusion_gates(f: dict) -> list[str]:
    """除外/重大減点ゲート。該当タグを返す(空なら問題なし)。"""
    gates = []
    if not f.get("liquidity_ok", 0):
        # 2026-08-26判明: 従来は `turnover_log < 7.0`(約1000万円/日)も同時に
        # 満たさないと除外されなかったが、liquidity_ok自体はMIN_AVG_TURNOVER
        # (3000万円/日)基準で判定している。1000万〜3000万円/日の銘柄は
        # liquidity_ok=0(不合格)なのにturnover_log>=7.0のためこのANDを
        # すり抜け、A/B候補として通過し続けていた(3121で実例確認: 8/21・
        # 8/26と2回A判定されたが、8/21分は判定フェーズでliquidity_failとして
        # failし、学習ログ自体が「流動性ゲートを厳格化」と記録していた=
        # 判定時と予測時で実質異なる基準を使っていた不整合)。liquidity_ok
        # 単体で判定するよう単純化し、判定時と予測時の基準を一致させる。
        gates.append("liquidity_fail")              # 流動性不足
    if f.get("popularity_loss", 0):
        gates.append("popularity_loss")             # 人気離散(出来高減+価格下落)
    if f.get("downtrend_risk", 0) >= 0.75 and f.get("material_raw", 0) < 0.2:
        gates.append("downtrend_no_material")       # 右肩下がり+材料なし
    if f.get("high_zone_upper_wick", 0) and f.get("volume_top_risk", 0):
        gates.append("high_zone_trap")              # 高値圏天井トラップ
    if f.get("going_concern_flag", 0) and f.get("material_raw", 0) < 0.3:
        gates.append("going_concern")
    return gates


def realistic_upside(f: dict) -> float:
    """
    +20%の現実到達余地(0..1)。
    52週高値までの値幅は使わない。抵抗線までの距離・初動性・出来高で評価。
    """
    s = 0.5
    # 抵抗線が近すぎず遠すぎず(5〜25%上)だと素直に伸びやすい
    dr = f.get("dist_to_resistance", 0)
    if 0.03 <= dr <= 0.30:
        s += 0.2
    elif dr > 0.30:
        s += 0.1
    if f.get("near_breakout", 0) > 0.5 or f.get("broke_resistance", 0):
        s += 0.2
    if f.get("vol_spike", 1) > 1.5:
        s += 0.1
    # 既に上がり切っている(25日線乖離が大)は余地減
    if f.get("dev25", 0) > 0.25:
        s -= 0.3
    # 2026-09-03判明: 判定済み予測1384件の事後分析で、52週高値からの距離
    # (pct_from_52w_high、負値=高値からの下落率)と成功率に強い相関を確認。
    # D判定: 高値近辺(0〜-5%)12.0%・-5〜-15%25.5%・-15〜-30%39.6%・-30%超56.8%
    # (n=167/216/225/398、単調増加)。B判定でも高値近辺は59.1%と他レンジ
    # (77〜92%)より明確に低い。「52週高値までの値幅を上値余地にしない」
    # という従来方針(このdocstring冒頭)は妥当だが、高値圏そのものへの
    # 減点が高値圏上ヒゲ+出来高天井の組み合わせ(exclusion_gates の
    # high_zone_trap)でしか効いておらず、この単純な距離指標だけでの
    # 高値づかみリスクを拾えていなかった。
    #
    # 2026-09-03追記: 導入時の-0.25/-0.08は composite 側で upside の重みが
    # 12%しかないため、実際のcomposite寄与差はわずか0.03点(0.09〜0.12)しか
    # 生まれず、観測された効果の大きさ(D判定で成功率が最大4.7倍違う)に対して
    # 実効性が乏しかった。composite寄与差を0.03→0.08点程度に広げるため
    # -0.7/-0.25 に強化。
    p52 = f.get("pct_from_52w_high", -1)
    if p52 >= -0.05:
        s -= 0.7
    elif p52 >= -0.15:
        s -= 0.25

    # 2026-09-17: 当初この関数に日中値幅の加減点を入れたが、その後 volatility を
    # 独立したサブスコア(WEIGHTS で0.22)に格上げしたため、ここに残すと二重計上に
    # なる。値幅の評価は volatility_score() に一本化した。
    return _clip01(s)


def score_candidate(f: dict, ml_prob: float | None = None,
                    similarity: float | None = None,
                    extra_info: dict | None = None,
                    danger_similarity: float | None = None,
                    path_trust: dict | None = None,
                    sim_thresholds: dict | None = None) -> dict:
    """
    1銘柄のフルスコアリング。サブスコア・総合・分類・理由・失敗条件を返す。
    extra_info: {"top_category", "top_title", "themes_matched", "name"} を受け取ると
    reasons の文章が具体的になる。
    danger_similarity: 過去の危険失敗パターンへの類似度(0..1)。similarity(成功類似度)との
                差分(超過分)のみを減点に使う — 絶対値は similarity と相関0.92で
                ほぼ同一(急騰候補は成功/失敗問わず「勢いのある値動き」という同じ特徴量
                領域を共有するため)。差分でないと実質全候補が一律減点されてしまう。
    path_trust: classify_path -> trust_multiplier の辞書 (learning.get_trust_multipliers())。
                実績(shrinkage込み)に応じてスコアを自律調整する。件数少ない pathは1.0付近。
    sim_thresholds: {"strong","very_strong"} — model.Predictor.sim_thresholds。
                ライブサンプルの増減でpos プールの類似度分布が動くたびに固定閾値が
                選別力を失う/暴走する不具合(2026-08-10, 08-14)を受け、再学習ごとに
                自己較正した値を都度渡す。省略時は安全なデフォルト(0.68/0.78)。
    """
    sub = {
        "material": material_score(f),
        "chart": chart_score(f),
        "volume": volume_score(f),
        "theme": theme_score(f),
        "similarity": float(similarity) if similarity is not None else 0.0,
        "fundamental": fundamental_score(f),
        "volatility": volatility_score(f),
    }
    gates = exclusion_gates(f)
    upside = realistic_upside(f)
    dsim_raw = float(danger_similarity) if danger_similarity is not None else 0.0
    sim_val = float(similarity) if similarity is not None else 0.0
    # 危険パターン類似度の"超過分"のみを見る (成功類似度を上回った分だけが本当のリスク信号)。
    # 絶対値のまま使うと成功類似度と相関0.92で実質同じものを二重評価してしまう。
    dsim = max(0.0, dsim_raw - sim_val)

    weighted = sum(WEIGHTS[k] * sub[k] for k in WEIGHTS)
    top = max(sub["material"], sub["chart"], sub["volume"], sub["theme"], sub["similarity"])  # 火種
    prob = float(ml_prob) if ml_prob is not None else weighted
    sub["probability"] = round(prob, 4)  # _classify で高確率判定に使用

    # 総合: 相対(weighted) + 不確実性込みML + 火種(突出) + 現実到達余地
    #
    # 2026-09-17: 係数もホールドアウトで測り直した(検証8日、top10成功率)。
    #   旧式+旧重み 35.0%(danger 32.5%) → 旧式+新重み 43.8%(25.0%)
    #   → prob を 0.28→0.10 に下げて 48.8%(18.8%)
    # ML確率は単独では40.0%と強いが danger_fail 23.8% と高く、0.28も配ると
    # リスクだけが増えていた。top(火種)を削ると検証top20が35.0→36.9%と僅かに
    # 上がるが top10 は同値で、材料の価値を測れるようになった時に効く可能性が
    # あるため残している。
    composite = (0.62 * weighted + 0.10 * prob + 0.18 * top + 0.10 * upside)

    # リスク減衰
    risk = 0.0
    risk += 0.20 * f.get("downtrend_risk", 0)
    risk += 0.10 * f.get("rebound_capped", 0)
    risk += 0.10 * f.get("volume_top_risk", 0)
    composite *= (1 - min(risk, 0.5))

    # 危険失敗パターン類似度(超過分)による減点。実データで超過分は大半0、
    # p99程度でも~0.015、最大でも~0.04程度の小さいスケールのため、係数を大きめに
    # (最大35%減に上限)。ごく少数(上位数%)の "本当に危険寄り" な候補だけに効く設計。
    if dsim > 0:
        composite *= (1 - min(0.35, 2.0 * dsim))

    # ゲート: 該当で大幅減点(完全破綻は実質除外)
    if gates:
        composite *= 0.25

    composite = _clip01(composite)
    st = sim_thresholds or {"strong": 0.68, "very_strong": 0.78}
    category, classify_path = _classify(sub, f, gates, upside, composite, st)

    # 自律学習フィードバック: 実績(shrinkage込み)に応じたpath別信頼度を反映。
    # 件数が少ないpathはmultiplierが1.0近傍のため実質無調整 (learning.py参照)。
    trust_mult = 1.0
    if path_trust and classify_path in path_trust:
        trust_mult = path_trust[classify_path]
        if abs(trust_mult - 1.0) > 1e-6:
            adjusted = _clip01(composite * trust_mult)
            category, classify_path = _classify(sub, f, gates, upside, adjusted, st)
            composite = adjusted

    reasons = _reasons(sub, f, upside, extra_info)
    if dsim >= 0.02:
        reasons.append(f"注意: 成功類似度を上回る危険失敗パターン類似(超過{dsim:.3f}) — 慎重評価")
    fail_conditions = _failure_conditions(f, sub)

    return {
        "sub": {k: round(v, 3) for k, v in sub.items()},
        "score": round(composite, 4),
        "probability": round(prob, 4),
        "upside": round(upside, 3),
        "category": category,
        "classify_path": classify_path,
        "gates": gates,
        "reasons": reasons,
        "failure_conditions": fail_conditions,
        "top_driver": max(sub, key=sub.get),
        "danger_similarity": round(dsim, 3),
        "path_trust_multiplier": round(trust_mult, 3),
        "price_levels": _price_levels(f),
    }


def _price_levels(f: dict) -> dict:
    """+20%到達までの経路を具体的な価格で示す(表示専用、スコアには不使用)。

    overhead_supply_days(現値〜+20%帯の累積出来高÷直近20日平均出来高)は
    2026-09-17の実測で成功率と逆相関する(上値が厚いほど成功率が高い)ことが
    判明しており、これは pct_from_52w_high の言い換えであることも2次元
    クロス集計で確認済み。従ってスコアには入れず、参考情報としてのみ出す。
    """
    close = f.get("_close", 0.0)
    if not close:
        return {}
    return {
        "base": round(close),
        "target20": round(f.get("_target20_price", close * 1.2)),
        "resistance": round(f.get("_resistance_price", 0.0)),
        "support": round(f.get("_support_price", 0.0)),
        "failure_line": round(f.get("_failure_line_price", 0.0)),
        "failure_line_date": f.get("_failure_line_date", ""),
        "failure_distance_pct": round(f.get("_failure_distance", 0.0) * 100, 1),
        "overhead_supply_days": round(f.get("_overhead_supply_days", 0.0), 1),
    }


# カテゴリの順位境界 (その日の非ゲート候補内での順位)。
# 2026-09-17に満期済み4,798件で実測した成功率に合わせた:
#   1-5位 70.0% / 6-10位 40.0% / 11-20位 31.2% / 21-30位 28.1%
#   / 31-50位 26.9% / 51-100位 14.6% / 101位以下 10.1%  [ベースライン14.8%]
CATEGORY_RANKS = (("A", 5), ("B", 20), ("C", 50), ("D", 100))


def assign_categories(scored: list[dict]) -> None:
    """その日の候補全体の順位からカテゴリを付け直す(リストを直接書き換える)。

    2026-09-17まではルール条件の組み合わせ(_classify)でカテゴリを決めていたが、
    満期済み4,798件で測ると classify_path 別の成功率は件数30以上の6経路すべてが
    8.6〜19.9%に収まり、ベースライン14.8%と区別できなかった。除外ゲートで弾いた
    E_gate が15.2%で、B_ai_signal(14.9%)や B_ml_prob_ai(11.1%)より高いという
    逆転まで起きていた。一方スコアの順位は 70.0%→10.1% と単調に7倍の差がつく。
    ラベルの精度はスコアが担保し、「なぜこの銘柄か」の説明は classify_path と
    reasons が担う、という分担にする。

    ゲート該当銘柄は従来どおり E のまま順位から外す。ゲートには流動性不足
    (MIN_AVG_TURNOVER未満)のように「実際に買えない」条件が含まれており、
    成功率と相関しないことと、候補として出してよいことは別だから。
    ルールベースの判定結果は flags.rule_category に残す。
    """
    ranked = [r for r in scored if not r.get("gates")]
    ranked.sort(key=lambda r: -r["score"])
    for r in scored:
        r["rule_category"] = r.get("category", "")
    for i, r in enumerate(ranked, 1):
        for cat, limit in CATEGORY_RANKS:
            if i <= limit:
                r["category"] = cat
                break
        else:
            r["category"] = "E"
    for r in scored:
        if r.get("gates"):
            r["category"] = "E"


def _classify(sub: dict, f: dict, gates: list[str], upside: float, composite: float,
             sim_thresholds: dict | None = None) -> tuple[str, str]:
    """ルールベースのカテゴリと分類パス名を返す。

    2026-09-17以降、表示されるカテゴリは assign_categories() が順位から付け直す。
    ここで返すカテゴリは flags.rule_category として記録されるだけで、
    classify_path のほうが本来の役割(どの条件で拾われたかの説明)になっている。
    """
    if gates:
        return "E", "E_gate"
    st = sim_thresholds or {"strong": 0.68, "very_strong": 0.78}
    strong_material   = sub["material"] >= 0.5
    decent_material   = sub["material"] >= 0.3
    # 2026-09-17: chart_score の構成要素を実測に合わせて作り直した際、スコアの
    # 分布が下にずれた(中央値 0.386 → 0.134)。旧閾値 0.55/0.40 のままだと
    # A/B/C 候補がほぼ消滅するため、旧分布で同じパーセンタイル(91.9% / 52.2%)に
    # あたる値に較正した。候補の出現数を変えずに中身だけ入れ替えるのが狙い。
    good_chart        = sub["chart"] >= 0.43
    fair_chart        = sub["chart"] >= 0.15
    good_volume       = sub["volume"] >= 0.50
    decent_volume     = sub["volume"] >= 0.35
    # 2026-08-14: 固定の magic number (0.68/0.78 →一時0.85/0.92) は、ライブサンプル
    # 数が48→161件に増えるだけで pos プールの分布が動き再び暴走した(B 142→238)。
    # 再学習のたびに model.py が典型的な負例に対する類似度分布を実測し直し、
    # 上位パーセンタイルを自己較正した値(sim_thresholds)を使う — 手動チューニング
    # に依存しない。
    strong_ai         = sub["similarity"] >= st["strong"]
    very_strong_ai    = sub["similarity"] >= st["very_strong"]
    high_prob         = sub.get("probability", 0) >= 0.80
    broke             = f.get("broke_resistance", 0)
    near              = f.get("near_breakout", 0) > 0.5

    # A: 材料+チャート+出来高揃い / または AI超高+チャート+出来高
    if composite >= 0.62 and upside >= 0.50:
        if decent_material and (good_chart or broke) and good_volume:
            return "A", "A_material_chart_volume"
        if very_strong_ai and (good_chart or broke) and good_volume:
            return "A", "A_ai_chart_volume"
        if high_prob and very_strong_ai and good_chart:
            return "A", "A_ml_ai_chart"

    # B: ブレイク確認買い型 (材料あり+前兆 / チャート+出来高 / AI強+シグナル)
    if composite >= 0.50:
        if decent_material and (near or good_volume):
            return "B", "B_material_volume"
        if good_chart and good_volume:
            return "B", "B_chart_volume"
        if strong_ai and (near or fair_chart) and decent_volume:
            return "B", "B_ai_signal"
        if very_strong_ai and composite >= 0.53:
            return "B", "B_very_strong_ai"
        if high_prob and strong_ai:
            return "B", "B_ml_prob_ai"
        if high_prob and decent_material and composite >= 0.52:
            return "B", "B_ml_prob_material"

    # C: 押し目・再点火待ち (売り枯れ+値持ち+AI確認)
    if strong_material and f.get("dry_up", 0) and sub["volume"] >= 0.35:
        return "C", "C_material_dryup"
    if strong_ai and f.get("dry_up", 0) and sub["chart"] >= 0.35 and composite >= 0.43:
        return "C", "C_ai_dryup"
    if strong_material and fair_chart and composite >= 0.45:
        return "C", "C_material_chart"

    # D: 面白いが不足
    if composite >= 0.38 or strong_material or good_chart or strong_ai:
        return "D", "D"
    return "E", "E"


def _reasons(sub: dict, f: dict, upside: float, extra: dict | None = None) -> list[str]:
    r = []
    info = extra or {}

    # 材料: カテゴリ名・タイトルを表示
    if sub["material"] >= 0.5:
        cat = info.get("top_category", "")
        title = info.get("top_title", "")
        cat_label = f"【{cat}】" if cat else "好材料"
        title_part = f" 「{title[:30]}…」" if title else ""
        r.append(f"材料: {cat_label}{title_part} (スコア{sub['material']:.2f}) — 未織り込み・接続度あり")
    elif sub["material"] >= 0.3:
        cat = info.get("top_category", "")
        r.append(f"材料あり(スコア{sub['material']:.2f})" + (f": 【{cat}】" if cat else "。続報・出来高連動を確認中"))

    # チャート
    if f.get("broke_resistance", 0):
        r.append("チャート: 抵抗線を上抜け(ブレイクアウト確認済み)")
    elif f.get("near_breakout", 0) > 0.5:
        r.append("チャート: 抵抗線まで5%以内 — ブレイク間近")
    if f.get("downtrend_stopped", 0) > 0 and f.get("volatility_contraction", 0) > 0.2:
        r.append("チャート: 下落止まり→ボラ縮小→横ばい(底固め形)")
    if f.get("higher_lows", 0) > 0.05:
        r.append("チャート: 安値切り上げを確認(上昇転換シグナル)")

    # 出来高
    spike = f.get("vol_spike", 1.0)
    bias = f.get("up_down_vol_bias", 0.0)
    if spike > 2.0 and bias > 0:
        r.append(f"出来高: 急増(平均比{spike:.1f}倍)を上昇日に伴う → 買い主導の初動示唆")
    elif spike > 1.4 and bias > 0:
        r.append(f"出来高: やや増加(平均比{spike:.1f}倍)で上昇優位")
    if f.get("dry_up", 0):
        r.append("出来高: 売り枯れ(出来高減+価格維持) → 次の材料で点火しやすい")

    # テーマ地合い: ETF名を明示
    themes = info.get("themes_matched", [])
    if themes and sub["theme"] >= 0.4:
        r.append(f"テーマ地合い良好(スコア{sub['theme']:.2f}): {'/'.join(themes[:3])} — ETF/指数で客観確認済み")
    elif sub["theme"] >= 0.3:
        r.append(f"テーマ地合いやや優位(スコア{sub['theme']:.2f})")

    # ML/類似度
    if sub["similarity"] >= 0.5:
        r.append(f"AI類似: 過去急騰前パターンと高類似(スコア{sub['similarity']:.2f}) — モデルが複数類似例を確認")
    elif sub["similarity"] >= 0.3:
        r.append(f"AI類似: 過去急騰前パターンと中程度類似(スコア{sub['similarity']:.2f})")

    # 上値余地
    if upside >= 0.7:
        r.append("上値: 抵抗線距離・初動性から+20%の現実到達余地が高い")
    elif upside >= 0.5:
        r.append("上値: 抵抗線距離から+20%到達の余地あり")

    if not r:
        r.append("突出した火種は弱め。監視レベル")
    return r


def _failure_conditions(f: dict, sub: dict) -> list[str]:
    """これが崩れたら撤退(失敗条件)。追跡時の failure タグ判定にも対応。

    2026-09-17: 「支持線割れ」等の定型文だけでは実際にどの価格を見ればよいか
    分からないため、算出済みの価格水準(直近10本の安値=Failure Line、60本高値
    =抵抗線)を具体的な数字で示す。
    """
    c = []
    fl = f.get("_failure_line_price", 0.0)
    fd = f.get("_failure_distance", 0.0)
    if fl > 0 and fd > 0:
        fl_date = f.get("_failure_line_date", "")
        when = f"({fl_date[5:]}安値)" if fl_date else ""
        c.append(f"¥{fl:,.0f}{when}割れ = 現値から-{fd*100:.1f}% "
                 f"→ quick_fail/trend_fail")
    else:
        c.append("予測時終値を明確に下回る/支持線割れ → quick_fail/trend_fail")
    if sub["material"] >= 0.3:
        c.append("材料の続報が出ず出来高が続かない → material_fail")
    if f.get("vol_spike", 1) > 1.5:
        c.append("出来高急増が初動でなく上ヒゲ天井だった → volume_fail")
    if sub["theme"] >= 0.4:
        c.append("テーマ資金が波及せずリーダー株のみ → theme_fail")
    if f.get("near_breakout", 0) > 0.5:
        res = f.get("_resistance_price", 0.0)
        where = f"(¥{res:,.0f})" if res > 0 else ""
        c.append(f"抵抗線{where}を上抜けできず戻り売り → chart_fail")
    c.append("地合い急悪化 → market_fail / 希薄化発表 → dilution_fail")
    return c
