"""材料ラベル(material-v1)の語彙。docs/LABELS.md の 3 章と一致させること。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))

MATERIAL_VERSION = "material-v1"

EVENT_TYPES = {
    "業績実績", "業績見通し", "商取引・顧客", "提携・アライアンス", "製品・サービス・技術",
    "生産・設備・供給", "M&A・事業再編", "規制・承認", "臨床・研究", "法務・知財",
    "資金調達・株式供給", "株主還元・資本構成", "上場・指数・市場制度", "経営・ガバナンス・人事",
    "政策・財政", "金融・マクロ", "地政学・貿易", "商品・外部価格", "セクター・同業波及", "その他",
}
ACTORS = {
    "当該企業", "顧客", "取引先", "パートナー", "競合企業", "同業企業", "供給者", "政府",
    "規制当局", "中央銀行", "裁判所", "国際機関", "取引所", "Index提供者", "市場", "その他",
}
PATHWAYS = {
    "売上", "受注残", "販売数量", "販売価格", "市場需要", "利益率", "原材料コスト", "人件費",
    "金利負担", "資金調達", "現金・流動性", "生産能力", "供給能力", "市場アクセス", "規制承認",
    "知的財産", "法的負担", "資産価値", "株式供給", "Float", "株主構成", "支配権", "割引率",
    "取引需給", "その他",
}
SCOPES = {"企業固有", "企業間", "サブ業界", "業界", "セクター", "国内マクロ", "グローバルマクロ"}
STAGES = {"報道・観測", "計画・提案", "正式発表", "契約締結", "決定", "承認", "実施開始", "完了",
          "実績・結果判明"}

# 価値判断の語は材料ラベルに入れない(facts に紛れ込んだら保存を止める)
VALUE_WORDS = ("好材料", "悪材料", "強い材料", "弱い材料", "超好材料", "爆上げ")


def timing(published_at: datetime | None, trading_days: set[str] | None = None) -> str:
    """公開日時(JST)から公開タイミングを決める。東証 9:00〜15:30。"""
    if published_at is None:
        return "公開時刻不明"
    d = published_at.strftime("%Y-%m-%d")
    # 平日の祝日は、価格データのある期間内でだけ判定できる(まだ来ていない日は分からない)
    holiday = bool(trading_days) and d <= max(trading_days) and d not in trading_days
    if published_at.weekday() >= 5 or holiday:
        return "非営業日"
    hm = published_at.hour * 60 + published_at.minute
    if hm < 9 * 60:
        return "寄り前"
    if hm <= 15 * 60 + 30:
        return "市場時間中"
    return "引け後"


def window_status(pub: datetime | None, date: str, tp: datetime | None, tn: datetime) -> str:
    """見出し 1 件が Material Window 内か。new / background / time_unknown / after_t_now。

    Material Window: T_prev < 公開時刻 <= T_now の見出しだけが新規材料。
    T_prev が無い(前回分析日時不明)ときは、T_now と同じ日(JST)に公開されたものだけを暫定の新規材料とする。
    日付しか分からない見出しは、T_prev の日付より後なら新規、同じ日なら time_unknown(新規に数えない)。
    """
    tn_date = tn.strftime("%Y-%m-%d")
    if pub is not None:
        pub = pub.astimezone(JST)
        if pub > tn:
            return "after_t_now"
        lo = tp if tp is not None else tn.replace(hour=0, minute=0, second=0, microsecond=0)
        return "new" if pub > lo else "background"
    # 日付しか分からない見出し
    if date > tn_date:
        return "after_t_now"
    if tp is None:
        return "new" if date == tn_date else "background"
    tp_date = tp.strftime("%Y-%m-%d")
    if date > tp_date:
        return "new"
    if date == tp_date:
        return "time_unknown"
    return "background"
