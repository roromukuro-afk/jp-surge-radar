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

def store_materials(code: str, items: list[dict]) -> int:
    """
    重複チェック+INSERTを項目ごとに逐次DB往復していたのを、コード単位で
    まとめて処理するよう変更 (2026-08-22)。大型銘柄は見出しが数十件になる
    ことがあり、逐次方式だと1銘柄で数十往復のDBラウンドトリップが発生して
    全銘柄フルスキャンの実行時間を大きく圧迫していた
    (item数 x 2往復 → 1往復のSELECT + 1回のバルクINSERTに削減)。
    """
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
    pos_impact = max((r["impact"] or 0) * (r["persistence"] or 0.5) for r in pos) if pos else 0.0
    neg_impact = max((r["impact"] or 0) for r in neg) if neg else 0.0
    last_date = rows[0]["date"]
    try:
        last_days = (datetime.strptime(asof, "%Y-%m-%d") - datetime.strptime(last_date, "%Y-%m-%d")).days
    except Exception:
        last_days = None
    # 鮮度ウェイト: T0/T-1 で出た材料を重視
    fresh = 1 if (last_days is not None and last_days <= 3) else 0
    material_raw = max(0.0, pos_impact - 0.5 * neg_impact)
    dilution = int(any(c in (r["category"] or "") for r in rows
                       for c in ["新株予約権", "第三者割当", "公募増資", "ワラント", "希薄化"]))
    going_concern = int(any("継続企業" in (r["category"] or "") for r in rows))

    # 質スコア: 最良の好材料を採用 (接続度×未織込×反応 ベース)
    pool = pos or rows
    best = max(pool, key=_quality)
    material_quality = round(_quality(best), 3)

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
            "WHERE code=%s AND date BETWEEN %s AND %s ORDER BY date DESC",
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
                f"WHERE code IN ({ph}) AND date BETWEEN %s AND %s ORDER BY code, date DESC",
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
            global _kabutan_logged_error
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
        global _kabutan_logged_error
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
            global _minkabu_logged_error
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
        global _minkabu_logged_error
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


def fetch_nikkei_news(code: str, max_items: int = 20, session=None) -> list[dict]:
    """
    日本経済新聞 会社情報の銘柄別ニュース見出し一覧。
    URL: https://www.nikkei.com/nkd/company/news/?scode={code}
    本文は有料会員限定だが、見出し一覧自体は無料公開されている。時刻のみ
    (日付なし)で表示されるため「本日」扱いとする(一覧は直近ニュース中心)。
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
        today = datetime.now().strftime("%Y-%m-%d")
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
            out.append({"date": today, "title": title, "url": href, "source": "nikkei"})
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


def enrich_top_codes(codes: list[str], asof: str, max_codes: int = 100) -> dict:
    """
    上位予測銘柄コードについて、Kabutan/みんかぶ/Yahoo!JP/日経ニュースで材料を補完する。
    daily pipeline の predict 後(および predict 内の momentum pool 事前取得)に
    呼ぶことで材料スコアの精度を高める。TDnet が rate-limit されている場合の
    代替材料源としても機能する。

    4ソースは別ホストなので4つをスレッドで並行実行し(サイト間の並列化)、
    かつ各ソース内でも同一サイトへ最大3並列でリクエストする(サイト内の
    軽度並列化、_fetch_batch_concurrent参照)。3000円以下の全銘柄(~2700)を
    GitHub Actions 1ジョブの上限6時間以内で一巡させるための高速化
    (2026-08-22: 「3000円以下に絞っているのだから完璧に(=全銘柄)回してほしい」
    という指摘に対応。サイト間×サイト内の二重並列化で、単純逐次実行に比べ
    総所要時間を概ね1/10程度に圧縮する狙い)。
    """
    if not codes:
        return {"enriched": 0}
    targets = codes[:max_codes]
    stored = 0

    from concurrent.futures import ThreadPoolExecutor

    fetchers = {
        "kabutan": lambda: fetch_kabutan_batch(targets, max_codes=max_codes),
        "minkabu": lambda: fetch_minkabu_batch(targets, max_codes=max_codes),
        "yahoojp": lambda: fetch_yahoo_jp_batch(targets, max_codes=max_codes),
        "nikkei": lambda: fetch_nikkei_batch(targets, max_codes=max_codes),
    }
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(fetchers)) as ex:
        futures = {name: ex.submit(fn) for name, fn in fetchers.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception as e:
                print(f"    [enrich:{name}] error (non-fatal): {e}", flush=True)
                results[name] = {}

    # store_materials は DB 書き込みなので並行実行後にメインスレッドでまとめて行う
    for by_code in results.values():
        for code, items in by_code.items():
            n = store_materials(code, items)
            stored += n

    return {"enriched_codes": len(targets), "materials_added": stored,
            "kabutan_codes": len(results.get("kabutan", {})),
            "minkabu_codes": len(results.get("minkabu", {})),
            "yahoojp_codes": len(results.get("yahoojp", {})),
            "nikkei_codes": len(results.get("nikkei", {}))}


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
