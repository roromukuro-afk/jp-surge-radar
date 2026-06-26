"""
J-Quants API アダプタ (併用・後付け)。

ユーザーが J-Quants(無料枠) に登録し、リフレッシュトークンを環境変数で渡したときのみ有効化。
  setx JQUANTS_REFRESH_TOKEN "..."   (または JQUANTS_MAIL / JQUANTS_PASSWORD)
未設定なら is_available()==False となり、yfinance(Yahoo) 主データ源で動作する。
"""
from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
import requests

_BASE = "https://api.jquants.com/v1"


def _refresh_token() -> str | None:
    tok = os.environ.get("JQUANTS_REFRESH_TOKEN")
    if tok:
        return tok
    mail = os.environ.get("JQUANTS_MAIL")
    pw = os.environ.get("JQUANTS_PASSWORD")
    if mail and pw:
        try:
            r = requests.post(f"{_BASE}/token/auth_user",
                              json={"mailaddress": mail, "password": pw}, timeout=20)
            r.raise_for_status()
            return r.json().get("refreshToken")
        except Exception:
            return None
    return None


def is_available() -> bool:
    return _refresh_token() is not None


def _id_token() -> str | None:
    rt = _refresh_token()
    if not rt:
        return None
    try:
        r = requests.post(f"{_BASE}/token/auth_refresh", params={"refreshtoken": rt}, timeout=20)
        r.raise_for_status()
        return r.json().get("idToken")
    except Exception:
        return None


def fetch_listed() -> pd.DataFrame:
    """全上場銘柄一覧。未認証なら空DF。"""
    tok = _id_token()
    if not tok:
        return pd.DataFrame()
    try:
        r = requests.get(f"{_BASE}/listed/info", headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        r.raise_for_status()
        rows = r.json().get("info", [])
        return pd.DataFrame(rows)
    except Exception:
        return pd.DataFrame()


def fetch_ohlcv(code: str, from_date: str | None = None) -> pd.DataFrame:
    """日足。未認証なら空DF。Yahoo と同じ列構成に正規化。"""
    tok = _id_token()
    if not tok:
        return pd.DataFrame()
    params = {"code": code}
    if from_date:
        params["from"] = from_date
    try:
        r = requests.get(f"{_BASE}/prices/daily_quotes",
                         headers={"Authorization": f"Bearer {tok}"}, params=params, timeout=30)
        r.raise_for_status()
        rows = r.json().get("daily_quotes", [])
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame(rows)
        out = pd.DataFrame({
            "date": pd.to_datetime(df["Date"]).dt.strftime("%Y-%m-%d"),
            "open": df.get("AdjustmentOpen", df.get("Open")),
            "high": df.get("AdjustmentHigh", df.get("High")),
            "low": df.get("AdjustmentLow", df.get("Low")),
            "close": df.get("AdjustmentClose", df.get("Close")),
            "volume": df.get("AdjustmentVolume", df.get("Volume")),
        }).dropna(subset=["close"])
        out["turnover"] = df.get("TurnoverValue", out["close"] * out["volume"]).values
        return out.reset_index(drop=True)
    except Exception:
        return pd.DataFrame()
