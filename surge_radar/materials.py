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
    if not items:
        return 0
    n = 0
    with db.cursor() as conn:
        for it in items:
            title = it.get("title", "")
            # 同一(code,date,title)の重複はスキップ
            dup = conn.execute(
                "SELECT 1 FROM materials WHERE code=%s AND date=%s AND title=%s LIMIT 1",
                (code, it.get("date"), title)).fetchone()
            if dup:
                continue
            cls = classify_material(title)
            conn.execute(
                """INSERT INTO materials(code,date,source,category,title,url,sentiment,impact,persistence)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (code, it.get("date"), it.get("source", "tdnet"), cls["category"],
                 title, it.get("url"), cls["sentiment"], cls["impact"], cls["persistence"]),
            )
            n += 1
    return n


def recent_material_score(code: str, asof: str, lookback_days: int = 25) -> dict:
    """
    DB内の直近材料を集計して材料サブスコア(0..1)と内訳を返す。
    価格/出来高反応との掛け合わせは features 側で行う(材料反応の確認)。
    """
    start = (datetime.strptime(asof, "%Y-%m-%d") - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT date,category,title,sentiment,impact,persistence FROM materials "
            "WHERE code=%s AND date BETWEEN %s AND %s ORDER BY date DESC",
            (code, start, asof),
        ).fetchall()
    themes_found: list[str] = []
    for r in rows:
        for t, kws in THEME_KEYWORDS.items():
            if any(k in (r["title"] or "") for k in kws) and t not in themes_found:
                themes_found.append(t)
    if not rows:
        return {"material_raw": 0.0, "pos_impact": 0.0, "neg_impact": 0.0,
                "has_fresh_material": 0, "last_material_days": None,
                "dilution_flag": 0, "going_concern_flag": 0, "n_materials": 0,
                "top_category": "", "top_title": "", "themes": []}

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
    }


# ---------- EDINET (金融庁公式API、無料、登録不要) ----------

def _edinet_code_map() -> dict[str, str]:
    """
    EDINET社名コード → TSE4桁証券コード のマッピング。24時間キャッシュ。
    /api/v2/companies.json の stockExchangeAndSecuritiesCode を解析して構築。
    例: "東証プライム 7203" → "7203"
    """
    from pathlib import Path
    edinet_dir = Path(_cache_dir()).parent / "edinet"
    edinet_dir.mkdir(exist_ok=True)
    map_file = edinet_dir / "company_map.json"
    cached = _load_cache(map_file, max_age_hours=24)
    if cached:
        return cached

    data = _get_json("https://disclosure.edinet-fsa.go.jp/api/v2/companies.json",
                     {}, retries=2, timeout=30, base_pause=1.5)
    if not data:
        return {}

    mapping: dict[str, str] = {}
    for co in data.get("results", []):
        ecode = (co.get("edinetCode") or "").strip()
        sec_info = (co.get("stockExchangeAndSecuritiesCode") or "").strip()
        if not ecode or not sec_info:
            continue
        parts = sec_info.split()
        for p in reversed(parts):
            if p.isdigit() and len(p) == 4:
                mapping[ecode] = p
                break
    _save_cache(map_file, mapping)
    print(f"    [EDINET] company map: {len(mapping)} codes loaded")
    return mapping


def fetch_edinet_docs(date: str) -> dict[str, list[dict]]:
    """
    EDINET から指定日の開示文書一覧を取得。
    edinetCode → TSE証券コード変換に companies.json マッピングを使用。
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

    params = {"date": date, "type": 2}
    data = _get_json(EDINET_API, params, retries=3, timeout=30, base_pause=1.5)
    if not data:
        print(f"    [EDINET] fetch failed for {date}")
        return {}

    # EDINET は edinetCode を返すので company map でTSEコードに変換
    code_map = _edinet_code_map()
    by_code: dict[str, list[dict]] = {}
    for doc in data.get("results", []):
        ecode = (doc.get("edinetCode") or "").strip()
        code = code_map.get(ecode)
        if not code:
            continue
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


def fetch_kabutan_news(code: str, max_items: int = 10) -> list[dict]:
    """
    Kabutan.jp から銘柄別ニュース・開示を取得。
    URL: https://kabutan.jp/stock/news%scode={code}  (銘柄固有ページ)
    旧 news/%stype=1&code={code} は全銘柄で同じ市場ニュースを返すため使用不可。
    日付形式: "26/06/25 15:30" → YYYY-MM-DD
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []
    url = f"https://kabutan.jp/stock/news%scode={code}"
    try:
        r = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if r.status_code != 200:
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
    except Exception:
        return []


def fetch_kabutan_batch(codes: list[str], pause: float = 1.0,
                        max_codes: int = 100) -> dict[str, list[dict]]:
    """複数銘柄の Kabutan ニュースを取得。上位予測銘柄の補完用。"""
    by_code: dict[str, list[dict]] = {}
    for i, code in enumerate(codes[:max_codes], 1):
        items = fetch_kabutan_news(code)
        if items:
            by_code[code] = items
        if i % 20 == 0:
            print(f"    [Kabutan] {i}/{min(len(codes), max_codes)} done, {len(by_code)} with news")
        time.sleep(pause)
    return by_code


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
    上位予測銘柄コードについて、Kabutan ニュースで材料を補完する。
    daily pipeline の predict 後に呼ぶことで材料スコアの精度を高める。
    TDnet が rate-limit されている場合の代替材料源として機能する。
    """
    if not codes:
        return {"enriched": 0}
    targets = codes[:max_codes]
    stored = 0

    # Kabutan.jp per-code ニュース (TDnetが使えない場合の主力補完)
    kab_by = fetch_kabutan_batch(targets, pause=0.8, max_codes=max_codes)
    for code, items in kab_by.items():
        n = store_materials(code, items)
        stored += n

    return {"enriched_codes": len(targets), "materials_added": stored,
            "kabutan_codes": len(kab_by)}


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
