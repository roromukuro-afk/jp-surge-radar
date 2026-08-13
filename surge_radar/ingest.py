"""
価格データ取り込み。Yahoo(主) / J-Quants(併用) から日足を取得し SQLite に保存。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd

from . import db
from .sources import jquants, yahoo


def upsert_prices(code: str, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    rows = [
        (code, r["date"], _f(r.get("open")), _f(r.get("high")), _f(r.get("low")),
         _f(r.get("close")), _f(r.get("volume")), _f(r.get("turnover")))
        for _, r in df.iterrows()
    ]
    with db.cursor() as conn:
        conn.executemany(
            """INSERT INTO prices(code,date,open,high,low,close,volume,turnover)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT(code,date) DO UPDATE SET
                 open=excluded.open,high=excluded.high,low=excluded.low,
                 close=excluded.close,volume=excluded.volume,turnover=excluded.turnover""",
            rows,
        )
    return len(rows)


def _f(x):
    try:
        if x is None or pd.isna(x):
            return None
        return float(x)
    except Exception:
        return None


def fetch_one(code: str, range_: str = "2y") -> int:
    """1銘柄を取得して保存。J-Quants優先(あれば)→Yahoo。返り値は保存行数。"""
    df = pd.DataFrame()
    if jquants.is_available():
        df = jquants.fetch_ohlcv(code)
    if df.empty:
        df = yahoo.fetch_ohlcv(code, range_=range_)
    return upsert_prices(code, df)


def fetch_many(codes: list[str], range_: str = "2y", pause: float = 0.25,
               log_every: int = 100, on_progress=None, workers: int = 8) -> dict:
    """複数銘柄を取得。I/Oバウンドなのでスレッド並列で取得する。

    yfinance は HTTP 取得が支配的なため、逐次だと数千銘柄で 1〜2時間かかり daily が
    タイムアウトする。ThreadPoolExecutor で並列化 (既定8並列)。各スレッドは db.py の
    スレッドローカル接続を使うため DB 書き込みは安全。workers<=1 で従来の逐次動作。
    失敗銘柄は stale のまま翌営業日に再取得される (自己修復)。
    """
    ok = 0; fail = 0; total_rows = 0
    failed_codes: list[str] = []

    if workers <= 1:
        for i, code in enumerate(codes, 1):
            try:
                n = fetch_one(code, range_=range_)
                if n > 0:
                    ok += 1; total_rows += n
                else:
                    fail += 1; failed_codes.append(code)
            except Exception:
                fail += 1; failed_codes.append(code)
            if on_progress and i % log_every == 0:
                on_progress(i, len(codes), ok, fail)
            time.sleep(pause)
        return {"ok": ok, "fail": fail, "rows": total_rows, "failed_codes": failed_codes[:200]}

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(code: str):
        try:
            return code, fetch_one(code, range_=range_), None
        except Exception as e:  # noqa: BLE001
            return code, 0, e

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_one, c) for c in codes]
        for fut in as_completed(futures):
            code, n, err = fut.result()
            done += 1
            if err is None and n > 0:
                ok += 1; total_rows += n
            else:
                fail += 1; failed_codes.append(code)
            if on_progress and done % log_every == 0:
                on_progress(done, len(codes), ok, fail)
    return {"ok": ok, "fail": fail, "rows": total_rows, "failed_codes": failed_codes[:200]}


def load_history(code: str, min_bars: int = 0) -> pd.DataFrame:
    """DBから日足を昇順で取得。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT date,open,high,low,close,volume,turnover FROM prices "
            "WHERE code=%s ORDER BY date ASC", (code,)
        ).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    if min_bars and len(df) < min_bars:
        return df
    return df


def latest_price(code: str) -> dict | None:
    with db.cursor() as conn:
        r = conn.execute(
            "SELECT date,close,volume FROM prices WHERE code=%s ORDER BY date DESC LIMIT 1", (code,)
        ).fetchone()
    return dict(r) if r else None


def available_codes() -> list[str]:
    with db.cursor() as conn:
        rows = conn.execute("SELECT DISTINCT code FROM prices").fetchall()
    return [r["code"] for r in rows]


def load_history_bulk(codes: list[str], chunk: int = 500,
                      lookback_days: int | None = None,
                      as_of: str | None = None) -> dict[str, "pd.DataFrame"]:
    """複数銘柄の日足をまとめて取得 (1クエリ/チャンク)。{code: df(昇順)} を返す。

    predict 等で銘柄ごとに load_history する代わりに使い、DB 往復を激減させる。
    価格データが無い銘柄はキーに含まれない。

    lookback_days: 指定時は直近N暦日分のみ取得しDB転送量を削減する。
    features.build_features は内部で直近260営業日分しか使わない(52週高値計算が
    上限)ため、毎日の predict/track では全2年分を転送する必要がない。値は同一
    (260営業日分あれば計算結果は全期間取得時と完全一致、検証済み)。
    None なら従来通り全期間 (teacher.py の教師データ構築等、広い期間が必要な
    用途向けにデフォルトは維持)。
    as_of: 期間計算の基準日 (YYYY-MM-DD)。省略時は現在日時。過去日付での
    バックフィル実行(predict.generate の asof 引数等)でも正しい範囲を取得する
    ために、呼び出し側の asof をそのまま渡すこと (省略すると常に「今日」基準
    になり、過去日付バックフィル時に必要な期間を取りこぼす)。
    """
    if not codes:
        return {}
    cols = ["date", "open", "high", "low", "close", "volume", "turnover"]
    by_code: dict[str, list] = {}
    since_clause = ""
    extra_params: tuple = ()
    if lookback_days is not None:
        ref = datetime.strptime(as_of, "%Y-%m-%d") if as_of else datetime.now()
        cutoff = (ref - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        since_clause = " AND date >= %s"
        extra_params = (cutoff,)
    for i in range(0, len(codes), chunk):
        part = codes[i:i + chunk]
        ph = ",".join(["%s"] * len(part))
        with db.cursor() as conn:
            rows = conn.execute(
                f"SELECT code,date,open,high,low,close,volume,turnover FROM prices "
                f"WHERE code IN ({ph}){since_clause} ORDER BY code, date ASC",
                tuple(part) + extra_params
            ).fetchall()
        for r in rows:
            by_code.setdefault(r["code"], []).append(r)
    out: dict[str, pd.DataFrame] = {}
    for c, rs in by_code.items():
        out[c] = pd.DataFrame([{k: r[k] for k in cols} for r in rs])
    return out


def stale_codes(codes: list[str], stale_days: int = 0) -> list[str]:
    """
    最新の価格データが (today - stale_days) より古いコードのみ返す。
    既に最新データがある銘柄をスキップするための差分取得支援。

    stale_days=0(既定)は「本日分の行が無ければ必ず再取得対象にする」という
    意味。旧実装は stale_days=2 かつ date>=cutoff の境界が inclusive だった
    ため、休日を挟んで最終営業日データがちょうど2暦日前になる週(例: 月曜取引→
    火曜祝日→水曜)で「まだ新しい」と誤判定され、ほぼ全銘柄が再取得スキップ
    される不具合があった(2026-08-12に発覚)。実際には平日の"通常"ケースでも
    前日データが常に閾値内に収まってしまい、連日ほぼ全銘柄がスキップされ、
    前日終値のまま予測していたことが判明している。
    """
    cutoff = (datetime.now() - timedelta(days=stale_days)).strftime("%Y-%m-%d")
    with db.cursor() as conn:
        fresh = {r["code"] for r in conn.execute(
            "SELECT code FROM prices WHERE date >= %s GROUP BY code", (cutoff,)
        ).fetchall()}
    return [c for c in codes if c not in fresh]


def prune_old_prices(keep_days: int = 760) -> dict:
    """
    保持期間より古い価格行を削除し、Neon等のストレージ使用量を一定に保つ。
    features.build_features は直近260営業日分しか使わず、seed-teacher による
    教師データ再構築も2年(730暦日)あれば十分なため、760日(バッファ込み)より
    古い行は不要。日次パイプラインで毎回呼んでも削除件数はわずかで安価。
    """
    cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    with db.cursor() as conn:
        before = conn.execute("SELECT COUNT(*) n FROM prices WHERE date < %s", (cutoff,)).fetchone()["n"]
        if before:
            conn.execute("DELETE FROM prices WHERE date < %s", (cutoff,))
    return {"cutoff": cutoff, "deleted": before}
