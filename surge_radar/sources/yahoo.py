"""
Yahoo Finance chart API クライアント (yfinanceラッパに依存しない直接実装)。

この環境では yfinance のクッキー/クラム取得が失敗するが、chart エンドポイントは
直接叩けば 200 / JPY データを返すため、こちらを主データ源にする。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; surge-radar/0.1)"}
_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_SESSION = requests.Session()
_JST = timezone(timedelta(hours=9))
_SESSION.headers.update(_HEADERS)


def to_yahoo_symbol(code: str) -> str:
    """証券コード -> Yahooシンボル。数値4桁の日本株は .T を付与。"""
    code = str(code).strip()
    if code.endswith(".T") or code.startswith("^") or code.isalpha():
        return code
    if code.isdigit() or re.match(r"^[0-9][0-9A-Z][0-9][0-9A-Z]$", code):
        return f"{code}.T"
    return code


def fetch_ohlcv(code: str, range_: str = "2y", interval: str = "1d",
                retries: int = 3, pause: float = 0.4) -> pd.DataFrame:
    """1銘柄の日足を取得。columns: date(open/high/low/close/volume). 失敗時は空DF。
    返す価格は取得時点までの株式分割を反映した基準(Yahoo の仕様)。取得範囲内の分割は
    df.attrs["splits"] = [{"date", "ratio"}] に入れる(分割前に保存した行と基準が違うことを呼び出し側が判断するため)。"""
    sym = to_yahoo_symbol(code)
    url = _BASE.format(sym=sym)
    params = {"range": range_, "interval": interval, "includeAdjustedClose": "true", "events": "split"}
    last_err = None
    for attempt in range(retries):
        try:
            r = _SESSION.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            r.raise_for_status()
            data = r.json()
            result = (data.get("chart") or {}).get("result")
            if not result:
                return pd.DataFrame()
            res = result[0]
            ts = res.get("timestamp")
            if not ts:
                return pd.DataFrame()
            q = res["indicators"]["quote"][0]
            df = pd.DataFrame({
                "date": [datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d") for t in ts],
                "open": q.get("open"),
                "high": q.get("high"),
                "low": q.get("low"),
                "close": q.get("close"),
                "volume": q.get("volume"),
            })
            df = df.dropna(subset=["close"]).reset_index(drop=True)
            df["turnover"] = df["close"] * df["volume"]
            splits = []
            for sp in ((res.get("events") or {}).get("splits") or {}).values():
                if sp.get("numerator") and sp.get("denominator"):
                    splits.append({"date": datetime.fromtimestamp(sp["date"], tz=_JST).strftime("%Y-%m-%d"),
                                   "ratio": sp["numerator"] / sp["denominator"]})
            df.attrs["splits"] = sorted(splits, key=lambda x: x["date"])
            time.sleep(pause)
            return df
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(pause * (attempt + 1))
    if last_err:
        # 上位でログ。ここでは空を返す。
        pass
    return pd.DataFrame()


def fetch_meta(code: str) -> dict:
    """時価総額・発行株数などのメタ取得 (quoteSummary)。失敗時は空dict。"""
    sym = to_yahoo_symbol(code)
    url = f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
    try:
        r = _SESSION.get(url, params={"modules": "price,defaultKeyStatistics,summaryDetail"}, timeout=15)
        if r.status_code != 200:
            return {}
        res = r.json()["quoteSummary"]["result"]
        return res[0] if res else {}
    except Exception:
        return {}


_JP_STAT = {
    "market_cap_mil_yen": re.compile(r"時価総額\s*(?:用語\s*)?([\d,]+)\s*百万円\s*\(\s*(\d{1,2}/\d{1,2})\s*\)"),
    "shares_outstanding": re.compile(r"発行済株式数\s*(?:用語\s*)?([\d,]+)\s*株\s*\(\s*(\d{1,2}/\d{1,2})\s*\)"),
}


def fetch_jp_stats(code: str) -> dict:
    """Yahoo!ファイナンス日本版の銘柄ページから時価総額・発行済株式数を読む(値と基準日)。
    Float(浮動株)はこのページに無いので返さない。読めなかった項目は含めない(推測で埋めない)。
    旧来の quoteSummary API は 2026-09-26 時点で空を返す(認証が要る)。"""
    from bs4 import BeautifulSoup
    r = _SESSION.get(f"https://finance.yahoo.co.jp/quote/{to_yahoo_symbol(code)}",
                     headers={"Accept-Language": "ja-JP"}, timeout=15)
    if r.status_code != 200:
        return {}
    text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
    out = {}
    for key, rx in _JP_STAT.items():
        m = rx.search(text)
        if m:
            out[key] = int(m.group(1).replace(",", ""))
            out[f"{key}_asof"] = m.group(2)
    return out


def fetch_splits(code: str, range_: str = "3mo") -> list[dict]:
    """株式分割・併合の履歴(Yahoo chart API の events=split)。
    返り値: [{"date": "YYYY-MM-DD"(JST), "ratio": 新株数/旧株数}]。2:1 分割なら ratio=2、1:10 併合なら 0.1。
    取得できなければ例外を投げる(分割が無いことと区別するため)。"""
    r = _SESSION.get(_BASE.format(sym=to_yahoo_symbol(code)),
                     params={"range": range_, "interval": "1d", "events": "split"}, timeout=20)
    r.raise_for_status()
    res = (r.json().get("chart") or {}).get("result")
    if not res:
        raise RuntimeError(f"{code}: chart の結果が空")
    out = []
    for s in ((res[0].get("events") or {}).get("splits") or {}).values():
        num, den = s.get("numerator"), s.get("denominator")
        if num and den:
            d = datetime.fromtimestamp(s["date"], tz=_JST).strftime("%Y-%m-%d")
            out.append({"date": d, "ratio": num / den})
    return sorted(out, key=lambda x: x["date"])
