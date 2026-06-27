"""
日次パイプライン (放置自動運用の中核)。

毎日の流れ:
  1. ユニバース更新 (週1で十分だが毎日でも可)
  2. 株価更新 (対象銘柄のOHLCV)
  3. 材料収集 (TDnet)
  4. テーマ地合い更新
  5. 過去予測の追跡・成否判定・教師データ化
  6. (条件成立で)再学習
  7. AI急騰予測ランキング生成
全ステップを job_logs に記録。途中失敗しても可能な範囲で継続。
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime

from . import db, ingest, materials, predict, push_notify, themes, track, train, universe


def _log_start(job: str) -> int:
    with db.cursor() as conn:
        cur = conn.execute(
            "INSERT INTO job_logs(job,started_at,status) VALUES(%s,%s,'running')",
            (job, datetime.now().isoformat(timespec="seconds")))
        return cur.lastrowid


def _log_end(job_id: int, status: str, counts: dict | None = None, message: str = "") -> None:
    with db.cursor() as conn:
        conn.execute(
            "UPDATE job_logs SET finished_at=%s,status=%s,counts=%s,message=%s WHERE id=%s",
            (datetime.now().isoformat(timespec="seconds"), status,
             json.dumps(counts or {}, ensure_ascii=False), message[:2000], job_id))


def step(job: str, fn, *args, **kwargs):
    """WARNING step: exception is logged but pipeline continues."""
    jid = _log_start(job)
    print(f"[{datetime.now():%H:%M:%S}] >>> {job}", flush=True)
    try:
        res = fn(*args, **kwargs)
        _log_end(jid, "ok", res if isinstance(res, dict) else {"result": str(res)})
        print(f"[{datetime.now():%H:%M:%S}] <<< {job}: {res}", flush=True)
        return res
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        _log_end(jid, "error", message=tb)
        print(f"[{datetime.now():%H:%M:%S}] WARN {job}: {e}", flush=True)
        print(tb, file=sys.stderr, flush=True)
        return None


def step_critical(job: str, fn, *args, **kwargs):
    """CRITICAL step: exception aborts the entire pipeline."""
    jid = _log_start(job)
    print(f"[{datetime.now():%H:%M:%S}] >>> {job}", flush=True)
    try:
        res = fn(*args, **kwargs)
        _log_end(jid, "ok", res if isinstance(res, dict) else {"result": str(res)})
        print(f"[{datetime.now():%H:%M:%S}] <<< {job}: {res}", flush=True)
        return res
    except Exception as e:
        tb = traceback.format_exc()
        _log_end(jid, "error", message=tb)
        print(f"[{datetime.now():%H:%M:%S}] CRITICAL {job}: {e}", flush=True)
        print(tb, file=sys.stderr, flush=True)
        raise RuntimeError(f"Critical step '{job}' failed: {e}") from e


def update_universe_step(stale_days: int = 7):
    """Skip remote fetch if securities table was updated within stale_days."""
    with db.cursor() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt, MAX(updated_at) as last FROM securities"
        ).fetchone()
        cnt = row["cnt"] if row else 0
        last = row["last"] if row else None

    if cnt > 100 and last:
        from datetime import timezone
        if hasattr(last, "tzinfo") and last.tzinfo:
            age = (datetime.now(timezone.utc) - last).total_seconds() / 86400
        else:
            age = (datetime.now() - last).total_seconds() / 86400
        if age < stale_days:
            print(f"  [universe] {cnt} securities, updated {age:.1f}d ago — skipping remote fetch", flush=True)
            return {"universe": cnt, "skipped": True}

    print(f"  [universe] fetching remote universe (cnt={cnt}, last={last})", flush=True)
    rows = universe.load_universe()
    n = universe.save_universe(rows)
    return {"universe": n}


def update_prices_step(codes: list[str], range_: str = "2y", pause: float = 0.25,
                       incremental: bool = True):
    """
    incremental=True のとき、直近2日以内にデータがある銘柄はスキップ。
    初回は全件取得(~2h)、以降は差分のみ(数分)。
    """
    if incremental:
        to_fetch = ingest.stale_codes(codes, stale_days=2)
        skipped_fresh = len(codes) - len(to_fetch)
        print(f"    [ingest] incremental: {len(to_fetch)} stale / {skipped_fresh} fresh (skipped)", flush=True)
    else:
        to_fetch = codes
        skipped_fresh = 0

    def prog(i, total, ok, fail):
        print(f"    prices {i}/{total} ok={ok} fail={fail}", flush=True)

    result = ingest.fetch_many(to_fetch, range_=range_, pause=pause,
                               log_every=10, on_progress=prog)
    result["skipped_fresh"] = skipped_fresh
    return result


def collect_materials_step(codes: list[str], pause: float = 0.3, days: int = 14,
                           max_pages: int = 5, time_limit_s: float = 120.0):
    """
    差分取得: DB最新材料日以降のみAPIから取得。
    ソース: TDnet範囲API + EDINET公式API (両方無料・無登録)。
    8時間ファイルキャッシュにより同日複数回の呼び出しも安全。
    """
    from datetime import datetime, timedelta
    codeset = set(codes)
    today = datetime.now().strftime("%Y-%m-%d")
    total = 0
    ok = 0
    disclosures_seen = 0

    # ---- TDnet範囲取得 ----
    last = materials.last_materials_date()
    since_date = None
    if last and last >= today:
        with db.cursor() as conn:
            n = conn.execute(
                "SELECT COUNT(DISTINCT code) n FROM materials WHERE date=%s", (today,)
            ).fetchone()["n"]
        print(f"    [materials] TDnet already current (last={last}), today codes={n}")
    else:
        if last:
            since_dt = datetime.strptime(last, "%Y-%m-%d") + timedelta(days=1)
            since_date = since_dt.strftime("%Y-%m-%d")
            print(f"    [materials] TDnet incremental fetch since {since_date}")
        by_code = materials.fetch_tdnet_range(days=days, pause=pause, since_date=since_date,
                                               max_pages=max_pages, time_limit_s=time_limit_s)
        for code, items in by_code.items():
            if codeset and code not in codeset:
                continue
            if items:
                n = materials.store_materials(code, items)
                total += n
                if n > 0:
                    ok += 1
        disclosures_seen = sum(len(v) for v in by_code.values())

    # ---- EDINET 公式API (無料・登録不要。直近3日分を取得) ----
    try:
        for d in range(0, min(days, 3)):
            dt_str = (datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d")
            edinet_by = materials.fetch_edinet_docs(dt_str)
            for code, items in edinet_by.items():
                if codeset and code not in codeset:
                    continue
                if items:
                    n = materials.store_materials(code, items)
                    total += n
                    if n > 0:
                        ok += 1
    except Exception as e:
        print(f"    [EDINET] error: {e}")

    return {"codes_with_materials": ok, "materials_stored": total,
            "disclosures_seen": disclosures_seen,
            "since_date": since_date or (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")}


def themes_step(asof: str):
    reg = themes.update_theme_regime(asof)
    return {"themes": len(reg)}


def run_daily(*, limit: int | None = None, price_range: str = "2y",
              skip_materials: bool = False, retrain_if_needed: bool = True,
              price_pause: float = 0.25, material_pause: float = 0.3,
              material_days: int = 14, material_max_pages: int = 5,
              material_time_limit: float = 120.0,
              update_universe: bool = True) -> dict:
    """日次フル実行。limit で銘柄数を制限可(テスト/初回向け)。"""
    db.init_db()
    asof = datetime.now().strftime("%Y-%m-%d")
    summary: dict = {"asof": asof}
    pipeline_jid = _log_start("daily_pipeline")
    try:
        # --- CRITICAL steps: failure aborts pipeline ---
        if update_universe:
            step_critical("universe", update_universe_step)
        else:
            print(f"[{datetime.now():%H:%M:%S}] universe update skipped", flush=True)

        codes = universe.get_target_codes()
        if not codes:
            print(f"[{datetime.now():%H:%M:%S}] universe empty, seeding from local...", flush=True)
            seed_rows = universe.load_universe(use_remote=False)
            universe.save_universe(seed_rows)
            codes = universe.get_target_codes()
        if limit:
            codes = codes[:limit]
        summary["target_codes"] = len(codes)
        print(f"[{datetime.now():%H:%M:%S}] target codes: {len(codes)}", flush=True)

        summary["prices"] = step_critical("ingest", update_prices_step,
                                          codes, price_range, price_pause)

        # --- WARNING steps: failure logs but pipeline continues ---
        if not skip_materials:
            summary["materials"] = step(
                "materials", collect_materials_step, codes,
                material_pause, material_days, material_max_pages, material_time_limit)

        summary["themes"] = step("themes", themes_step, asof)
        summary["track"] = step("track", track.track_all, asof)

        if retrain_if_needed:
            summary["teacher_status"] = step("teacher_status", train.ensure_historical)
            summary["retrain"] = step("train", train.retrain, f"daily {asof}")

        # --- CRITICAL: predict must succeed ---
        summary["predict"] = step_critical("predict", predict.generate, asof,
                                           use_materials=not skip_materials)

        # top-code material enrichment: warning only
        if not skip_materials:
            try:
                with db.cursor() as conn:
                    top_codes = [r["code"] for r in conn.execute(
                        "SELECT code FROM predictions WHERE run_date=%s ORDER BY score DESC LIMIT 100",
                        (asof,)).fetchall()]
                if top_codes:
                    summary["enrich"] = step("enrich_materials",
                                             materials.enrich_top_codes, top_codes, asof)
            except Exception as e:
                print(f"    [enrich] skip: {e}", flush=True)

        pred_summary = summary.get("predict", {}) or {}
        n_ab = (pred_summary.get("A", 0) or 0) + (pred_summary.get("B", 0) or 0)
        _log_end(pipeline_jid, "ok", {
            "asof": asof, "target_codes": summary.get("target_codes", 0),
            "predict": pred_summary,
        })
        print(f"[{datetime.now():%H:%M:%S}] pipeline OK - A/B={n_ab}", flush=True)
        if push_notify.is_configured():
            # 粒度別通知 (A候補/danger_fail/live成功/通常サマリ)
            step("push_notify", push_notify.notify_pipeline_result, summary, asof)
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc()
        _log_end(pipeline_jid, "error", message=tb)
        print(f"[{datetime.now():%H:%M:%S}] pipeline FAILED: {e}", flush=True)
        if push_notify.is_configured():
            try:
                push_notify.send_all(
                    title="急騰レーダー パイプライン失敗",
                    body=f"{asof}: {str(e)[:120]}",
                    url="/logs", tag="pipeline-error")
            except Exception:
                pass
        raise
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="surge_radar daily pipeline")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--range", default="2y")
    ap.add_argument("--skip-materials", action="store_true")
    ap.add_argument("--no-retrain", action="store_true")
    a = ap.parse_args()
    out = run_daily(limit=a.limit, price_range=a.range,
                    skip_materials=a.skip_materials, retrain_if_needed=not a.no_retrain)
    print(json.dumps(out, ensure_ascii=False, indent=2))
