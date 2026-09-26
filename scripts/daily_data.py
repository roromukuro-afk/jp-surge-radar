"""
日次のデータ処理(Claude の判断を含まない部分)。

  1. 全銘柄の日足を取得
  2. 基準日(日足が揃っている最新の営業日)のスナップショットを作る
  3. 全銘柄のニュース見出しを取得して保存
  4. 追跡中の候補の成否を更新

使い方: python scripts/daily_data.py [--skip-prices] [--skip-news] [--limit N]
  --limit N は動作確認用に銘柄数を絞る。
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime

import _boot  # noqa: F401

from surge_radar import db, ingest, news, snapshot, track, universe


def _log_start(job: str) -> int:
    with db.cursor() as conn:
        return conn.execute("INSERT INTO runs(job) VALUES(%s) RETURNING id", (job,)).fetchone()["id"]


def _log_end(run_id: int, status: str, counts: dict, message: str = "") -> None:
    with db.cursor() as conn:
        conn.execute("UPDATE runs SET finished_at=now(), status=%s, counts=%s, message=%s "
                     "WHERE id=%s", (status, db.j(counts), message[:2000], run_id))


def base_date() -> str:
    """日足が 1000 銘柄以上揃っている最新の日付。"""
    with db.cursor() as conn:
        r = conn.execute("SELECT date FROM prices GROUP BY date HAVING COUNT(*) > 1000 "
                         "ORDER BY date DESC LIMIT 1").fetchone()
    return r["date"]


def news_since(bd: str) -> str:
    """見出しを取りに行く起点。初回は保存している価格の最初の日、以降は前回の基準日。"""
    with db.cursor() as conn:
        prev = conn.execute("SELECT MAX(date) d FROM snapshots WHERE date < %s", (bd,)).fetchone()["d"]
        first = conn.execute("SELECT MIN(date) d FROM prices").fetchone()["d"]
    return prev or first


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-prices", action="store_true")
    ap.add_argument("--skip-news", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    db.init_db()
    run_id = _log_start("daily_data")
    counts: dict = {}
    t0 = time.monotonic()
    try:
        codes = universe.get_target_codes()
        if a.limit:
            codes = codes[:a.limit]
        counts["universe"] = len(codes)

        if not a.skip_prices:
            r = ingest.fetch_many(codes, range_="5d", workers=8)
            counts["prices"] = {k: r[k] for k in ("ok", "fail", "rows")}
            print(f"[prices] {counts['prices']}  {time.monotonic()-t0:.0f}s", flush=True)

        bd = base_date()
        counts["base_date"] = bd
        counts["snapshot"] = snapshot.build(bd)
        print(f"[snapshot] {counts['snapshot']}  {time.monotonic()-t0:.0f}s", flush=True)

        if not a.skip_news:
            with db.cursor() as conn:
                snap_codes = [r["code"] for r in conn.execute(
                    "SELECT code FROM snapshots WHERE date=%s", (bd,)).fetchall()]
            if a.limit:
                snap_codes = [c for c in snap_codes if c in set(codes)]
            since = news_since(bd)
            until = datetime.now().strftime("%Y-%m-%d")
            counts["news_window"] = [since, until]
            counts["news"] = news.collect(snap_codes, since, until)
            print(f"[news] {json.dumps(counts['news'], ensure_ascii=False)}  "
                  f"{time.monotonic()-t0:.0f}s", flush=True)

        counts["track"] = track.update_all()
        counts["elapsed_s"] = round(time.monotonic() - t0)
        _log_end(run_id, "ok", counts)
        print(json.dumps(counts, ensure_ascii=False, indent=1))
    except Exception as e:
        counts["elapsed_s"] = round(time.monotonic() - t0)
        _log_end(run_id, "error", counts, f"{type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    main()
