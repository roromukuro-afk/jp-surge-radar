"""
Yahoo Finance chart API クライアント (yfinanceラッパに依存しない直接実装)。

この環境では yfinance のクッキー/クラム取得が失敗するが、chart エンドポイントは
直接叩けば 200 / JPY データを返すため、こちらを主データ源にする。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
import requests

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; surge-radar/0.1)"}
_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
_SESSION = requests.Session()
_SESSION.headers.update(_HEADERS)


def to_yahoo_symbol(code: str) -> str:
    """証券コード -> Yahooシンボル。数値4桁の日本株は .T を付与。"""
    code = str(code).strip()
    if code.endswith(".T") or code.startswith("^") or code.isalpha():
        return code
    if code.isdigit():
        return f"{code}.T"
    return code


def fetch_ohlcv(code: str, range_: str = "2y", interval: str = "1d",
                retries: int = 3, pause: float = 0.4) -> pd.DataFrame:
    """1銘柄の日足を取得。columns: date(open/high/low/close/volume). 失敗時は空DF。"""
    sym = to_yahoo_symbol(code)
    url = _BASE.format(sym=sym)
    params = {"range": range_, "interval": interval, "includeAdjustedClose": "true"}
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
