"""
ニュース見出しの取得(生データのみ)。

旧 materials.py から取得関数だけを移したもの。旧版にあった正規表現による分類・
除外・スコアリングは持ち込まない(分類は Claude が見出しを読んで行う。
procedures/label.md)。取得した見出しはそのまま news テーブルに保存する。
"""
from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

import requests

from . import db

TDNET_API = "https://webapi.yanoshin.jp/webapi/tdnet/list/{q}.json"

# 2026-09-26: disclosure.edinet-fsa.go.jp は disclosure2.edinet-fsa.go.jp へ
# リダイレクトされる際に /api/v2 のパスが落ち、HTTP 200 で「規定外操作が
# 行われました」というHTMLのエラー画面が返る。_get_json はJSONにできず None を
# 返すため "fetch failed: no data" となり、2026-08-28 以降 EDINET は1件も
# 取れていなかった(キー自体は有効)。API専用ホストを直接指定する。
EDINET_API = "https://api.edinet-fsa.go.jp/api/v2/documents.json"

_HHMM = re.compile(r"(\d{1,2}):(\d{2})")


def _published(date_str: str, raw: str | None) -> str | None:
    """日付(YYYY-MM-DD)と、サイトが表示した時刻を含む文字列から、公開日時(JST)を作る。
    時刻が読み取れなければ None(公開時刻不明)。日付だけから時刻を推測しない。"""
    m = _HHMM.search(raw or "")
    if not (date_str and m):
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh < 24 and 0 <= mm < 60):
        return None
    return f"{date_str}T{hh:02d}:{mm:02d}:00+09:00"

class FetchFailed(Exception):
    """銘柄別ページの取得に失敗した(HTTP 200 以外・通信エラー・ページ解析の例外)。
    「見出しが 0 件だった」と区別するために使う(2026-09-28: 遮断された銘柄が「材料なし」に
    見えていた)。status は http_403 / http_429 / error_<例外名> など。"""

    def __init__(self, status: str):
        super().__init__(status)
        self.status = status



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



def fetch_tdnet_range(since_date: str, until_date: str | None = None,
                      limit: int = 10000) -> dict[str, list[dict]]:
    """
    since_date〜until_date の全開示を取得し、証券コード -> 開示リスト を返す。

    2026-09-26 判明: yanoshin の範囲 API は page パラメータを無視し、何ページ目を
    要求しても最新 limit 件を返す。旧実装は limit=200 でページ送りしていたため、
    複数日の範囲でも最新 200 件を繰り返し受け取っていただけだった(17 日分を
    要求して 364 件・154 社。正しくは 2,241 件・1,263 社)。limit を十分大きくして
    1 回で取り、件数が limit に達したら取りこぼしの可能性があるので日ごとに取り直す。
    """
    until_date = until_date or datetime.now().strftime("%Y-%m-%d")
    frm = datetime.strptime(since_date, "%Y-%m-%d")
    to = datetime.strptime(until_date, "%Y-%m-%d")
    cf = _cache_file(frm.strftime("%Y%m%d"), to.strftime("%Y%m%d"))
    cached = _load_cache(cf)
    if cached is not None:
        return cached

    def _get(q: str) -> list[dict] | None:
        d = _get_json(TDNET_API.format(q=q), {"limit": limit}, retries=3, timeout=90)
        return None if d is None else d.get("items", [])

    items = _get(f"{frm:%Y%m%d}-{to:%Y%m%d}")
    if items is None:
        raise RuntimeError(f"TDnet の取得に失敗 {since_date}〜{until_date}")
    if len(items) >= limit:
        items = []
        day = frm
        while day <= to:
            got = _get(f"{day:%Y%m%d}")
            if got is None:
                raise RuntimeError(f"TDnet の取得に失敗 {day:%Y-%m-%d}")
            if len(got) >= limit:
                raise RuntimeError(f"TDnet {day:%Y-%m-%d} が {limit} 件を超えた。limit を上げること")
            items.extend(got)
            day += timedelta(days=1)

    by_code: dict[str, list[dict]] = {}
    for it in items:
        td = it.get("Tdnet", it)
        code = _norm_code(td.get("company_code", ""))
        if not re.match(r"^[0-9][0-9A-Z][0-9][0-9A-Z]$", code):
            continue
        pub = td.get("pubdate") or ""
        by_code.setdefault(code, []).append({
            "date": pub[:10], "title": td.get("title", ""),
            "url": td.get("document_url", ""), "source": "tdnet",
            "published_at": _published(pub[:10], pub[11:])})
    print(f"    [TDnet] {since_date}〜{until_date}: {len(items)} 件 / {len(by_code)} 社", flush=True)
    if by_code:
        _save_cache(cf, by_code)
    return by_code



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
        raise RuntimeError("EDINET_API_KEY 未設定")

    params = {"date": date, "type": 2, "Subscription-Key": api_key}
    data = _get_json(EDINET_API, params, retries=3, timeout=30, base_pause=1.5)
    if not data or data.get("StatusCode") not in (None, 200):
        msg = (data or {}).get("message", "no data")
        raise RuntimeError(f"EDINET の取得に失敗 {date}: {str(msg)[:80]}")

    by_code: dict[str, list[dict]] = {}
    for doc in data.get("results", []):
        sec_code = (doc.get("secCode") or "").strip()
        if len(sec_code) < 4:
            continue
        code = sec_code[:4]
        submit = doc.get("submitDateTime") or ""
        submit_date = submit[:10] or date
        desc = doc.get("docDescription") or ""
        filer = doc.get("filerName") or ""
        by_code.setdefault(code, []).append({
            "date": submit_date,
            "title": f"{desc}（{filer}）" if filer else desc,
            "url": "",
            "source": "edinet",
            "published_at": _published(submit_date, submit[11:]),
        })

    if by_code:
        _save_cache(cache_file, by_code)
    n = sum(len(v) for v in by_code.values())
    print(f"    [EDINET] {date}: {n} docs, {len(by_code)} codes")
    return by_code



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
        raise FetchFailed("error_no_bs4")
    url = f"https://kabutan.jp/stock/news?code={code}"
    client = session or requests
    try:
        r = client.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        if r.status_code != 200:
            raise FetchFailed(f"http_{r.status_code}")
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        now = datetime.now()
        if soup.select_one(".s_news_list") is None:
            raise FetchFailed("no_list")  # 想定したページでない(一覧の枠が無い)。0 件と区別する
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
            date_str = None  # 日付が読めない見出しは保存しない(今日の日付で埋めない)
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
            if not date_str:
                continue
            out.append({
                "date": date_str,
                "title": title,
                "url": href,
                "source": "kabutan",
                "published_at": _published(date_str, date_raw),
            })
        return out
    except FetchFailed:
        raise
    except Exception as e:
        raise FetchFailed(f"error_{type(e).__name__}") from e



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
        raise FetchFailed("error_no_bs4")
    url = f"https://minkabu.jp/stock/{code}/news"
    client = session or requests
    try:
        r = client.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        if r.status_code != 200:
            raise FetchFailed(f"http_{r.status_code}")
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
            date_str = None  # 日付が読めない見出しは保存しない(今日の日付で埋めない)
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
            if not date_str:
                continue
            out.append({
                "date": date_str,
                "title": title,
                "url": href,
                "source": f"minkabu({orig_source})" if orig_source != "minkabu" else "minkabu",
                "published_at": _published(date_str, time_text),
            })
        return out
    except FetchFailed:
        raise
    except Exception as e:
        raise FetchFailed(f"error_{type(e).__name__}") from e



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
        raise FetchFailed("error_no_bs4")
    url = f"https://finance.yahoo.co.jp/quote/{code}.T/news"
    client = session or requests
    try:
        r = client.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        if r.status_code != 200:
            raise FetchFailed(f"http_{r.status_code}")
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
            # 表記は "9/25"(当日以外・時刻なし)か "8:50"(当日)。2026-09-26 に実ページで確認
            date_str = None  # 日付が読めない見出しは保存しない(今日の日付で埋めない)
            try:
                if "/" in time_text:
                    mm, dd = (int(x) for x in time_text.split()[0].split("/"))
                    yr = now.year if mm <= now.month else now.year - 1
                    date_str = f"{yr:04d}-{mm:02d}-{dd:02d}"
                elif _HHMM.fullmatch(time_text):
                    date_str = now.strftime("%Y-%m-%d")
            except Exception:
                pass
            if not date_str:
                continue
            out.append({
                "date": date_str,
                "title": title,
                "url": href,
                "source": f"yahoojp({orig_source})" if orig_source != "yahoo" else "yahoojp",
                "published_at": _published(date_str, time_text),
            })
        return out
    except FetchFailed:
        raise
    except Exception as e:
        raise FetchFailed(f"error_{type(e).__name__}") from e



def _parse_nikkei_date(text: str, now: datetime) -> str:
    """
    日経の銘柄別ニュース一覧の日付表記を YYYY-MM-DD に変換する。
    実表記は4パターン: "18:13"(当日・時刻のみ) / "9/16" / "2025/12/5" /
    "9/15更新"。年が省略された表記で月日が未来になる場合は前年とみなす。
    解釈できない場合は "" を返す(呼び出し側はその見出しを保存しない)。
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
        raise FetchFailed("error_no_bs4")
    url = f"https://www.nikkei.com/nkd/company/news/?scode={code}"
    client = session or requests
    try:
        r = client.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "ja-JP,ja;q=0.9",
        })
        if r.status_code != 200:
            raise FetchFailed(f"http_{r.status_code}")
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        out = []
        now = datetime.now()
        rows = soup.select("li.m-listFormat_item")
        if not rows:
            # 2026-09-29: 長時間の連続取得中、一覧の無いページ(HTTP 200)が返り、2,849 銘柄が
            # 「0 件」と記録された(同じ銘柄を後で取ると見出しがあった)。日経の銘柄ページは
            # 過去の記事も並ぶので一覧が空になることはまず無い。一覧が無ければ取得失敗として扱う
            raise FetchFailed("no_list")
        for li in rows[:max_items]:
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
            time_text = time_el.get_text(strip=True) if time_el else ""
            if "更新" in time_text:
                continue  # 更新日時しか分からない(初回公開日時と混同しない)
            date_str = _parse_nikkei_date(time_text, now)
            if not date_str:
                continue  # 日付が読めない見出しは保存しない(今日の日付で埋めない)
            out.append({"date": date_str, "title": title, "url": href, "source": "nikkei",
                        "published_at": _published(date_str, time_text)})
        return out
    except FetchFailed:
        raise
    except Exception as e:
        raise FetchFailed(f"error_{type(e).__name__}") from e



def _fetch_per_code(codes: list[str], fetch_fn, label: str, pause: float = 1.0,
                    stop_after: int = 20) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """銘柄ごとの見出しを 1 サイトにつき 1 本ずつ、pause 秒以上空けて取る。

    戻り値: (銘柄 -> 見出し, 銘柄 -> 取得状態)。取得状態は
      ok(見出しあり) / empty(ページは取れたが見出し 0 件) / http_403 などの失敗 /
      skipped_blocked(失敗が stop_after 回続いたので、それ以降は取りに行かなかった)
    2026-09-28: 旧版は 1 サイト 3 並列・0.5 秒間隔で、失敗を空の結果として返していたため、
    みんかぶの 403 やYahooの途中からの遮断が「その銘柄に材料が無い」と区別できなかった。
    失敗が続いたら取りに行くのをやめるのは、遮断をさらに長引かせないため。
    """
    by_code: dict[str, list[dict]] = {}
    status: dict[str, str] = {}
    session = requests.Session()
    streak = 0
    try:
        for i, code in enumerate(codes):
            if streak >= stop_after:
                for rest in codes[i:]:
                    status[rest] = "skipped_blocked"
                print(f"    [{label}] 失敗が {stop_after} 回続いたので打ち切り "
                      f"({len(codes) - i} 銘柄は未取得)", flush=True)
                break
            try:
                items = fetch_fn(code, session=session)
                streak = 0
                status[code] = "ok" if items else "empty"
                if items:
                    by_code[code] = items
            except FetchFailed as e:
                streak += 1
                status[code] = e.status
            time.sleep(pause)
            if (i + 1) % 200 == 0:
                print(f"    [{label}] {i + 1}/{len(codes)} {_tally(status)}", flush=True)
    finally:
        session.close()
    return by_code, status


def _tally(status: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in status.values():
        out[v] = out.get(v, 0) + 1
    return dict(sorted(out.items()))



# ---------- 保存 ----------

def title_key(title: str) -> str:
    """同じ見出しを 1 件にまとめるためのキー(全角半角・空白の揺れを吸収)。"""
    t = unicodedata.normalize("NFKC", title or "")
    return re.sub(r"\s+", "", t)


def store(by_code: dict[str, list[dict]], since: str, until: str,
          codes: set[str] | None = None) -> int:
    """見出しを news に保存する。since〜until の日付のものだけ。既存は重複させない。"""
    rows = []
    for code, items in by_code.items():
        if codes is not None and code not in codes:
            continue
        for it in items:
            d = it.get("date") or ""
            title = (it.get("title") or "").strip()
            if not title or not (since <= d <= until):
                continue
            rows.append((code, d, it.get("source") or "", title, title_key(title),
                         it.get("url") or "", it.get("published_at")))
    if not rows:
        return 0
    with db.cursor() as conn:
        before = conn.execute("SELECT COUNT(*) n FROM news").fetchone()["n"]
        conn.executemany(
            """INSERT INTO news(code,date,source,title,title_key,url,published_at)
               VALUES(%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(code,source,title,date) DO NOTHING""", rows)
        after = conn.execute("SELECT COUNT(*) n FROM news").fetchone()["n"]
    return after - before


OK_STATUSES = ("ok", "empty")
# みんかぶは 2026-09-28〜30 の 3 日続けてこのPCから全銘柄 403 のため取得をやめた(関数は残す)
PER_CODE_SOURCES = ("kabutan", "yahoojp", "nikkei")


def store_coverage(base_date: str, rows: list[tuple[str, str, str]]) -> None:
    """取得の成否を news_coverage に保存する。rows = [(source, code, status)]。
    同じ基準日で取り直したとき、前に取れていた銘柄を失敗で上書きしない。"""
    if not rows:
        return
    with db.cursor() as conn:
        conn.executemany(
            """INSERT INTO news_coverage(base_date,source,code,status) VALUES(%s,%s,%s,%s)
               ON CONFLICT(base_date,source,code) DO UPDATE
                 SET status=EXCLUDED.status, fetched_at=now()
                 WHERE EXCLUDED.status IN ('ok','empty') OR news_coverage.status NOT IN ('ok','empty')""",
            [(base_date, src, code, st) for src, code, st in rows])


NIKKEI_PAUSE = 3.0


def collect(codes: list[str], since: str, until: str, base_date: str | None = None,
            nikkei_codes: list[str] | None = None) -> dict:
    """全銘柄の見出しを全ソースから取得して保存する。ソースごとの件数と取得状態の内訳を返す。
    base_date を渡すと、取得の成否を news_coverage に記録する。
    日経は nikkei_codes だけを NIKKEI_PAUSE 秒間隔で取る(None なら全銘柄)。2026-09-29・30 に 1 秒間隔だと
    約 40 銘柄で一覧の無いページが返るようになり、10/1 の試験では 3 秒間隔で 100 銘柄すべて取れた。
    全銘柄を 3 秒間隔で取ると約 2 時間 40 分かかるため、その日に動いた銘柄に絞る。"""
    from concurrent.futures import ThreadPoolExecutor

    codeset = set(codes)
    days = (datetime.strptime(until, "%Y-%m-%d") - datetime.strptime(since, "%Y-%m-%d")).days

    def _tdnet():
        return fetch_tdnet_range(since, until), None

    def _edinet():
        merged: dict[str, list[dict]] = {}
        for d in range(days + 1):
            ds = (datetime.strptime(since, "%Y-%m-%d") + timedelta(days=d)).strftime("%Y-%m-%d")
            for code, items in fetch_edinet_docs(ds).items():
                merged.setdefault(code, []).extend(items)
        return merged, None

    fetchers = {  # 配信元 -> (取得関数, 対象銘柄, 間隔秒)
        "kabutan": (fetch_kabutan_news, codes, 1.0),
        "yahoojp": (fetch_yahoo_jp_news, codes, 1.0),
        "nikkei": (fetch_nikkei_news, codes if nikkei_codes is None else nikkei_codes, NIKKEI_PAUSE),
    }
    jobs = {"tdnet": _tdnet, "edinet": _edinet}
    for name, (fn, targets, pause) in fetchers.items():
        # サイトが違えば同時に取ってよい。1 サイトの中は 1 本ずつ(_fetch_per_code)
        jobs[name] = (lambda fn=fn, name=name, targets=targets, pause=pause:
                      _fetch_per_code(targets, fn, name, pause=pause))

    counts: dict[str, dict] = {}
    cov: list[tuple[str, str, str]] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {name: ex.submit(fn) for name, fn in jobs.items()}
        for name, fut in futs.items():
            try:
                by_code, status = fut.result()
                counts[name] = {"codes": len(by_code),
                                "stored": store(by_code, since, until, codeset)}
                if status is None:
                    cov.append((name, "*", "ok"))
                else:
                    counts[name]["status"] = _tally(status)
                    cov.extend((name, c, st) for c, st in status.items())
            except Exception as e:  # 1 ソースの失敗で全体を止めない。記録は残す
                counts[name] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}
                cov.append((name, "*", f"error_{type(e).__name__}"))
    if base_date:
        store_coverage(base_date, cov)
    return counts


def coverage(conn, base_date: str, codes: list[str] | None = None) -> dict:
    """基準日の取得状況。{"bulk": {source: status}, "per_code": {code: {source: status}}}。
    記録の無い配信元は not_recorded。"""
    rows = conn.execute("SELECT source, code, status FROM news_coverage WHERE base_date=%s",
                        (base_date,)).fetchall()
    bulk = {"tdnet": "not_recorded", "edinet": "not_recorded"}
    per: dict[str, dict[str, str]] = {}
    for r in rows:
        if r["code"] == "*":
            bulk[r["source"]] = r["status"]
        elif codes is None or r["code"] in codes:
            per.setdefault(r["code"], {})[r["source"]] = r["status"]
    for c in (codes or per.keys()):
        d = per.setdefault(c, {})
        for src in PER_CODE_SOURCES:
            d.setdefault(src, "not_recorded")
    return {"bulk": bulk, "per_code": per}


def material_checked(bulk: dict[str, str], per_code: dict[str, str]) -> bool:
    """「新規材料なし」と言える取得状況か。TDnet が取れていて、銘柄別のニュースを
    少なくとも 1 サイトで取れた(0 件を含む)こと。満たさなければ「新規材料確認不能」。"""
    return bulk.get("tdnet") == "ok" and any(per_code.get(s) in OK_STATUSES for s in PER_CODE_SOURCES)
