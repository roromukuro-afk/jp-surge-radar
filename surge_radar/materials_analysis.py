"""
材料の本番品質分析 (LLM不使用・ルールベース)。

材料を「件数」でなく「質」で評価するための派生フィールドを生成する:
  - material_type : 正規化した材料種別 (上方修正/大型受注/提携/増資 ...)
  - unpriced      : 未織り込み感 (サプライズ性が高いほど高い)
  - connection    : 銘柄接続度 (公式開示=高, 市場/テーマ言及=低)
  - chart_reaction: 材料後のチャート反応 (価格が伸びたか)
  - volume_reaction: 材料後の出来高反応 (出来高が伴ったか)
  - risk          : 出尽くし/希薄化リスク (高いほど危険)
  - ai_comment    : 上記を統合した日本語の一言コメント

設計方針:
  - 公式開示 (TDnet/EDINET) は接続度・信頼度が高い。Kabutan/Yahoo は見出し中心で接続度はやや低い。
  - 「材料が出た後に株価・出来高が反応したか」を prices から事後計算し、
    出尽くし (反応済みで上ヒゲ) を検出する。
  - スコアは件数でなく「未織り込み × 持続性 × 接続度 × 反応」で材料の強さを表す。
"""
from __future__ import annotations

# material_type -> (unpriced_base, persistence_base, dilution_risk, direction)
# unpriced: サプライズ性 / dilution_risk: 希薄化リスク / direction: +1好材料 -1悪材料
_TYPE_ATTRS: dict[str, tuple[float, float, float, int]] = {
    "上方修正":       (0.85, 0.70, 0.0, +1),
    "業績予想修正":   (0.55, 0.50, 0.0, +1),
    "過去最高益":     (0.70, 0.70, 0.0, +1),
    "黒字転換":       (0.80, 0.70, 0.0, +1),
    "増配":           (0.55, 0.65, 0.0, +1),
    "復配":           (0.65, 0.65, 0.0, +1),
    "自社株買い":     (0.65, 0.55, 0.0, +1),
    "株式分割":       (0.45, 0.40, 0.0, +1),
    "大型受注":       (0.90, 0.80, 0.0, +1),
    "受注":           (0.70, 0.65, 0.0, +1),
    "資本業務提携":   (0.85, 0.75, 0.0, +1),
    "提携":           (0.70, 0.65, 0.0, +1),
    "M&A・買収":      (0.80, 0.65, 0.0, +1),
    "TOB":            (0.90, 0.55, 0.0, +1),
    "新製品・新サービス": (0.55, 0.55, 0.0, +1),
    "薬事承認":       (0.88, 0.75, 0.0, +1),
    "特許":           (0.55, 0.60, 0.0, +1),
    "補助金・採択":   (0.60, 0.60, 0.0, +1),
    "受賞":           (0.40, 0.40, 0.0, +1),
    "月次":           (0.45, 0.55, 0.0, +1),
    "決算":           (0.40, 0.45, 0.0, +1),
    # ネガティブ / 希薄化
    "下方修正":       (0.85, 0.70, 0.0, -1),
    "減配・無配":     (0.70, 0.60, 0.0, -1),
    "新株予約権":     (0.55, 0.55, 0.85, -1),
    "第三者割当":     (0.55, 0.55, 0.80, -1),
    "公募増資":       (0.65, 0.65, 0.90, -1),
    "ワラント":       (0.55, 0.55, 0.80, -1),
    "継続企業の前提": (0.85, 0.80, 0.50, -1),
    "特別損失":       (0.55, 0.40, 0.0, -1),
}

# タイトルから material_type を引くためのキーワード -> type
_TYPE_KEYWORDS: list[tuple[str, str]] = [
    ("上方修正", "上方修正"), ("業績予想の修正", "業績予想修正"), ("通期予想", "業績予想修正"),
    ("過去最高", "過去最高益"), ("最高益", "過去最高益"),
    ("黒字転換", "黒字転換"), ("黒字化", "黒字転換"),
    ("増配", "増配"), ("復配", "復配"),
    ("自己株式の取得", "自社株買い"), ("自己株式取得", "自社株買い"), ("自社株買", "自社株買い"),
    ("株式分割", "株式分割"),
    ("大型受注", "大型受注"), ("大口受注", "大型受注"),
    ("受注", "受注"),
    ("資本業務提携", "資本業務提携"), ("業務資本提携", "資本業務提携"),
    ("資本提携", "資本業務提携"),
    ("業務提携", "提携"), ("提携", "提携"),
    ("TOB", "TOB"), ("公開買付", "TOB"), ("株式公開買付", "TOB"),
    ("M&A", "M&A・買収"), ("買収", "M&A・買収"), ("子会社化", "M&A・買収"),
    ("新製品", "新製品・新サービス"), ("新サービス", "新製品・新サービス"), ("新商品", "新製品・新サービス"),
    ("薬事", "薬事承認"), ("承認申請", "薬事承認"), ("承認取得", "薬事承認"),
    ("製造販売承認", "薬事承認"), ("治験", "薬事承認"), ("第III相", "薬事承認"),
    ("特許", "特許"),
    ("補助金", "補助金・採択"), ("採択", "補助金・採択"), ("交付決定", "補助金・採択"),
    ("受賞", "受賞"), ("表彰", "受賞"),
    ("月次", "月次"),
    ("決算", "決算"), ("四半期", "決算"), ("本決算", "決算"), ("中間決算", "決算"),
    ("下方修正", "下方修正"),
    ("減配", "減配・無配"), ("無配", "減配・無配"),
    ("新株予約権", "新株予約権"), ("ストックオプション", "新株予約権"),
    ("第三者割当", "第三者割当"),
    ("公募増資", "公募増資"), ("公募による", "公募増資"),
    ("ワラント", "ワラント"),
    ("継続企業", "継続企業の前提"), ("ゴーイングコンサーン", "継続企業の前提"),
    ("特別損失", "特別損失"), ("特損", "特別損失"),
]


def classify_type(title: str, body: str = "", fallback_category: str = "") -> str:
    """タイトル/本文から正規化 material_type を返す。該当なしは ''。"""
    text = f"{title} {body} {fallback_category}"
    for kw, mtype in _TYPE_KEYWORDS:
        if kw in text:
            return mtype
    return ""


def base_connection(source: str, title: str, code: str = "", name: str = "") -> float:
    """銘柄接続度: 公式開示ほど高い。市場・テーマ言及は低い。"""
    s = (source or "").lower()
    if s in ("tdnet", "edinet"):
        conn = 1.0  # 当該企業の公式開示
    elif s == "kabutan":
        conn = 0.8  # 銘柄ニュースページ由来
    elif "yahoo" in s:
        conn = 0.7
    else:
        conn = 0.6
    # タイトルに「市場」「業界」「セクター」など全体言及が強いと接続度を下げる
    if any(k in (title or "") for k in ["業界", "セクター", "市場全体", "日経平均", "相場", "見通し", "ランキング"]):
        conn = min(conn, 0.5)
    return round(conn, 3)


def analyze(title: str, *, body: str = "", source: str = "", code: str = "",
            name: str = "", fallback_category: str = "") -> dict:
    """材料1件の派生フィールド (reaction除く) を返す。"""
    mtype = classify_type(title, body, fallback_category)
    attrs = _TYPE_ATTRS.get(mtype)
    if attrs is None:
        # 未分類: 見出しのみ。弱い好材料として最小限に扱う
        unpriced = 0.35
        persistence = 0.35
        dilution = 0.0
        direction = +1
    else:
        unpriced, persistence, dilution, direction = attrs
    connection = base_connection(source, title, code, name)
    impact = round(min(unpriced * (0.6 + 0.4 * connection), 1.0), 3)
    sentiment = round(direction * impact, 3)
    return {
        "material_type": mtype,
        "unpriced": round(unpriced, 3),
        "persistence": round(persistence, 3),
        "connection": connection,
        "impact": impact,
        "sentiment": sentiment,
        "dilution_risk": round(dilution, 3),
        "direction": direction,
    }


def compute_reactions(prices: list[dict] | None, material_date: str,
                      win: int = 5) -> dict:
    """材料日の前後から chart/volume 反応と出尽くしリスクを計算。

    prices: [{date, high, low, close, volume}, ...] 昇順。None/不足時は0。
    chart_reaction : 材料後win日の高値が材料前終値からどれだけ上げたか (0..1)
    volume_reaction: 材料後win日の平均出来高 / 材料前20日平均 (0..1 に正規化)
    exhaust_risk   : 上げた後に上ヒゲで失速 = 出尽くしリスク (0..1)
    """
    out = {"chart_reaction": 0.0, "volume_reaction": 0.0, "exhaust_risk": 0.0,
           "reaction_known": 0}
    if not prices:
        return out
    # 材料日の位置 (材料日以前で最も近い足)
    idx = None
    for i, b in enumerate(prices):
        if b["date"] <= material_date:
            idx = i
        else:
            break
    if idx is None or idx < 1:
        return out
    base_close = prices[idx]["close"] or 0
    if base_close <= 0:
        return out
    after = prices[idx + 1: idx + 1 + win]
    if not after:
        return out  # まだ反応期間が来ていない
    out["reaction_known"] = 1
    hi = max((b["high"] or 0) for b in after)
    up = (hi - base_close) / base_close
    # +20%で1.0に飽和
    out["chart_reaction"] = round(max(0.0, min(up / 0.20, 1.0)), 3)
    # 出来高反応
    before = prices[max(0, idx - 20): idx + 1]
    vb = [b["volume"] or 0 for b in before]
    va = [b["volume"] or 0 for b in after]
    avg_b = (sum(vb) / len(vb)) if vb else 0
    avg_a = (sum(va) / len(va)) if va else 0
    if avg_b > 0:
        ratio = avg_a / avg_b
        out["volume_reaction"] = round(max(0.0, min((ratio - 1.0) / 2.0, 1.0)), 3)
    # 出尽くし: 上げ(up>5%)+出来高増の後、最終終値が高値から大きく押している
    last_close = after[-1]["close"] or 0
    if up > 0.05 and hi > 0:
        fade = (hi - last_close) / hi
        if fade > 0.5 * up:  # 上げ幅の半分以上を吐き出した
            out["exhaust_risk"] = round(min(fade / max(up, 0.01), 1.0), 3)
    return out


def make_ai_comment(a: dict, reactions: dict) -> str:
    """material_type + 各スコアから日本語コメントを組み立てる (ルールベース)。"""
    mtype = a.get("material_type") or "材料"
    parts: list[str] = [mtype]
    unp = a.get("unpriced", 0)
    conn = a.get("connection", 0)
    dil = a.get("dilution_risk", 0)
    cr = reactions.get("chart_reaction", 0)
    vr = reactions.get("volume_reaction", 0)
    ex = reactions.get("exhaust_risk", 0)
    known = reactions.get("reaction_known", 0)

    if unp >= 0.7:
        parts.append("サプライズ性高")
    elif unp >= 0.5:
        parts.append("やや未織り込み")
    if conn >= 0.9:
        parts.append("公式開示で銘柄直結")
    elif conn < 0.6:
        parts.append("接続度やや低")
    if dil >= 0.7:
        parts.append("希薄化リスク大")
    if a.get("direction", 1) < 0:
        parts.append("悪材料寄り")

    if known:
        if cr >= 0.5 and vr >= 0.4:
            parts.append("出来高伴い株価反応あり")
        elif cr >= 0.5 and vr < 0.4:
            parts.append("出来高薄く反応に持続性不安")
        elif cr < 0.2:
            parts.append("株価反応は限定的")
        if ex >= 0.5:
            parts.append("上ヒゲ失速=出尽くし警戒")
    else:
        parts.append("反応はこれから")

    return " / ".join(parts)
