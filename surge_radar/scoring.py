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

# サブスコア重み (相対評価の基礎)
WEIGHTS = {
    "material": 0.26,
    "chart": 0.22,
    "volume": 0.22,
    "theme": 0.12,
    "similarity": 0.12,
    "fundamental": 0.06,
}


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def chart_score(f: dict) -> float:
    """理想形(下落止まり→横ばい→安値切り上げ→ブレイク)に近いほど高い。"""
    s = 0.0
    s += 0.18 * _clip01(f.get("downtrend_stopped", 0) + 0.5)   # 下落止まり
    s += 0.14 * _clip01(f.get("volatility_contraction", 0))     # ボラ縮小
    s += 0.12 * _clip01(f.get("sideways", 0))                   # 横ばい化
    s += 0.16 * _clip01(f.get("higher_lows", 0) + 0.3)          # 安値切り上げ
    s += 0.10 * _clip01(f.get("lower_highs_stopped", 0) + 0.3)  # 高値切り下げ停止
    s += 0.16 * _clip01(f.get("near_breakout", 0))              # ブレイク接近
    s += 0.08 * f.get("broke_resistance", 0)                    # 抵抗線突破
    s += 0.06 * f.get("price_above_ma25", 0)
    # 25日線が上向きでその上にいる
    if f.get("ma25_slope", 0) > 0 and f.get("price_above_ma25", 0):
        s += 0.05
    # リスク減点
    s -= 0.30 * _clip01(f.get("downtrend_risk", 0))            # 右肩下がり
    s -= 0.15 * f.get("rebound_capped", 0)                     # 戻り売り
    s -= 0.15 * f.get("high_zone_upper_wick", 0)               # 高値圏上ヒゲ
    return _clip01(s)


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
    if not f.get("liquidity_ok", 0) and f.get("turnover_log", 0) < 7.0:
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
    return _clip01(s)


def score_candidate(f: dict, ml_prob: float | None = None,
                    similarity: float | None = None,
                    extra_info: dict | None = None) -> dict:
    """
    1銘柄のフルスコアリング。サブスコア・総合・分類・理由・失敗条件を返す。
    extra_info: {"top_category", "top_title", "themes_matched", "name"} を受け取ると
    reasons の文章が具体的になる。
    """
    sub = {
        "material": material_score(f),
        "chart": chart_score(f),
        "volume": volume_score(f),
        "theme": theme_score(f),
        "similarity": float(similarity) if similarity is not None else 0.0,
        "fundamental": fundamental_score(f),
    }
    gates = exclusion_gates(f)
    upside = realistic_upside(f)

    weighted = sum(WEIGHTS[k] * sub[k] for k in WEIGHTS)
    top = max(sub["material"], sub["chart"], sub["volume"], sub["theme"], sub["similarity"])  # 火種
    prob = float(ml_prob) if ml_prob is not None else weighted
    sub["probability"] = round(prob, 4)  # _classify で高確率判定に使用

    # 総合: 相対(weighted) + 不確実性込みML + 火種(突出) + 現実到達余地
    composite = (0.42 * weighted + 0.28 * prob + 0.18 * top + 0.12 * upside)

    # リスク減衰
    risk = 0.0
    risk += 0.20 * f.get("downtrend_risk", 0)
    risk += 0.10 * f.get("rebound_capped", 0)
    risk += 0.10 * f.get("volume_top_risk", 0)
    composite *= (1 - min(risk, 0.5))

    # ゲート: 該当で大幅減点(完全破綻は実質除外)
    if gates:
        composite *= 0.25

    composite = _clip01(composite)
    category, classify_path = _classify(sub, f, gates, upside, composite)
    reasons = _reasons(sub, f, upside, extra_info)
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
    }


def _classify(sub: dict, f: dict, gates: list[str], upside: float, composite: float) -> tuple[str, str]:
    """カテゴリと分類パス名(B/C条件追跡用)を返す。"""
    if gates:
        return "E", "E_gate"
    strong_material   = sub["material"] >= 0.5
    decent_material   = sub["material"] >= 0.3
    good_chart        = sub["chart"] >= 0.55
    fair_chart        = sub["chart"] >= 0.40
    good_volume       = sub["volume"] >= 0.50
    decent_volume     = sub["volume"] >= 0.35
    strong_ai         = sub["similarity"] >= 0.68
    very_strong_ai    = sub["similarity"] >= 0.78
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
    """これが崩れたら撤退(失敗条件)。追跡時の failure タグ判定にも対応。"""
    c = []
    c.append("予測時終値を明確に下回る/支持線割れ → quick_fail/trend_fail")
    if sub["material"] >= 0.3:
        c.append("材料の続報が出ず出来高が続かない → material_fail")
    if f.get("vol_spike", 1) > 1.5:
        c.append("出来高急増が初動でなく上ヒゲ天井だった → volume_fail")
    if sub["theme"] >= 0.4:
        c.append("テーマ資金が波及せずリーダー株のみ → theme_fail")
    if f.get("near_breakout", 0) > 0.5:
        c.append("抵抗線を上抜けできず戻り売り → chart_fail")
    c.append("地合い急悪化 → market_fail / 希薄化発表 → dilution_fail")
    return c
