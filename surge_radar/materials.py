"""
材料分析 (ルールベース + 軽量NLP)。

方針:
- 材料は「存在するだけ」では高評価にしない。重視するのは未織り込み感・持続性・株価インパクト・
  銘柄接続度・続報余地・出来高/チャート反応・出尽くしリスク。
- v1 はキーワード辞書 + 開示種別 + 価格/出来高反応で 0..1 のサブスコア化。
- 上位候補のみ後で LLM 深掘り (analyze_with_llm フック) を呼べる構造。
- 無料データ源: yanoshin TDnet WebAPI (登録不要 JSON)。取得失敗時は価格反応からの近似に縮退。

レート制限対策:
- 8時間ファイルキャッシュで同日複数回呼び出しを吸収。
- 差分取得: DB最新材料日を確認し未取得分のみAPIリクエスト。
- 指数バックオフ: 429/タイムアウト時に 2^i 秒待機。
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

from . import db

TDNET_API = "https://webapi.yanoshin.jp/webapi/tdnet/list/{q}.json"
TDNET_RANGE = "https://webapi.yanoshin.jp/webapi/tdnet/list/{f}-{t}.json"
EDINET_API = "https://disclosure.edinet-fsa.go.jp/api/v2/documents.json"
YAHOO_NEWS_API = "https://query2.finance.yahoo.com/v1/finance/search"  # 日本株では英語ニュースが返るため実質未使用

# カテゴリ -> (株価インパクト基礎, 持続性基礎, 方向[+1/-1])
CATEGORY_KEYWORDS: dict[str, tuple[float, float, int]] = {
    "上方修正": (0.9, 0.7, +1),
    "業績予想の修正": (0.6, 0.5, +1),
    "過去最高": (0.7, 0.7, +1),
    "黒字転換": (0.8, 0.7, +1),
    "黒字化": (0.8, 0.7, +1),
    "増配": (0.6, 0.6, +1),
    "復配": (0.6, 0.6, +1),
    "自己株式取得": (0.7, 0.5, +1),  # 自社株買い(買付・取得)
    "自社株買": (0.7, 0.5, +1),
    "自己株式処分": (0.55, 0.3, -1),  # 処分は希薄化方向(報酬・割当)
    "株式分割": (0.5, 0.4, +1),
    "受注": (0.8, 0.7, +1),
    "大型受注": (0.9, 0.8, +1),
    "提携": (0.7, 0.6, +1),
    "資本業務提携": (0.8, 0.7, +1),
    "M&A": (0.7, 0.6, +1),
    "買収": (0.7, 0.6, +1),
    "TOB": (0.9, 0.5, +1),
    "公開買付": (0.9, 0.5, +1),
    "新製品": (0.6, 0.6, +1),
    "新サービス": (0.5, 0.5, +1),
    "承認": (0.85, 0.7, +1),
    "認可": (0.8, 0.7, +1),
    "薬事": (0.8, 0.7, +1),
    "特許": (0.6, 0.6, +1),
    "補助金": (0.6, 0.6, +1),
    "採択": (0.6, 0.6, +1),
    "受賞": (0.4, 0.4, +1),
    "月次": (0.4, 0.5, +1),
    # ネガティブ
    "下方修正": (0.9, 0.7, -1),
    "減配": (0.6, 0.6, -1),
    "無配": (0.7, 0.6, -1),
    "新株予約権": (0.7, 0.6, -1),   # 希薄化
    "第三者割当": (0.7, 0.6, -1),
    "公募増資": (0.8, 0.7, -1),
    "ワラント": (0.7, 0.6, -1),
    "希薄化": (0.8, 0.7, -1),
    "継続企業": (0.9, 0.8, -1),     # 継続企業の前提注記
    "特別損失": (0.6, 0.4, -1),
    "業績下振れ": (0.7, 0.6, -1),
}

# 事故/事件系の語。「受注」「提携」等の好材料キーワードが本文中に偶発的に
# 含まれていても(例:「工事受注会社」=事故を起こした施工業者、を指す用法)、
# 好材料として扱わないための除外リスト(2026-08-28追加)。
_INCIDENT_OVERRIDE_KEYWORDS = (
    "転落事故", "死亡事故", "業務上過失致死", "家宅捜索", "逮捕", "起訴", "送検",
    "労災", "死傷", "遺体", "不祥事", "検察", "書類送検",
)

# 2026-09-17判明: 3565(アセンテック)の「オリックス、アセンテックのTOB不成立
# 子会社化断念」が、本文中の「TOB」に単純キーワードマッチして
# sentiment=+0.75/impact=0.9 の好材料【TOB】に誤分類されていた(「不成立」
# 「断念」という失敗を示す語を一切見ていないため)。TOB/M&A/受注/承認等の
# "プロセス系"好材料キーワードは、不成立・否決等の語と同時に出現した場合、
# 方向を反転させる(成立/獲得できなかった、の意味になるため)。
_PROCESS_FAILURE_KEYWORDS = (
    "不成立", "不採択", "否決", "破談", "白紙撤回", "断念", "撤回", "不承認", "却下",
)
_PROCESS_KEYWORDS = {
    "TOB", "公開買付", "M&A", "買収", "提携", "資本業務提携", "受注", "大型受注",
    "承認", "認可", "薬事", "特許", "採択",
}

# テーマ語彙 (マクロ/業界)。テーマ地合いと併用。
THEME_KEYWORDS = {
    "半導体": ["半導体", "ウエハ", "後工程", "前工程", "ファウンドリ", "SoC", "メモリ"],
    "AI": ["AI", "人工知能", "生成AI", "LLM", "機械学習"],
    "データセンター": ["データセンター", "DC", "サーバ", "液冷"],
    "防衛": ["防衛", "防衛費", "ミサイル", "装備"],
    "原子力": ["原子力", "原発", "SMR", "核燃料"],
    "宇宙": ["宇宙", "衛星", "ロケット"],
    "量子": ["量子"],
    "ロボット": ["ロボット", "ヒューマノイド", "FA"],
    "サイバー": ["セキュリティ", "サイバー", "ゼロトラスト"],
    "GX": ["脱炭素", "GX", "再エネ", "水素", "ペロブスカイト"],
    "インバウンド": ["インバウンド", "訪日"],
}


def classify_material(title: str, body: str = "") -> dict:
    """開示/ニュースの本文から方向・インパクト・持続性を推定。"""
    text = f"{title} {body}"
    hits: list[tuple[str, float, float, int]] = []
    for kw, (imp, per, direction) in CATEGORY_KEYWORDS.items():
        if kw in text:
            hits.append((kw, imp, per, direction))
    themes = [t for t, kws in THEME_KEYWORDS.items() if any(k in text for k in kws)]

    # 2026-08-28判明: 1433(ベステラ)の「川崎転落事故、工事受注会社の支店を
    # 家宅捜索　業務上過失致死容疑」が、本文中の「工事受注会社」の「受注」に
    # 単純キーワードマッチして sentiment=+0.75/impact=0.8 の好材料【受注】に
    # 誤分類されていた(死亡事故・家宅捜索という強いネガティブ文脈を一切見ていない
    # ため)。事故/事件系の語が含まれる場合は、偶発的に一致した好材料方向の
    # キーワードを無視する。
    if any(k in text for k in _PROCESS_FAILURE_KEYWORDS):
        hits = [
            (kw, imp, per, -1) if kw in _PROCESS_KEYWORDS and direction > 0
            else (kw, imp, per, direction)
            for kw, imp, per, direction in hits
        ]

    if any(k in text for k in _INCIDENT_OVERRIDE_KEYWORDS):
        hits = [h for h in hits if h[3] < 0]

    if not hits:
        return {"category": "", "impact": 0.0, "persistence": 0.0,
                "sentiment": 0.0, "themes": themes, "matched": []}

    # 最も強い材料を主、複数なら少し加点
    hits.sort(key=lambda x: x[1], reverse=True)
    main = hits[0]
    impact = min(main[1] + 0.05 * (len(hits) - 1), 1.0)
    persistence = main[2]
    sentiment = float(main[3]) * min(0.5 + 0.25 * len(hits), 1.0)
    return {
        "category": main[0],
        "impact": round(impact, 3),
        "persistence": round(persistence, 3),
        "sentiment": round(sentiment, 3),
        "themes": themes,
        "matched": [h[0] for h in hits],
    }


# ---------- キャッシュヘルパ ----------

def _cache_dir() -> Path:
    from .config import CACHE_DIR
    d = Path(CACHE_DIR) / "tdnet"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cache_file(from_str: str, to_str: str) -> Path:
    return _cache_dir() / f"{from_str}_{to_str}.json"


def _load_cache(path: Path, max_age_hours: float = 8.0) -> dict | None:
    if not path.exists():
        return None
    if time.time() - path.stat().st_mtime > 3600 * max_age_hours:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(path: Path, data: dict) -> None:
    try:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------- HTTP ----------

def _norm_code(raw: str) -> str:
    """yanoshin の company_code(5桁・末尾0等)を4桁証券コードへ。"""
    s = str(raw).strip()
    if len(s) == 5 and s.endswith("0"):
        return s[:4]
    return s[:4]


def _get_json(url: str, params: dict, retries: int = 2, timeout: int = 15,
              base_pause: float = 0.5) -> dict | None:
    """GET with capped retry. 2 retries max, linear backoff to avoid long hangs."""
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0 (surge-radar/1.0)"})
            if r.status_code == 429:
                wait = min(base_pause * (i + 1), 5.0)
                print(f"    [mat] 429 rate-limit wait {wait:.0f}s", flush=True)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                time.sleep(base_pause)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.Timeout:
            print(f"    [mat] timeout retry {i+1}/{retries}", flush=True)
            time.sleep(base_pause)
        except requests.exceptions.ConnectionError:
            time.sleep(base_pause)
        except Exception:
            time.sleep(base_pause)
    return None


# ---------- TDnet 個別銘柄 ----------

def fetch_tdnet(code: str, days: int = 30, limit: int = 50) -> list[dict]:
    """個別銘柄の直近開示(フォールバック用)。"""
    d = _get_json(TDNET_API.format(q=code), {"limit": limit}, retries=2, timeout=15)
    if not d:
        return []
    cutoff = datetime.now() - timedelta(days=days)
    out = []
    for it in d.get("items", []):
        td = it.get("Tdnet", it)
        pubdate = td.get("pubdate") or ""
        try:
            dt = datetime.strptime(pubdate[:10], "%Y-%m-%d")
        except Exception:
            dt = None
        if dt and dt < cutoff:
            continue
        out.append({"date": dt.strftime("%Y-%m-%d") if dt else "",
                    "title": td.get("title", ""), "url": td.get("document_url", ""),
                    "source": "tdnet"})
    return out


# ---------- TDnet 日付範囲一括取得 ----------

def last_materials_date() -> str | None:
    """DB内の最新材料日付。差分取得の起点として使用。"""
    with db.cursor() as conn:
        r = conn.execute("SELECT MAX(date) d FROM materials").fetchone()
    return r["d"] if r and r["d"] else None


def fetch_tdnet_range(days: int = 14, max_pages: int = 5, per_page: int = 200,
                      pause: float = 0.3, since_date: str | None = None,
                      time_limit_s: float = 120.0) -> dict[str, list[dict]]:
    """
    日付範囲で全開示をまとめて取得し、証券コード -> 開示リスト の辞書を返す。

    since_date を指定すると days は無視されその日以降を取得。
    8時間ファイルキャッシュで同日複数回呼び出し・レート制限を回避。
    """
    to = datetime.now()
    if since_date:
        frm = datetime.strptime(since_date, "%Y-%m-%d")
    else:
        frm = to - timedelta(days=days)

    from_str = frm.strftime("%Y%m%d")
    to_str = to.strftime("%Y%m%d")

    # キャッシュ確認(8時間以内の同一範囲)
    cf = _cache_file(from_str, to_str)
    cached = _load_cache(cf)
    if cached is not None:
        n = sum(len(v) for v in cached.values())
        print(f"    [TDnet] cache hit {from_str}-{to_str}: {n} disclosures, {len(cached)} codes")
        return cached

    by_code: dict[str, list[dict]] = {}
    url = TDNET_RANGE.format(f=from_str, t=to_str)
    total_items = 0
    t_start = time.monotonic()

    for page in range(1, max_pages + 1):
        if time.monotonic() - t_start > time_limit_s:
            print(f"    [TDnet] time limit {time_limit_s:.0f}s reached at page {page}, stopping", flush=True)
            break
        d = _get_json(url, {"limit": per_page, "page": page})
        if not d:
            print(f"    [TDnet] page {page} failed. Stopping.", flush=True)
            break
        items = d.get("items", [])
        if not items:
            break
        for it in items:
            td = it.get("Tdnet", it)
            code = _norm_code(td.get("company_code", ""))
            if not code.isdigit():
                continue
            pubdate = td.get("pubdate") or ""
            try:
                dt = datetime.strptime(pubdate[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
            except Exception:
                dt = ""
            by_code.setdefault(code, []).append({
                "date": dt, "title": td.get("title", ""),
                "url": td.get("document_url", ""), "source": "tdnet"})
        total_items += len(items)
        print(f"    [TDnet] page {page}: {len(items)} items (total {total_items})", flush=True)
        if len(items) < per_page:
            break
        time.sleep(pause)

    if by_code:
        _save_cache(cf, by_code)
        print(f"    [TDnet] fetched {total_items} disclosures, {len(by_code)} codes → cached")
    else:
        print(f"    [TDnet] 0 items fetched (rate limited or no data for range {from_str}-{to_str})")

    return by_code


# ---------- DB保存 ----------

# Yahoo!JP/日経の銘柄別ニュースページには、その銘柄と無関係な市場全体の
# ランキング・市況ダイジェスト記事が関連コンテンツとして混在表示される
# (2026-08-25判明: 3276(JPMC)に「出来高変化率ランキング〜ファンディーノ、
# 大豊工業などがランクイン」、7356(Retty)に「前場のランキング【値上がり率】」
# が「その銘柄の材料」として保存されており、直近材料の5.8%・417銘柄に影響、
# 本日のA/B候補の8/81で最高スコア材料がこの種の無関係記事だった)。
# タイトルにその銘柄名が一切含まれない市場全体ダイジェストは、個別銘柄の
# 材料として評価する意味がないため保存段階で除外する。
_GENERIC_DIGEST_TITLE_PATTERNS = (
    "ランキング", "値上がり率", "値下がり率", "出来高変化率",
    "東証グロース（大引け）", "東証グロース（前引け）",
    "東証プライム（大引け）", "東証プライム（前引け）",
    "東証スタンダード（大引け）", "東証スタンダード（前引け）",
    "東京株式（", "日経平均株価", "日経平均先物",
    # 2026-08-27追加: 2484(出前館)の材料に無関係な「本日の【ゴールデンクロス／
    # デッドクロス】前場 GC=79銘柄 DC=112銘柄」(市場全体の指標集計)が、5027
    # (AnyMind Group)に「話題株ピックアップ【夕刊】(1): ＦＦＲＩ、テラドローン、
    # キオクシア」(無関係な複数銘柄の列挙)が、それぞれ紐付いていた。
    "ゴールデンクロス", "デッドクロス", "話題株ピックアップ",
    # 2026-08-27追加: 銘柄と無関係な年金・FP系の一般向けコラムが複数銘柄に
    # 重複して紐付いていた(例:「【秋の年金支給日は10月15日(木)】厚生年金
    # 「月20万円」の壁とFPが解説」が7596と3662の両方に同一タイトルで保存)。
    "とFPが解説", "年金支給日",
    # 2026-08-27追加: 8628(松井証券)の最高スコア材料が「キオクシア株と
    # NVIDIA株、比べて日中売買　個人の選択肢広がる」という無関係銘柄の記事
    # だった。証券会社の銘柄コードは、Nikkeiが個人投資家の売買動向・信用
    # 取引統計を報じる記事すべてに「関連銘柄」としてタグ付けされるため、
    # 同日だけで19件中17件がこの種の市場全体統計記事(その証券会社固有の
    # ニュースではない)で汚染されていた。
    "個人の信用取引", "個人株主の増加数", "個人投資家が株売り越し",
    "個人投資家が", "信用買い残", "信用評価損益率", "日経平均",
    "口座乗っ取り", "純利益率首位",
    # 2026-08-31追加: 2484(出前館)の最高スコア材料が「韓国大統領・イタリア
    # 首相が来日　今週の予定1月11日〜」という日経の「今週の予定」欄(政治・
    # 経済全般の週間スケジュール、個別銘柄と無関係)だった。165件がこの
    # パターンで汚染されていた。
    "今週の予定",
    # 2026-08-31追加: 7803(ブシロード)の材料が「新興株17日　グロース250が
    # 続落　半年ぶり安値」というマザーズ/グロース市場全体の日次相場コラム
    # (個別銘柄と無関係)だった。481件がこのパターンで汚染されていた。
    "グロース250", "グロース２５０",
    # 2026-09-15追加: 2804(ブルドックソース)の材料が「25年の全国百貨店の
    # 売上高が発表　1月23日ビジネス主な予定」、1419(タマホーム)が
    # 「デジタル見本市「CEATEC」開幕　10月14日ビジネス主な予定」という
    # 日経の「その日のビジネス関連予定一覧」欄(経済全体のスケジュール、
    # 個別銘柄と無関係)だった。1,679件がこのパターンで汚染されていた。
    "ビジネス主な予定",
    # 2026-09-17追加: 2307(クロスキャット)の材料に無関係な「＜特別気配＞
    # レオパレス、ＧＯ、ＡＢＥＪＡが買い気配」(複数銘柄を列挙する寄り付き前
    # 特別気配一覧、単一の主役企業が存在しない)が紐付いていた。
    "＜特別気配＞",
    # 2026-09-17追加: 深掘り分析でC判定候補の材料を全件検証したところ、テクニカル
    # 指標の市場全体集計(MACD/パラボリック/一目均衡表/ボリンジャー/25日線の
    # 売買サイン銘柄数リスト)・株探の割安株ランキング特集・フィスコの日次
    # コラムが、単一の主役企業を持たないまま個別銘柄の材料として保存されて
    # いた(4479マクアケの最高スコア材料が「16日前引け(速報）：上海総合指数は
    # 0.57％高」、2653イオン九州が「本日の【均衡表｜3役好転／逆転】」等)。
    # いずれもタイトルに主役企業が存在しないため無条件で除外する。
    # 「均衡表」「ボリンジャー」「25日線」を単体で除外すると
    # 「テクニカルで選ぶ注目銘柄：エムスリー＝日足一目均衡表の「雲」の上限を
    # 突破」「村田製---25日線や75日線を射程に入れたリバウンド狙い」のような
    # 実在の個別銘柄記事まで巻き込むため、集計枠の接頭辞に限定する
    # (ドライラン: 絞り込み後は自社名を含む行が0件であることを確認済み)。
    "｜買い／売りサイン", "｜買い／売り・転換", "本日の【均衡表",
    "本日の【ボリンジャー", "本日の【25日線",
    "総合指数は", "割安株特集", "【明日の好悪材料】", "本日の投資戦略",
    # 2026-09-17追加(第2弾): 当日のC判定候補4641(アルプス技研)の唯一の材料が
    # 「配当利回り“4％超”の【最高益】リスト〔第2弾〕26社選出 ＜成長株特集＞」
    # という株探のスクリーニング特集で、これが material_type=過去最高益 ・
    # sentiment=0.616 の好材料として最高スコア材料になっていた(同社固有の
    # 開示は1件も無い)。「NN社選出」型の銘柄リストは主役企業が存在しない。
    # レーティング日報・滬港通の香港株上位10銘柄・日次相場コラムも同様。
    "社選出", "成長株特集", "レーティング日報", "滬港通", "株式明日の戦略",
    # 2026-09-23追加: 「滬港通」は登録済みだったが対になる「深港通」が
    # 抜けており、3823(ＴＨＥ　ＷＨＹ　ＨＯＷ　ＤＯ　ＣＯＭＰＡＮＹ)に
    # 「「深港通」：香港株の取引上位10銘柄（9月17日）」が紐付いていた。
    # 香港株の売買代金上位一覧なので日本の個別銘柄の材料ではない。
    # ドライラン: 16件全件が対象、自社名を含む行は0件。
    "深港通",
    # 2026-09-17追加(第3弾): 論理削除の適用後に高スコア材料(sentiment>=0.5)を
    # 洗い直して残っていた集計枠。「本日/今週の【自社株買い】銘柄」(50件46銘柄、
    # いずれも s=0.75)は発表企業を列挙するリストで主役企業が存在しない。
    # 「コンビニ各社などが発表---月次動向」(10件9銘柄、s=0.75)、
    # 「＜マ－ケット日報＞」(19件10銘柄)も同様。
    # なお「<社名>－<値動き>　<理由>」型(株探/DZHの個別銘柄速報)は、主役企業が
    # タイトル先頭に入るため一律除外してはいけない。2175(エス・エム・エス)の
    # 「ＳＭＳ－3日ぶり反発　オアシスマネジメントの保有割合増加」は自社の本物の
    # 材料だが、登録名の2文字スタブ「エス」が略称「ＳＭＳ」に一致しないため
    # 自社名一致つきの除外に入れても誤除外される。この略称問題が解決するまで
    # この型には手を付けない。
    "【自社株買い】銘柄", "コンビニ各社などが発表", "マ－ケット日報",
    # 2026-09-17追加(第4弾): 「個別銘柄のひと口情報」(62件60銘柄)と
    # 「個別銘柄のスポット情報」(54件)は全銘柄で同一タイトルのまとめ枠で、
    # 主役企業がタイトルに一切現れない(ドライラン: 自社名を含む行0件)。
    "個別銘柄のひと口情報", "個別銘柄のスポット情報",
    # 2026-09-22追加: 深掘り分析で4440(ヴィッツ)に「NY原油・金相場（11日）－原油が
    # 9日ぶりに反落」、6203(豊和工業)に「〔ブル＆ベア〕大幅続伸＝AI関連に買い」
    # 「アジア株式市場サマリー：引け（15日）」、複数銘柄に「2026年【後半戦】急騰
    # 材料株 ベスト50」が紐付いていた。いずれも市況コラム/特集で主役企業が無い
    # (ドライラン: アジア株式市場サマリー172件・急騰材料株ベスト50 32件・NY原油
    # 2件・ブル＆ベア2件、自社名一致はサマリーの「リー」が偶然一致した1件のみ)。
    "NY原油・金相場", "アジア株式市場サマリー", "〔ブル＆ベア〕", "急騰材料株 ベスト50",
    # 2026-09-22追加: 3103(ユニチカ)に「ダイヤモンドバック・エネルギー(FANG.US)、
    # 取締役が1,532.33万ドルで普通株7.5万株を売却」(moomoo証券の米国株インサイダー
    # 売買速報)が紐付いていた。ドライラン: 「(XXX.US)」を含む38件はすべて moomoo の
    # 米国株記事で、自社名一致は0件。
    ".US)",
    # 2026-09-25追加: 深掘り分析で4052(フィーチャ)に「16日前引け(速報）：ハンセン
    # 指数は0.12％高」「新興市場展望：調整色強まりテーマ株物色へ」「東証グロ－ス
    # 指数は3日続落、上値の重い展開／グロース市況」、5721(エスクリプトエナジー)に
    # 「〔東南アジア株式〕まちまち（26日）」が紐付いていた。いずれも海外/新興市場
    # 全体の市況コラムで主役企業が無い。フィスコの「前場に注目すべき3つのポイント」
    # も同様の日次相場コラム。ドライラン: ハンセン指数は34件・東南アジア株式19件・
    # 新興市場展望8件・グロース市況30件は自社名一致0件、3つのポイント98件の
    # 自社名一致2件はいずれもスタブ「イン」の偶然一致(タイトルに社名なし)。
    "ハンセン指数は", "〔東南アジア株式〕", "新興市場展望", "／グロース市況",
    "前場に注目すべき3つのポイント",
    # 同日追加: 6904(原田工業)に「アジア・新興市場の主要株価指数一覧（20日）」。
    # ドライラン: 89件(欧州版含む)、自社名一致0件。
    "主要株価指数一覧",
    # 2026-09-26追加: 深掘り分析で3653(モルフォ)に「明日の【信用規制・解除】銘柄」
    # 「本日の【ストップ高／ストップ安】 引け S高＝7銘柄」、4180(Appier)に
    # 「レーティング週報【最上位を継続＋目標株価を増額】(2)」が紐付いていた。
    # いずれも主役企業がタイトルに無い集計枠(レーティング日報は登録済みだが週報が
    # 抜けていた)。ドライラン: 信用規制・解除10件・ストップ高／ストップ安112件・
    # レーティング週報186件、いずれも自社名一致0件。
    "【信用規制・解除】銘柄", "本日の【ストップ高／ストップ安】", "レーティング週報",
)


def _is_generic_market_digest(title: str) -> bool:
    return any(p in title for p in _GENERIC_DIGEST_TITLE_PATTERNS)


# 「【決算速報】{会社名}、...」「＜レーティング変更観測＞新規・{会社名}格上げ…」
# 「【アナリスト評価】{会社名}、…」のような複数銘柄まとめ枠のタイトルは、
# Yahoo!JPの個別銘柄ページに"他社"分が関連コンテンツとして混在表示されることが
# あり、無条件保存すると無関係企業の材料が紐付いてしまう(2026-08-26判明:
# 3070ジェリービーンズグループに無関係なJ-REIT「ハウスリート」の決算速報、
# 3315日本コークス工業に「ＧＬＰ」の決算速報、5580プロディライトに「ホテル
# リート」の決算速報、4689LINEヤフーに「弁護士コム／明治ＨＤ」のレーティング
# 変更観測・「リクルートＨ」のアナリスト評価、がそれぞれ紐付いていた)。
# 個別テンプレートごとに会社名部分を厳密抽出するのは壊れやすいため、これらの
# 枠マーカーを含み、かつ対象銘柄の登録名がタイトル中に一切登場しない場合は
# 一律で他社記事の誤紐付けとみなして除外する(2026-08-26、_title_company_mismatch
# から汎用化)。「人事、」「社長に」も同じ仕組みで拾える(2026-08-27追加:
# 2484(出前館)に無関係な「イメージ情報開発社長に半田基実氏」の人事ニュースが
# 4日連続で紐付いており、鮮度減衰ロジックにより"新鮮だが誤り"のこの材料が
# 上位表示の主因になっていた)。正しく自社の人事記事なら自社名がタイトルに
# 含まれるため誤って除外されることはない。「5％ルール・取得/処分」(大量保有
# 異動報告)も同じ理由で追加(2026-08-27: 「5％ルール・処分（21日）－サイバー
# エージェント　オービスインベストメントが7.99％→5.97％」という同一タイトルが
# 2157/2154/2331/2292/2749/2935/3134など7銘柄以上に重複して紐付いていた)。
_DIGEST_MARKER_PATTERNS = ("【決算速報】", "＜レーティング変更観測＞", "【アナリスト評価】",
                          "人事、", "社長に", "５％ルール", "5％ルール", "5%ルール",
                          # 2026-08-27追加: 8628(松井証券)に無関係な他社個別ニュースが
                          # 複数残存(「キオクシア株とNVIDIA株、比べて日中売買」「巨額損失
                          # の米AI特化ファンド、太陽誘電株の売買繰り返す」「キオクシア連動の
                          # レバ型ETF、米国で上場申請」「シーイーシー株価、一時７年８カ月
                          # ぶり高値」)。証券会社コードは無関係銘柄の個別記事にも幅広く
                          # 「関連銘柄」タグが付くため、これらのテンプレートも同じ仕組みで
                          # 除外する。
                          "比べて日中売買", "巨額損失の米AI特化ファンド", "連動のレバ型ETF",
                          "ぶり高値", "今夜のNEXT",
                          # 2026-08-27追加: 8628(松井証券)に無関係な「栄研化学vsダルトン、
                          # 鍵握った個人株主　MBO巡る暗闘に不満の声も」(4549栄研化学の
                          # MBO記事)が紐付いていた。「個人株主」という語を含む他社の
                          # 買収防衛戦記事も証券会社コードに関連銘柄タグが付く。
                          "MBO巡る",
                          # 2026-08-31追加: 7803(ブシロード)に「【ゲームエンタメ株概況
                          # (8/28)】「官公庁事業推進室」の新...」(その日のゲーム/エンタメ
                          # セクター全体の値動きまとめ)が紐付いていた。同一タイトルが
                          # ゲーム/エンタメ関連の多数の銘柄コードに重複して紐付く
                          # (他の【○○速報】系と同じ複数銘柄まとめ枠)。自社名がタイトル中に
                          # 実際に登場する場合(その日の値動きの主役として言及)は除外されない。
                          "【ゲームエンタメ株概況",
                          # 2026-09-03追加: 6897(ツインバード)に無関係な「三菱商事など商社株
                          # が高い　米バークシャーCEO「何十年も投資」（3日の株式市場）」が
                          # 紐付いていた。日経の「(N日の株式市場)」個別銘柄値動きコラムは
                          # 1本ごとに主役企業が異なり、その日の関連銘柄タグとして無関係な
                          # 多数の銘柄コードにも付与される(226件確認)。日付部分は可変なので
                          # 接尾辞「日の株式市場）」のみでマッチさせる。
                          "日の株式市場）",
                          # 2026-09-15追加: 2653(イオン九州)に無関係な「西日本シティ銀行、
                          # スマホ持ち込み禁止に」が紐付いていた(9536西部ガスHD/7180九州
                          # フィナンシャルグループにも同様に紐付き、計21件)。西日本シティ
                          # 銀行自身(持株会社含む)以外のコードでは自社名が一致しないため
                          # 誤紐付けとして除外される。
                          "西日本シティ銀行",
                          # 2026-09-15追加: 4714(リソー教育グループ)に無関係な「ヒューリック
                          # 株価が５日続伸　進学塾「鉄緑会」を買収」(3003ヒューリック自身の
                          # 買収記事)が紐付いていた。同業他社(学習塾業界)というだけで
                          # 関連銘柄タグが付く例。自社名不一致で除外される。
                          "ヒューリック株価が",
                          # 2026-09-15追加: 7811(中本パックス)に無関係な「今朝の注目
                          # ニュース！　★トリプラ、ビーエイブル、ＴＯブックスなどに注目！」
                          # が紐付いていた。毎朝配信される2〜3銘柄まとめ枠のコラムで、
                          # 131銘柄・140件に紐付いていた(他の【○○速報】系と同じ複数銘柄
                          # まとめ枠)。自社名がタイトル中に実際に登場する場合(例:
                          # 「オリジン、ＧＭＯペパボ、ネクソンなどに注目！」が3633
                          # GMOペパボに紐付くケース)は除外されない。
                          "今朝の注目ニュース",
                          # 2026-09-16追加: 3547(ユニシアホールディングス)に無関係な
                          # 「青山商事がイタリアン、FCで年に5〜7店舗出店へ」(別の外食
                          # フランチャイズ企業の記事)が17件も繰り返し紐付いていた。
                          # 同業(外食)というだけで関連銘柄タグが付く例。6310(井関農機)
                          # にも無関係な「M&A時の労使協議「法的義務化を」　労組JAMの
                          # 安河内会長」(労組会長の一般論コメント、井関農機自体のM&Aでは
                          # ない)が紐付いていた。
                          "青山商事がイタリアン", "労組ＪＡＭの安河内会長", "労組JAMの安河内会長",
                          # 2026-09-17追加: 2307(クロスキャット)に無関係な「かどや製油－
                          # 買い気配　インテグラル系が1株2514円でTOB　上場廃止へ」(2612
                          # かどや製油自身の記事)が1384/5842/2307など無関係な複数銘柄に
                          # 紐付いていた(結果、存在しないTOB材料でC判定候補の上位に
                          # 浮上)。株探の「○○－買い気配 [理由]」寄り付き前速報は、その日
                          # 値上がり配信された全銘柄の「関連銘柄」候補にまとめて紐付く。
                          # タイトル冒頭に主役企業の正式名/略称が入るため own_stub 一致
                          # チェックで自社材料は正しく残る。
                          "買い気配",
                          # 2026-09-17追加: 同じ深掘り検証で、複数銘柄まとめ枠のうち
                          # 主役企業がタイトルに入る型が未登録だった。3633(GMOペパボ)に
                          # 「【アナリスト予想】ＧＭＯＰＧ…」、6310(井関農機)に
                          # 「【アナリスト予想】東鉄工業…」、1861(熊谷組)に
                          # 「【FISCO銘柄コメント】蝶理…」「【アナリスト予想】ミライトワン…」、
                          # 3091(ブロンコビリー)に「【アナリスト予想】ゲンキードラッグ…」、
                          # 3222(USMH)に「【アナリスト予想】ライフＣ…」が紐付いていた。
                          # ドライラン実測: 【アナリスト予想】1,215件中1,017件、
                          # 【FISCO銘柄コメント】478件中44件、前日に動いた銘柄436件中414件、
                          # 動いた株・出来た株309件中287件、上場来高値192件中173件、
                          # 新興市場銘柄ダイジェスト62件中52件、本日の特選銘柄9件中8件が
                          # 他社記事として除外され、自社名を含む行は保持される。
                          # 「銘柄一覧」は「ＴＯＢ銘柄一覧（24日）－ボードルア、…」のように
                          # 対象企業がタイトルに並ぶことがあるため、無条件ではなく
                          # 自社名一致つきのこちらに入れる。
                          "【アナリスト予想】", "【FISCO銘柄コメント】",
                          "前日に動いた銘柄", "動いた株・出来た株", "上場来高値",
                          "新興市場銘柄ダイジェスト", "本日の特選銘柄", "銘柄一覧",
                          # 2026-09-17追加(第2弾): 2175(エス・エム・エス)に
                          # 「採れたて株価材料　Ｈｍｃｏｍｍ　社会インフラ監視技術に関する
                          # 特許出願」(265A Hmcomm)が material_type=特許 ・sentiment=0.75 の
                          # 強い好材料として紐付いていた(「モイ 京大iPS…」も同様)。
                          # 「採れたて株価材料－<社名> <内容>」「前場コメント No.N <社名列挙>」
                          # 「【ゲームエンタメ株前場(M/D)】…」はいずれも主役企業が
                          # タイトルに入る複数銘柄まとめ枠なので自社名一致つきで除外する。
                          # ドライラン実測: 採れたて株価材料91件中84件、前場コメント160件中
                          # 74件、ゲームエンタメ株前場508件中497件が他社記事として除外され、
                          # 自社が主役の回(例: マツキヨココカラ、カバー)は保持される。
                          "採れたて株価材料", "前場コメント", "【ゲームエンタメ株前場",
                          # 2026-09-17追加(第4弾): 主役企業がタイトルに並ぶまとめ枠。
                          # ドライラン実測で自社が主役の回は保持される
                          # (注目銘柄ダイジェスト58件中10件、業績修正（大引け後）44件中25件、
                          # 個別銘柄戦略118件中13件が自社名一致で残る)。
                          "注目銘柄ダイジェスト", "業績修正（大引け後）", "個別銘柄戦略",
                          # 2026-09-22追加: 8995(誠建設工業)に「《17日のストップ高・
                          # ストップ安銘柄》：報国マテリアがストップ高」「今週の【話題株
                          # ダイジェスト】 ＴＯＲＩＣＯ、ハイパー、エニマインド」が紐付いて
                          # いた。どちらもタイトルに主役企業が入るまとめ枠。ドライラン:
                          # ストップ高一覧91件中4件・話題株ダイジェスト66件中4件が自社名
                          # 一致で保持(例: 「ヴィッツがストップ高」は4440に残る)。
                          "ストップ高・ストップ安銘柄》", "話題株ダイジェスト",
                          # 2026-09-23追加: 3823(ＴＨＥ　ＷＨＹ　ＨＯＷ　ＤＯ
                          # ＣＯＭＰＡＮＹ)の材料を検証したところ、株探の決算まとめ枠
                          # 3種が未登録だった(「★本日の【イチオシ決算】 北川鉄、
                          # ノースサンド、ＢＲＡＮＵ」「明日の決算発表予定　テラドローン、
                          # サンバイオなど80社」「決算プラス・インパクト銘柄 【東証
                          # スタンダード・グロース】 … アストロＨＤ、サンバイオ、
                          # ビーエイブル」)。いずれも他社を列挙するリストなのに
                          # material_type=決算 の好材料として保存されていた。
                          # ドライラン実測: イチオシ決算84件中20件、明日の決算発表予定
                          # 108件中12件、決算プラス・インパクト銘柄299件中30件が
                          # 自社名一致で保持される(例: 「明日の決算発表予定　大光」が
                          # 3160大光に残る)。
                          "【イチオシ決算】", "明日の決算発表予定",
                          # 2026-09-26追加: 3653(モルフォ)に「決算マイナス・インパクト銘柄
                          # … テラドローン、リベラウェア、ＧＥＮＤＡ」「後場コメント No.3
                          # バンダイナム、ヴィッツ、…」が紐付いていた。プラス版/前場版は
                          # 登録済みで、対になるマイナス版/後場版が抜けていた。
                          # ドライラン実測: 決算マイナス・インパクト銘柄378件中25件、
                          # 後場コメント125件中86件が自社名一致で保持される。
                          "決算マイナス・インパクト銘柄", "後場コメント",
                          "決算プラス・インパクト銘柄")


def _title_company_mismatch(title: str, own_name: str) -> bool:
    if not own_name or not title:
        return False
    # 「○○GHD」「○○ＧＨＤ」は特定の持株会社を指す固有の略称。グループ内の
    # 別法人(同じ「○○」プレフィックスを持つ子会社・関連会社)は2文字スタブ
    # 一致で誤って生き残ってしまうため、自社名自体に持株会社シグナルが無い
    # 場合は別会社の記事とみなす(2026-09-01判明: 9041近鉄グループホール
    # ディングスの「近鉄GHDが個人向け銀行サービス」が8244近鉄百貨店/
    # 2978ツクルバ/9006京浜急行電鉄など無関係な複数銘柄に紐付いていた。
    # 同種にセンコーGHD→9904ベリテ等、計46件確認)。
    if any(m in title for m in ("GHD", "ＧＨＤ")) and not any(
            m in own_name for m in ("ホールディングス", "ＨＤ", "ＧＨＤ")):
        return True
    # 2026-09-17追加: 上と同じ「グループ共通プレフィックスの略称」問題。
    # 「ＧＭＯＰＧ」は3769 GMOペイメントゲートウェイ固有の略称だが、2文字スタブ
    # 「ＧＭ」が一致してしまうため、3633(GMOペパボ)に同社のアナリスト評価・予想が
    # 7件紐付いたまま残り、当日のエンジン候補2位の最高スコア材料になっていた。
    # 自社名に「ペイメント」を含まない GMO 系列各社では別会社の記事とみなす。
    if any(m in title for m in ("ＧＭＯＰＧ", "GMOPG")) and "ペイメント" not in own_name:
        return True
    if not any(p in title for p in _DIGEST_MARKER_PATTERNS):
        return False
    # 2026-08-27: 4文字スタブだと「カシオ計算機」の自社材料「カシオの株価、
    # ストップ高で5年ぶり高値」(見出しは略称「カシオ」表記)が own_stub=
    # 「カシオ計算」と不一致になり誤って除外されていた。見出しは正式社名を
    # 省略表記することが多いため、2文字スタブに緩めて誤除外を防ぐ。
    own_stub = own_name[:2]
    return not (own_stub and own_stub in title)


def store_materials(code: str, items: list[dict], own_name: str | None = None) -> int:
    """
    重複チェック+INSERTを項目ごとに逐次DB往復していたのを、コード単位で
    まとめて処理するよう変更 (2026-08-22)。大型銘柄は見出しが数十件になる
    ことがあり、逐次方式だと1銘柄で数十往復のDBラウンドトリップが発生して
    全銘柄フルスキャンの実行時間を大きく圧迫していた
    (item数 x 2往復 → 1往復のSELECT + 1回のバルクINSERTに削減)。

    市場全体のランキング・市況ダイジェスト記事(_is_generic_market_digest)や
    他社の決算速報の誤紐付け(_title_company_mismatch)は個別銘柄の材料として
    意味がない/誤りなので、ここで除外してから処理する
    (2026-08-25/26追加)。

    own_name を渡さない場合は1件ずつ SELECT する(2026-08-25の実装のまま、
    低頻度呼び出し向け)。enrich_top_codes のように数十〜百銘柄を連続で
    処理する場合は呼び出し側で securities を一括取得し own_name を渡すこと
    (2026-08-27: 個別 SELECT を100銘柄超で連続実行した際に接続がまれに
    失敗し own_name="" にフォールバックして誤紐付けフィルタが素通りする
    事例を確認したため)。
    """
    if not items:
        return 0
    items = [it for it in items if not _is_generic_market_digest(it.get("title", ""))]
    if not items:
        return 0

    if own_name is None:
        own_name = ""
        try:
            with db.cursor() as conn0:
                r = conn0.execute("SELECT name FROM securities WHERE code=%s", (code,)).fetchone()
                own_name = (r["name"] if r else "") or ""
        except Exception:
            pass
    if own_name:
        items = [it for it in items if not _title_company_mismatch(it.get("title", ""), own_name)]
    if not items:
        return 0
    from . import materials_analysis as ma

    dates = list({it.get("date") for it in items if it.get("date")})
    existing: set[tuple] = set()
    with db.cursor() as conn:
        if dates:
            ph = ",".join(["%s"] * len(dates))
            rows = conn.execute(
                f"SELECT date, title FROM materials WHERE code=%s AND date IN ({ph})",
                (code, *dates)).fetchall()
            existing = {(r["date"], r["title"]) for r in rows}

        to_insert = []
        seen = set()
        for it in items:
            title = it.get("title", "")
            date = it.get("date")
            key = (date, title)
            if key in existing or key in seen:
                continue
            seen.add(key)
            source = it.get("source", "tdnet")
            cls = classify_material(title)
            a = ma.analyze(title, body=it.get("body", "") or "", source=source, code=code)
            # 旧分類(category)が空なら material_type を流用、スコアは強い方を採用
            category = cls["category"] or a["material_type"]
            impact = max(cls["impact"], a["impact"])
            persistence = max(cls["persistence"], a["persistence"])
            sentiment = cls["sentiment"] if cls["sentiment"] != 0 else a["sentiment"]
            risk = a["dilution_risk"]
            ai_comment = ma.make_ai_comment(a, {"reaction_known": 0})
            to_insert.append((code, date, source, category, title, it.get("url"),
                              it.get("body", "") or "", sentiment, impact, persistence,
                              a["unpriced"], a["connection"], a["material_type"], risk,
                              ai_comment))

        if to_insert:
            conn.executemany(
                """INSERT INTO materials
                   (code,date,source,category,title,url,body,sentiment,impact,persistence,
                    unpriced,connect,material_type,risk,ai_comment,updated_at)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)""",
                to_insert)
    return len(to_insert)


def _empty_material_score() -> dict:
    return {"material_raw": 0.0, "pos_impact": 0.0, "neg_impact": 0.0,
            "has_fresh_material": 0, "last_material_days": None,
            "dilution_flag": 0, "going_concern_flag": 0, "n_materials": 0,
            "top_category": "", "top_title": "", "themes": [],
            "material_quality": 0.0, "top_material_type": "", "top_ai_comment": "",
            "top_unpriced": 0.0, "top_connection": 0.0, "top_chart_reaction": 0.0,
            "top_volume_reaction": 0.0, "top_risk": 0.0}


def _quality(r: dict) -> float:
    """1材料の質スコア (0..1): 接続度×(未織込×持続×反応) ×(1-出尽くし/希薄化)。

    信頼度だけでなく未織り込み感・持続性・接続度・チャート反応・出来高反応を重視し、
    出尽くし/希薄化リスクで減点する。新カラムが無い行でも0で安全に動く。
    """
    unp = float(r.get("unpriced") or 0)
    per = float(r.get("persistence") or 0)
    conn = float(r.get("connect") or 0) or 0.6
    cr = float(r.get("chart_reaction") or 0)
    vr = float(r.get("volume_reaction") or 0)
    risk = float(r.get("risk") or 0)
    core = 0.40 * unp + 0.25 * per + 0.20 * cr + 0.15 * vr
    q = conn * core * (1.0 - 0.5 * risk)
    return max(0.0, min(q, 1.0))


def _days_old(row_date: str | None, asof: str) -> int | None:
    """材料の発表日からasofまでの営業日ベースの経過日数(土日を除く簡易版、祝日は未考慮)。

    2026-08-25: 単純な暦日差だと、金曜発表の材料は月曜時点で暦上3日経過扱いになり
    不当に減衰してしまう(市場が休みの週末は実質「情報が古くなる」時間ではない)。
    土日を飛ばして数えることで、金曜の材料が月曜も実質1営業日前として扱われるようにする。
    """
    if not row_date:
        return None
    try:
        d0 = datetime.strptime(row_date, "%Y-%m-%d").date()
        d1 = datetime.strptime(asof, "%Y-%m-%d").date()
    except Exception:
        return None
    if d1 <= d0:
        return 0
    days = 0
    cur = d0
    while cur < d1:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # Mon-Fri のみカウント
            days += 1
    return days


def _recency_decay(days_old: int | None, half_life_days: float = 1.5) -> float:
    """材料の鮮度減衰。half_life_days(営業日)経過するごとにスコアを半分にする。

    2026-08-25判明: 過去の判定済み予測を「材料の発表からの経過日数」で分けると、
    発表0〜1日以内の新鮮な材料を持つ予測は成功率8.2%だったのに対し、2日以上
    経過した材料は3.6%と半分以下だった(全体911件の79%が実は2日以上経過した
    古い材料で、うち27%は11〜25日前)。従来はpos_impact/material_qualityとも
    lookback期間(25日)内で最もインパクトの高い材料を無条件採用しており、
    3週間前の大型開示が居座り続けて「材料あり」上位に古い・既に織り込み済みの
    銘柄を拾ってしまっていた。経過日数(営業日)に応じて指数減衰させることで、
    同程度のインパクトなら新しい材料を優先する。
    half_life=1.5営業日(2026-08-25、当日〜翌営業日の情報をより強く優先すべき
    というユーザー指摘を反映して初期値4日から短縮): 当日=1.0, 1営業日前=0.63,
    2営業日前=0.40, 5営業日前=0.10 と、当日〜翌営業日を明確に優遇しつつ
    ゼロにはしない。
    """
    if days_old is None or days_old < 0:
        return 1.0
    return 0.5 ** (days_old / half_life_days)


def score_material_rows(rows: list[dict], asof: str) -> dict:
    """既に取得済みの材料行リストからスコアを計算 (単一/バルク共通)。

    rows は date DESC 順。最低限 date,category,title,sentiment,impact,persistence を持つ。
    新カラム (unpriced,connect,chart_reaction,volume_reaction,risk,material_type,ai_comment)
    があれば material_quality と top_* 表示フィールドも算出する (モデル特徴量は不変)。
    """
    themes_found: list[str] = []
    for r in rows:
        for t, kws in THEME_KEYWORDS.items():
            if any(k in (r["title"] or "") for k in kws) and t not in themes_found:
                themes_found.append(t)
    if not rows:
        return _empty_material_score()

    pos = [r for r in rows if (r["sentiment"] or 0) > 0]
    neg = [r for r in rows if (r["sentiment"] or 0) < 0]
    pos_impact = max(((r["impact"] or 0) * (r["persistence"] or 0.5)
                      * _recency_decay(_days_old(r["date"], asof)) for r in pos), default=0.0)
    neg_impact = max(((r["impact"] or 0) * _recency_decay(_days_old(r["date"], asof)) for r in neg), default=0.0)
    last_date = rows[0]["date"]
    last_days = _days_old(last_date, asof)
    # 鮮度ウェイト: T0/T-1 で出た材料を重視
    fresh = 1 if (last_days is not None and last_days <= 3) else 0
    material_raw = max(0.0, pos_impact - 0.5 * neg_impact)
    dilution = int(any(c in (r["category"] or "") for r in rows
                       for c in ["新株予約権", "第三者割当", "公募増資", "ワラント", "希薄化"]))
    going_concern = int(any("継続企業" in (r["category"] or "") for r in rows))

    # 質スコア: 最良の好材料を採用 (接続度×未織込×反応 ベース、鮮度減衰込み)
    pool = pos or rows
    best = max(pool, key=lambda r: _quality(r) * _recency_decay(_days_old(r["date"], asof)))
    material_quality = round(_quality(best) * _recency_decay(_days_old(best["date"], asof)), 3)

    # 上位材料タイトル(理由表示用)
    top_row = pos[0] if pos else rows[0]
    top_title = (top_row["title"] or "")[:60]

    return {
        "material_raw": round(material_raw, 3),
        "pos_impact": round(pos_impact, 3),
        "neg_impact": round(neg_impact, 3),
        "has_fresh_material": fresh,
        "last_material_days": last_days,
        "dilution_flag": dilution,
        "going_concern_flag": going_concern,
        "n_materials": len(rows),
        "top_category": pos[0]["category"] if pos else (rows[0]["category"] or ""),
        "top_title": top_title,
        "themes": themes_found,
        # --- 新: 質スコア + 表示フィールド (モデル特徴量には未使用) ---
        "material_quality": material_quality,
        "top_material_type": best.get("material_type") or "",
        "top_ai_comment": best.get("ai_comment") or "",
        "top_unpriced": round(float(best.get("unpriced") or 0), 3),
        "top_connection": round(float(best.get("connect") or 0), 3),
        "top_chart_reaction": round(float(best.get("chart_reaction") or 0), 3),
        "top_volume_reaction": round(float(best.get("volume_reaction") or 0), 3),
        "top_risk": round(float(best.get("risk") or 0), 3),
    }


def recent_material_score(code: str, asof: str, lookback_days: int = 25) -> dict:
    """DB内の直近材料を集計して材料サブスコア(0..1)と内訳を返す (単一銘柄)。"""
    start = (datetime.strptime(asof, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT date,category,title,sentiment,impact,persistence,unpriced,connect,"
            "chart_reaction,volume_reaction,risk,material_type,ai_comment FROM materials "
            "WHERE code=%s AND date BETWEEN %s AND %s AND NOT COALESCE(excluded, FALSE) "
            "ORDER BY date DESC",
            (code, start, asof),
        ).fetchall()
    return score_material_rows(rows, asof)


def recent_material_scores_bulk(codes: list[str], asof: str, lookback_days: int = 25,
                                chunk: int = 500) -> dict[str, dict]:
    """複数銘柄の材料スコアをまとめて取得 (1クエリ/チャンク)。

    predict のループで銘柄ごとに DB 往復するのを避ける。
    返り値に存在しない銘柄は空スコア扱いにすること。
    """
    if not codes:
        return {}
    start = (datetime.strptime(asof, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    by_code: dict[str, list] = {}
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        ph = ",".join(["%s"] * len(part))
        with db.cursor() as conn:
            rows = conn.execute(
                f"SELECT code,date,category,title,sentiment,impact,persistence,unpriced,connect,"
                f"chart_reaction,volume_reaction,risk,material_type,ai_comment FROM materials "
                f"WHERE code IN ({ph}) AND date BETWEEN %s AND %s "
                f"AND NOT COALESCE(excluded, FALSE) ORDER BY code, date DESC",
                tuple(part) + (start, asof),
            ).fetchall()
        for r in rows:
            by_code.setdefault(r["code"], []).append(r)
    return {c: score_material_rows(rs, asof) for c, rs in by_code.items()}


# ---------- EDINET (金融庁公式API、無料、登録不要) ----------

def fetch_edinet_docs(date: str) -> dict[str, list[dict]]:
    """
    EDINET から指定日の開示文書一覧を取得。
    証券コードは documents.json 自体が返す secCode(5桁、先頭4桁がTSEコード)を
    直接使う。2026-08-22判明: 従来は edinetCode→TSEコード変換に
    companies.json という存在しないエンドポイントを叩いており
    (2023年のEDINETリニューアルで廃止・JS依存のダウンロードに変更されたため
    固定URLでは取得不能)、常に空マッピングで全件が捨てられ、EDINET連携は
    実装当初から一件も機能していなかった。secCode はファンド関連の書類では
    null になるが、上場企業自身の開示(有価証券報告書・変更報告書等)では
    populated されており、それだけで十分実用になる。
    12時間キャッシュ。
    """
    from pathlib import Path
    edinet_dir = Path(_cache_dir()).parent / "edinet"
    edinet_dir.mkdir(exist_ok=True)
    cache_file = edinet_dir / f"{date.replace('-','')}.json"

    cached = _load_cache(cache_file, max_age_hours=12)
    if cached is not None:
        n = sum(len(v) for v in cached.values())
        if n > 0:
            print(f"    [EDINET] cache hit {date}: {n} docs, {len(cached)} codes")
            return cached

    # EDINET API v2 は2024年以降サブスクリプションキー必須 (無料登録で取得)。
    # 未設定ならスキップ (キーが無いと 401 で0件になるため理由を明示)。
    api_key = os.environ.get("EDINET_API_KEY", "").strip()
    if not api_key:
        print(f"    [EDINET] skipped {date}: EDINET_API_KEY 未設定 "
              f"(https://api.edinet-fsa.go.jp で無料取得し環境変数に設定)")
        return {}

    params = {"date": date, "type": 2, "Subscription-Key": api_key}
    data = _get_json(EDINET_API, params, retries=3, timeout=30, base_pause=1.5)
    if not data or data.get("StatusCode") not in (None, 200):
        msg = (data or {}).get("message", "no data")
        print(f"    [EDINET] fetch failed for {date}: {str(msg)[:80]}")
        return {}

    by_code: dict[str, list[dict]] = {}
    for doc in data.get("results", []):
        sec_code = (doc.get("secCode") or "").strip()
        if len(sec_code) < 4:
            continue
        code = sec_code[:4]
        submit_date = (doc.get("submitDateTime") or "")[:10] or date
        desc = doc.get("docDescription") or ""
        filer = doc.get("filerName") or ""
        by_code.setdefault(code, []).append({
            "date": submit_date,
            "title": f"{desc}（{filer}）" if filer else desc,
            "url": "",
            "source": "edinet",
        })

    if by_code:
        _save_cache(cache_file, by_code)
    n = sum(len(v) for v in by_code.values())
    print(f"    [EDINET] {date}: {n} docs, {len(by_code)} codes")
    return by_code


# ---------- Yahoo Finance ニュース (個別銘柄補完用) ----------

def fetch_yahoo_finance_news(code: str, count: int = 5) -> list[dict]:
    """Yahoo Finance から銘柄ニュースを取得。上位予測銘柄の材料補完に使用。"""
    sym = f"{code}.T"
    params = {"q": sym, "quotesCount": 0, "newsCount": count,
              "enableFuzzyQuery": "false", "newsQuerySchema": "v3"}
    try:
        r = requests.get(YAHOO_NEWS_API, params=params, timeout=10,
                         headers={"User-Agent": "Mozilla/5.0 (surge-radar/1.0)"})
        if r.status_code != 200:
            return []
        news_items = r.json().get("news", [])
        out = []
        for item in news_items:
            title = (item.get("title") or "").strip()
            if not title:
                continue
            pub = item.get("providerPublishTime", 0)
            dt = datetime.fromtimestamp(pub).strftime("%Y-%m-%d") if pub else datetime.now().strftime("%Y-%m-%d")
            out.append({"date": dt, "title": title,
                        "url": item.get("link", ""), "source": "yahoo_news"})
        return out
    except Exception:
        return []


def fetch_kabutan_news(code: str, max_items: int = 10, session=None) -> list[dict]:
    """
    Kabutan.jp から銘柄別ニュース・開示を取得。
    URL: https://kabutan.jp/stock/news?code={code}  (銘柄固有ページ)
    旧 news/?type=1&code={code} は全銘柄で同じ市場ニュースを返すため使用不可。
    2026-08-22判明: URLの"?"が誤って"%s"という文字列になっており常に404で
    実装当初から一件も取得できていなかった(enrich_top_codesが毎日
    "kabutan_codes: 0"を返し続けていた原因)。
    日付形式: "26/06/25 15:30" → YYYY-MM-DD
    session: 渡された場合は接続を使い回す(TCP/TLSハンドシェイクの
    再確立を避け、バッチ取得を大幅に高速化する。2026-08-22追加)。
    """
    global _kabutan_logged_error
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    url = f"https://kabutan.jp/stock/news?code={code}"
    client = session or requests
    try:
        r = client.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if r.status_code != 200:
            if not _kabutan_logged_error:
                _kabutan_logged_error = True
                print(f"    [kabutan] DIAG non-200: code={code} status={r.status_code} "
                      f"len={len(r.text)} body_head={r.text[:200]!r}", flush=True)
            return []
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        now = datetime.now()
        for row in soup.select(".s_news_list tr")[:max_items]:
            a = row.find("a")
            time_el = row.find(class_="news_time")
            if not a:
                continue
            title = a.get_text(strip=True)
            if not title:
                continue
            href = a.get("href", "")
            if href and not href.startswith("http"):
                href = "https://kabutan.jp" + href
            # Date format: "26/06/25 15:30" (YY/MM/DD HH:MM)
            date_raw = time_el.get_text(strip=True) if time_el else ""
            date_str = now.strftime("%Y-%m-%d")
            if date_raw:
                try:
                    parts = date_raw.split("/")
                    if len(parts) == 3:
                        # YY/MM/DD HH:MM
                        yy = int(parts[0])
                        yr = 2000 + yy
                        mm = int(parts[1])
                        dd_rest = parts[2].split()
                        dd = int(dd_rest[0])
                    else:
                        # Fallback: MM/DD HH:MM
                        mm = int(parts[0])
                        dd = int(parts[1].split()[0])
                        yr = now.year if mm <= now.month else now.year - 1
                    date_str = f"{yr:04d}-{mm:02d}-{dd:02d}"
                except Exception:
                    pass
            out.append({
                "date": date_str,
                "title": title,
                "url": href,
                "source": "kabutan",
            })
        return out
    except Exception as e:
        if not _kabutan_logged_error:
            _kabutan_logged_error = True
            print(f"    [kabutan] DIAG exception: code={code} {type(e).__name__}: {str(e)[:200]}", flush=True)
        return []


_kabutan_logged_error = False
_minkabu_logged_error = False


def fetch_minkabu_news(code: str, max_items: int = 15, session=None) -> list[dict]:
    """
    みんかぶ(minkabu.jp)の銘柄別ニュースページから見出しを取得。
    URL: https://minkabu.jp/stock/{code}/news
    みんかぶは株探・フィスコ等複数の配信元記事の見出しを集約表示しているため、
    本文が有料であっても見出し(タイトル)だけは無料で読める。既存の材料分析
    (classify_material/materials_analysis.analyze)はタイトルのキーワード
    マッチングで動くため、本文が取れなくても見出しだけで十分活用できる。
    日付形式: "今日 08:30" または "08/21 16:35" (MM/DD HH:MM、年は現在年basis)。
    session: 渡された場合は接続を使い回す(2026-08-22追加、高速化目的)。
    """
    global _minkabu_logged_error
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    url = f"https://minkabu.jp/stock/{code}/news"
    client = session or requests
    try:
        r = client.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        if r.status_code != 200:
            if not _minkabu_logged_error:
                _minkabu_logged_error = True
                print(f"    [minkabu] DIAG non-200: code={code} status={r.status_code} "
                      f"len={len(r.text)} body_head={r.text[:200]!r}", flush=True)
            return []
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        now = datetime.now()
        for li in soup.select('ul.md_list[data-role="news-list-section"] > li')[:max_items]:
            a = li.select_one(".title_box a")
            if not a:
                continue
            title = a.get_text(strip=True)
            if not title:
                continue
            href = a.get("href", "")
            if href and not href.startswith("http"):
                href = "https://minkabu.jp" + href
            src_a = li.select_one("a.fcgl")
            orig_source = src_a.get_text(strip=True) if src_a else "minkabu"
            time_text = ""
            for div in li.select(".flex.items-center"):
                t = div.get_text(strip=True)
                if t:
                    time_text = t
            date_str = now.strftime("%Y-%m-%d")
            try:
                if "今日" in time_text:
                    date_str = now.strftime("%Y-%m-%d")
                elif "/" in time_text:
                    mmdd = time_text.split()[0]
                    mm, dd = (int(x) for x in mmdd.split("/"))
                    yr = now.year if mm <= now.month else now.year - 1
                    date_str = f"{yr:04d}-{mm:02d}-{dd:02d}"
            except Exception:
                pass
            out.append({
                "date": date_str,
                "title": title,
                "url": href,
                "source": f"minkabu({orig_source})" if orig_source != "minkabu" else "minkabu",
            })
        return out
    except Exception as e:
        if not _minkabu_logged_error:
            _minkabu_logged_error = True
            print(f"    [minkabu] DIAG exception: code={code} {type(e).__name__}: {str(e)[:200]}", flush=True)
        return []


def fetch_yahoo_jp_news(code: str, max_items: int = 20, session=None) -> list[dict]:
    """
    Yahoo!ファイナンス日本版(finance.yahoo.co.jp、既存の fetch_yahoo_finance_news
    が使う query1.finance.yahoo.com の検索APIとは別物)の銘柄別ニュースページ。
    URL: https://finance.yahoo.co.jp/quote/{code}.T/news
    株探・フィスコに加え、時事通信・トレーダーズウェブ・ダイヤモンド・ザイ等
    複数配信元の見出しを集約している(2026-08-22, 7203で実測確認)。件数も
    他ソースより多く出る傾向。CSS-modulesのクラス名はハッシュ付きで変わり
    うるため、末尾ハッシュに依存しないプレフィックス一致(正規表現)で選択する。
    session: 渡された場合は接続を使い回す(2026-08-22追加、高速化目的)。
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    url = f"https://finance.yahoo.co.jp/quote/{code}.T/news"
    client = session or requests
    try:
        r = client.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        if r.status_code != 200:
            return []
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        now = datetime.now()
        for it in soup.find_all(class_=re.compile(r"^_NewsItem_\w"))[:max_items]:
            a = it if it.name == "a" else it.find("a", class_=re.compile(r"__link"))
            h3 = it.find(class_=re.compile(r"__heading"))
            if not h3:
                continue
            title = h3.get_text(strip=True)
            if not title:
                continue
            href = a.get("href", "") if a else ""
            if href and not href.startswith("http"):
                href = "https://finance.yahoo.co.jp" + href
            media_el = it.find(class_=re.compile(r"supplement--media"))
            time_el = it.find(class_=re.compile(r"supplement--time"))
            orig_source = media_el.get_text(strip=True) if media_el else "yahoo"
            time_text = time_el.get_text(strip=True) if time_el else ""
            date_str = now.strftime("%Y-%m-%d")
            try:
                if "/" in time_text:
                    mm, dd = (int(x) for x in time_text.split("/"))
                    yr = now.year if mm <= now.month else now.year - 1
                    date_str = f"{yr:04d}-{mm:02d}-{dd:02d}"
            except Exception:
                pass
            out.append({
                "date": date_str,
                "title": title,
                "url": href,
                "source": f"yahoojp({orig_source})" if orig_source != "yahoo" else "yahoojp",
            })
        return out
    except Exception:
        return []


def _fetch_batch_concurrent(codes: list[str], fetch_fn, max_codes: int, label: str,
                            pause: float = 0.5, max_workers: int = 3) -> dict[str, list[dict]]:
    """
    銘柄ごとの見出し取得を軽度に並列化する共通ヘルパー。

    1サイトあたり同時3接続程度は通常のブラウザ閲覧と同程度で、各サイトへの
    リクエスト頻度を過度に上げずに全体スループットを約3倍にできる
    (2026-08-22: 3000円以下の全銘柄~2700をGitHub Actionsの1ジョブ上限
    6時間以内に一巡させるための高速化)。ワーカーごとにリクエスト後 pause
    秒待つことで、単純に全並列で叩くよりは礼儀を保つ。

    requests.Session() で接続を使い回す(2026-08-22追加): これまで
    requests.get() を毎回単独で呼んでおり、同じサイトへの2件目以降の
    リクエストでもTCP/TLSハンドシェイクを毎回やり直していた。実データ
    200銘柄での検証で1銘柄あたり実測18秒(小規模ベンチマークの8倍)と
    大幅に遅かった原因の有力候補。HTTPAdapterでプール上限をmax_workers
    以上に設定し、ワーカー間でSessionを共有してkeep-aliveを効かせる。
    """
    from concurrent.futures import ThreadPoolExecutor
    from requests.adapters import HTTPAdapter

    targets = codes[:max_codes]
    by_code: dict[str, list[dict]] = {}
    if not targets:
        return by_code

    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=max_workers, pool_maxsize=max_workers)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    def _worker(code: str):
        items = fetch_fn(code, session=session)
        time.sleep(pause)
        return code, items

    done = 0
    try:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            for code, items in ex.map(_worker, targets):
                done += 1
                if items:
                    by_code[code] = items
                if done % 50 == 0:
                    print(f"    [{label}] {done}/{len(targets)} done, {len(by_code)} with news", flush=True)
    finally:
        session.close()
    return by_code


def fetch_yahoo_jp_batch(codes: list[str], pause: float = 0.5,
                         max_codes: int = 100) -> dict[str, list[dict]]:
    """複数銘柄のYahoo!ファイナンス日本版ニュースを取得。上位予測銘柄の補完用。"""
    return _fetch_batch_concurrent(codes, fetch_yahoo_jp_news, max_codes, "yahoojp", pause)


def _parse_nikkei_date(text: str, now: datetime) -> str:
    """
    日経の銘柄別ニュース一覧の日付表記を YYYY-MM-DD に変換する。
    実表記は4パターン: "18:13"(当日・時刻のみ) / "9/16" / "2025/12/5" /
    "9/15更新"。年が省略された表記で月日が未来になる場合は前年とみなす。
    解釈できない場合は "" を返す(呼び出し側で当日扱いにフォールバック)。
    """
    t = (text or "").strip().replace("更新", "").strip()
    if not t:
        return ""
    if ":" in t and "/" not in t:
        return now.strftime("%Y-%m-%d")
    parts = t.split("/")
    try:
        if len(parts) == 3:
            yr, mm, dd = int(parts[0]), int(parts[1]), int(parts[2].split()[0])
        elif len(parts) == 2:
            mm, dd = int(parts[0]), int(parts[1].split()[0])
            yr = now.year
            if (mm, dd) > (now.month, now.day):
                yr -= 1
        else:
            return ""
        return f"{yr:04d}-{mm:02d}-{dd:02d}"
    except Exception:
        return ""


def fetch_nikkei_news(code: str, max_items: int = 20, session=None) -> list[dict]:
    """
    日本経済新聞 会社情報の銘柄別ニュース見出し一覧。
    URL: https://www.nikkei.com/nkd/company/news/?scode={code}
    本文は有料会員限定だが、見出し一覧自体は無料公開されている。

    2026-09-17まで、この関数は全見出しを無条件に「本日」付けで保存していた
    (一覧は直近ニュース中心だろうという前提)。実際にはニュースの少ない銘柄では
    数週間〜1年以上前の記事が一覧の先頭に居座り続けるため、古い記事が毎日
    "新しい材料" として再登録されていた(例: 4180Appierの「最終増益」は8/17の
    記事だが9/8〜9/16の毎日に保存、6310井関農機の「上方修正」記事は2025-11-14
    公開)。鮮度減衰ロジックが効かず has_fresh_material も常時1になるため、
    一覧に表示されている実際の日付(.m-listItem_time)を使う。
    session: 渡された場合は接続を使い回す(2026-08-22追加、高速化目的)。
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    url = f"https://www.nikkei.com/nkd/company/news/?scode={code}"
    client = session or requests
    try:
        r = client.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        if r.status_code != 200:
            return []
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        for li in soup.select("li.m-listFormat_item")[:max_items]:
            a = li.select_one(".m-listItem_text_text a")
            if not a:
                continue
            title = a.get_text(strip=True)
            if not title:
                continue
            href = a.get("href", "")
            if href and not href.startswith("http"):
                href = "https://www.nikkei.com" + href
            time_el = li.select_one(".m-listItem_time")
            date_str = _parse_nikkei_date(
                time_el.get_text(strip=True) if time_el else "", now) or today
            out.append({"date": date_str, "title": title, "url": href, "source": "nikkei"})
        return out
    except Exception:
        return []


def fetch_nikkei_batch(codes: list[str], pause: float = 0.5,
                       max_codes: int = 100) -> dict[str, list[dict]]:
    """複数銘柄の日経ニュースを取得。上位予測銘柄の補完用。"""
    return _fetch_batch_concurrent(codes, fetch_nikkei_news, max_codes, "nikkei", pause)


def fetch_minkabu_batch(codes: list[str], pause: float = 0.5,
                        max_codes: int = 100) -> dict[str, list[dict]]:
    """複数銘柄のみんかぶニュースを取得。上位予測銘柄の補完用。"""
    return _fetch_batch_concurrent(codes, fetch_minkabu_news, max_codes, "minkabu", pause)


def fetch_kabutan_batch(codes: list[str], pause: float = 0.5,
                        max_codes: int = 100) -> dict[str, list[dict]]:
    """複数銘柄の Kabutan ニュースを取得。上位予測銘柄の補完用。"""
    return _fetch_batch_concurrent(codes, fetch_kabutan_news, max_codes, "kabutan", pause)


def fetch_tdnet_per_code(codes: list[str], days: int = 30, pause: float = 0.5,
                          max_codes: int = 200) -> dict[str, list[dict]]:
    """
    個別銘柄TDnetを指定コードリストに対して呼ぶ (上位予測銘柄の材料補完用)。
    範囲エンドポイントがレート制限を受けたときの代替。
    """
    by_code: dict[str, list[dict]] = {}
    targets = codes[:max_codes]
    for i, code in enumerate(targets, 1):
        items = fetch_tdnet(code, days=days)
        if items:
            by_code[code] = items
        if i % 50 == 0:
            print(f"    [TDnet per-code] {i}/{len(targets)} done, {len(by_code)} with materials")
        time.sleep(pause)
    return by_code


def enrich_top_codes(codes: list[str], asof: str, max_codes: int = 100,
                     include_blocked_sources: bool = False) -> dict:
    """
    上位予測銘柄コードについて、Yahoo!JP/日経ニュースで材料を補完する。
    daily pipeline の predict 後(および predict 内の momentum pool 事前取得)に
    呼ぶことで材料スコアの精度を高める。TDnet が rate-limit されている場合の
    代替材料源としても機能する。

    2026-08-25判明: kabutan/みんかぶはGitHub Actionsのクラウド実行環境からの
    アクセスをBot対策でブロックしている(kabutan: JS実行チャレンジページ
    「Human Verification」、Playwrightヘッドレスブラウザで実際にJSを実行
    させても解決せず=人間の実操作を要求する本格的なチャレンジの可能性が高い。
    みんかぶ: 単純なIPブロック403 Forbidden、ブラウザエンジンでも無関係に
    ブロックされ続ける)。ローカルPCからは正常に取得できるため、リポジトリの
    コードにバグがあるわけではなくクラウドIPのレピュテーション判定が原因と
    確認済み。技術的に自動化での突破が現実的でないため、この2ソースは
    include_blocked_sources=True を明示した場合のみ追加する
    (2026-09-02: ローカル専用の scripts/materials_local_extra.py から使用。
    GitHub Actions からは常に False のまま呼ぶこと)。

    ソースはそれぞれ別ホストなのでスレッドで並行実行し(サイト間の並列化)、
    かつサイト内でも同一サイトへ最大3並列でリクエストする(サイト内の
    軽度並列化、_fetch_batch_concurrent参照)。
    """
    if not codes:
        return {"enriched": 0}
    targets = codes[:max_codes]
    stored = 0

    from concurrent.futures import ThreadPoolExecutor

    fetchers = {
        "yahoojp": lambda: fetch_yahoo_jp_batch(targets, max_codes=max_codes),
        "nikkei": lambda: fetch_nikkei_batch(targets, max_codes=max_codes),
    }
    if include_blocked_sources:
        fetchers["kabutan"] = lambda: fetch_kabutan_batch(targets, max_codes=max_codes)
        fetchers["minkabu"] = lambda: fetch_minkabu_batch(targets, max_codes=max_codes)
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(fetchers)) as ex:
        futures = {name: ex.submit(fn) for name, fn in fetchers.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception as e:
                print(f"    [enrich:{name}] error (non-fatal): {e}", flush=True)
                results[name] = {}

    # own_name を銘柄ごとに個別SELECTすると数十〜百銘柄の連続呼び出しで接続が
    # まれに失敗し誤紐付けフィルタが素通りする(2026-08-27判明)。ここで一括取得
    # してから store_materials に渡す。
    with db.cursor() as conn0:
        ph = ",".join(["%s"] * len(targets))
        name_rows = conn0.execute(
            f"SELECT code, name FROM securities WHERE code IN ({ph})", tuple(targets)).fetchall()
    name_map = {r["code"]: r["name"] or "" for r in name_rows}

    # store_materials は DB 書き込みなので並行実行後にメインスレッドでまとめて行う
    for by_code in results.values():
        for code, items in by_code.items():
            n = store_materials(code, items, own_name=name_map.get(code, ""))
            stored += n

    return {"enriched_codes": len(targets), "materials_added": stored,
            "yahoojp_codes": len(results.get("yahoojp", {})),
            "nikkei_codes": len(results.get("nikkei", {})),
            "kabutan_codes": len(results.get("kabutan", {})),
            "minkabu_codes": len(results.get("minkabu", {}))}


def analyze_with_llm(code: str, materials_text: str) -> dict | None:
    """
    上位候補のみ呼ぶ LLM 深掘りフック。
    ANTHROPIC_API_KEY が設定され、かつ有効化フラグがある場合のみ動作 (デフォルト無効=コスト0)。
    """
    import os
    if not os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("SURGE_ENABLE_LLM") != "1":
        return None
    try:
        import anthropic  # 遅延import (未インストールでも全体は動く)
    except Exception:
        return None
    try:
        client = anthropic.Anthropic()
        prompt = (
            "あなたは日本株の短期急騰材料を評価するアナリストです。以下の材料について "
            "未織り込み感/持続性/株価インパクト/銘柄接続度/続報余地/出尽くしリスク を各0-1で、"
            "JSONのみで返してください。\n\n" + materials_text[:4000]
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(msg.content[0].text)
    except Exception:
        return None
