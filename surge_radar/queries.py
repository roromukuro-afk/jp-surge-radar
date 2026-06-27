"""Web表示用の読み取りクエリ。"""
from __future__ import annotations

import json

from . import db
from .db import loadj


def latest_run_date() -> str | None:
    with db.cursor() as conn:
        r = conn.execute("SELECT MAX(run_date) d FROM predictions").fetchone()
    return r["d"] if r and r["d"] else None


def run_dates(limit: int = 30) -> list[str]:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT DISTINCT run_date FROM predictions ORDER BY run_date DESC LIMIT %s", (limit,)).fetchall()
    return [r["run_date"] for r in rows]


def ranking(run_date: str | None = None, category: str | None = None, limit: int = 100) -> list[dict]:
    run_date = run_date or latest_run_date()
    if not run_date:
        return []
    q = "SELECT * FROM predictions WHERE run_date=%s"
    args = [run_date]
    if category and category != "ALL":
        q += " AND category=%s"
        args.append(category)
    q += " ORDER BY rank ASC LIMIT %s"
    args.append(limit)
    with db.cursor() as conn:
        rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["reasons"] = loadj(d.get("reasons"), [])
        d["failure_conditions"] = loadj(d.get("failure_conditions"), [])
        d["flags"] = loadj(d.get("flags"), {})
        out.append(d)
    return out


def category_summary(run_date: str | None = None) -> dict:
    run_date = run_date or latest_run_date()
    if not run_date:
        return {}
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) n FROM predictions WHERE run_date=%s GROUP BY category",
            (run_date,)).fetchall()
    return {r["category"]: r["n"] for r in rows}


def prediction_detail(pid: int) -> dict | None:
    with db.cursor() as conn:
        p = conn.execute("SELECT * FROM predictions WHERE id=%s", (pid,)).fetchone()
        if not p:
            return None
        oc = conn.execute("SELECT * FROM prediction_outcomes WHERE prediction_id=%s", (pid,)).fetchone()
        mats = conn.execute(
            "SELECT date,category,title,url,sentiment,impact,persistence,material_type,"
            "unpriced,connect,chart_reaction,volume_reaction,risk,ai_comment "
            "FROM materials WHERE code=%s ORDER BY date DESC LIMIT 15", (p["code"],)).fetchall()
        prices = conn.execute(
            "SELECT date,open,high,low,close,volume FROM prices WHERE code=%s ORDER BY date DESC LIMIT 120",
            (p["code"],)).fetchall()
    d = dict(p)
    d["reasons"] = loadj(d.get("reasons"), [])
    d["failure_conditions"] = loadj(d.get("failure_conditions"), [])
    d["flags"] = loadj(d.get("flags"), {})
    d["features"] = loadj(d.get("features"), {})
    d["outcome"] = dict(oc) if oc else None
    if d["outcome"]:
        d["outcome"]["failure_tags"] = loadj(d["outcome"].get("failure_tags"), [])
    d["materials"] = [dict(m) for m in mats]
    price_list = list(reversed([dict(x) for x in prices]))  # 昇順
    closes = [b["close"] for b in price_list]
    for i, b in enumerate(price_list):
        b["ma5"]  = round(sum(closes[max(0,i-4):i+1])  / min(i+1, 5),  1) if i >= 4  else None
        b["ma25"] = round(sum(closes[max(0,i-24):i+1]) / min(i+1, 25), 1) if i >= 24 else None
        b["ma75"] = round(sum(closes[max(0,i-74):i+1]) / min(i+1, 75), 1) if i >= 74 else None
    d["prices"] = price_list
    # SVGチャート用: MA折れ線ポイント文字列を事前計算 (Jinja2のループ変数スコープ回避)
    d["chart_meta"] = _chart_meta(price_list, d["run_date"])
    return d


def _chart_meta(prices: list[dict], run_date: str) -> dict:
    """SVGチャート描画用のMA折れ線ポイント文字列とT0インデックスを返す。"""
    step = 7
    ph, pad = 200, 4
    if not prices:
        return {}
    hi = max(b["high"] for b in prices)
    lo = min(b["low"] for b in prices)
    rng = (hi - lo) or 1

    def _y(price: float) -> float:
        return round(pad + (1 - (price - lo) / rng) * (ph - 2 * pad), 2)

    pts5, pts25, pts75 = [], [], []
    t0_x = None
    for i, b in enumerate(prices):
        x = round(i * step + step / 2, 1)
        if b["date"] == run_date:
            t0_x = x
        if b["ma5"] is not None:
            pts5.append(f"{x},{_y(b['ma5'])}")
        if b["ma25"] is not None:
            pts25.append(f"{x},{_y(b['ma25'])}")
        if b["ma75"] is not None:
            pts75.append(f"{x},{_y(b['ma75'])}")

    return {
        "ma5_pts":  " ".join(pts5),
        "ma25_pts": " ".join(pts25),
        "ma75_pts": " ".join(pts75),
        "t0_x": t0_x,
        "hi": hi, "lo": lo, "step": step,
        "W": len(prices) * step,
        "PH": ph, "VH": 52, "pad": pad, "rng": rng,
    }


def history(limit: int = 200) -> list[dict]:
    """判定済み予測の履歴(成否付き)。"""
    with db.cursor() as conn:
        rows = conn.execute(
            """SELECT p.id,p.run_date,p.code,p.name,p.category,p.base_price,p.score,
                      o.result_class,o.max_up_20d,o.days_to_20pct,o.max_drawdown,o.failure_tags
               FROM predictions p JOIN prediction_outcomes o ON o.prediction_id=p.id
               ORDER BY p.run_date DESC, p.rank ASC LIMIT %s""", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["failure_tags"] = loadj(d.get("failure_tags"), [])
        out.append(d)
    return out


def failure_samples(limit: int = 200) -> list[dict]:
    with db.cursor() as conn:
        rows = conn.execute(
            """SELECT t.id,t.code,t.t0_date,t.tags,t.created_at,
                      p.name,p.category,p.score,p.id pred_id,
                      o.max_up_20d,o.max_drawdown,o.result_class
               FROM teacher_samples t
               LEFT JOIN predictions p ON p.id=t.prediction_id
               LEFT JOIN prediction_outcomes o ON o.prediction_id=t.prediction_id
               WHERE t.source='live_fail' ORDER BY t.created_at DESC LIMIT %s""", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = loadj(d.get("tags"), {})
        out.append(d)
    return out


def job_logs(limit: int = 60) -> list[dict]:
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM job_logs ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["counts"] = loadj(d.get("counts"), {})
        out.append(d)
    return out


def accuracy_stats() -> dict:
    """的中率・失敗率の集計 (pure DB — no ML deps)。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT o.result_class, COUNT(*) n FROM prediction_outcomes o "
            "JOIN predictions p ON p.id=o.prediction_id WHERE p.status='judged' "
            "GROUP BY o.result_class").fetchall()
        cat_rows = conn.execute(
            "SELECT p.category, o.result_class, COUNT(*) n FROM prediction_outcomes o "
            "JOIN predictions p ON p.id=o.prediction_id WHERE p.status='judged' "
            "GROUP BY p.category,o.result_class").fetchall()
        tag_rows = conn.execute(
            "SELECT failure_tags FROM prediction_outcomes o JOIN predictions p ON p.id=o.prediction_id "
            "WHERE p.status='judged'").fetchall()
        origin_rows = conn.execute(
            "SELECT COALESCE(p.origin,'live') origin, o.result_class, COUNT(*) n "
            "FROM prediction_outcomes o JOIN predictions p ON p.id=o.prediction_id "
            "WHERE p.status='judged' GROUP BY p.origin, o.result_class").fetchall()

    by_class = {r["result_class"]: r["n"] for r in rows}
    total = sum(by_class.values())
    success = by_class.get("S", 0) + by_class.get("A", 0) + by_class.get("B", 0)
    near = by_class.get("near", 0)

    by_cat: dict = {}
    for r in cat_rows:
        by_cat.setdefault(r["category"], {})[r["result_class"]] = r["n"]

    tag_count: dict = {}
    for r in tag_rows:
        for t in (loadj(r["failure_tags"], []) or []):
            tag_count[t] = tag_count.get(t, 0) + 1

    by_origin_raw: dict = {}
    for r in origin_rows:
        by_origin_raw.setdefault(r["origin"], {})[r["result_class"]] = r["n"]

    def _origin_stats(d: dict) -> dict:
        tot = sum(d.values())
        s = d.get("S", 0) + d.get("A", 0) + d.get("B", 0)
        n = d.get("near", 0)
        return {"total": tot, "by_class": d,
                "hit_rate": round(s / tot, 4) if tot else None,
                "hit_or_near_rate": round((s + n) / tot, 4) if tot else None}

    return {
        "total_judged": total,
        "by_class": by_class,
        "hit_rate": round(success / total, 4) if total else None,
        "hit_or_near_rate": round((success + near) / total, 4) if total else None,
        "by_category": by_cat,
        "failure_tags": dict(sorted(tag_count.items(), key=lambda kv: -kv[1])),
        "by_origin": {k: _origin_stats(v) for k, v in by_origin_raw.items()},
    }


def latest_model_meta() -> dict | None:
    """最新モデルメタデータ (pure DB — no ML deps)。"""
    with db.cursor() as conn:
        r = conn.execute(
            "SELECT version,trained_at,n_samples,n_pos,n_neg,metrics,feature_importance,notes "
            "FROM model_meta ORDER BY trained_at DESC LIMIT 1"
        ).fetchone()
    if not r:
        return None
    d = dict(r)
    d["metrics"] = loadj(d.get("metrics"), {})
    d["feature_importance"] = loadj(d.get("feature_importance"), {})
    return d


def teacher_counts() -> dict:
    """教師データ統計 (pure DB — no ML deps)。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) n, SUM(label) pos FROM teacher_samples GROUP BY source"
        ).fetchall()
        tot = conn.execute("SELECT COUNT(*) n FROM teacher_samples").fetchone()["n"]
        pos_tot = conn.execute("SELECT COUNT(*) n FROM teacher_samples WHERE label=1").fetchone()["n"]
        lf = conn.execute("SELECT tags FROM teacher_samples WHERE source='live_fail'").fetchall()

    source_stats = {r["source"]: {"n": r["n"], "pos": r["pos"] or 0} for r in rows}
    fail_tag_counts: dict = {}
    for r in lf:
        try:
            t = json.loads(r["tags"] or "{}")
            for tag in t.get("fail_tags", []):
                fail_tag_counts[tag] = fail_tag_counts.get(tag, 0) + 1
        except Exception:
            pass

    return {
        "_total": tot,
        "_pos_total": pos_tot,
        "_neg_total": tot - pos_tot,
        "by_source": source_stats,
        "fail_tag_counts": fail_tag_counts,
    }


def overview() -> dict:
    from datetime import datetime, timedelta
    cutoff_30d = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    with db.cursor() as conn:
        sec = conn.execute("SELECT COUNT(*) n FROM securities").fetchone()["n"]
        pr = conn.execute("SELECT COUNT(DISTINCT code) n FROM prices").fetchone()["n"]
        preds = conn.execute("SELECT COUNT(*) n FROM predictions").fetchone()["n"]
        judged = conn.execute("SELECT COUNT(*) n FROM predictions WHERE status='judged'").fetchone()["n"]
        mats = conn.execute("SELECT COUNT(*) n FROM materials").fetchone()["n"]
        last_mat = conn.execute("SELECT MAX(date) d FROM materials").fetchone()
        last_mat_ts = conn.execute("SELECT MAX(created_at) ts FROM materials").fetchone()
        teacher = conn.execute("SELECT COUNT(*) n FROM teacher_samples").fetchone()["n"]
        live_fail = conn.execute("SELECT COUNT(*) n FROM teacher_samples WHERE source='live_fail'").fetchone()["n"]
        live_ok = conn.execute("SELECT COUNT(*) n FROM teacher_samples WHERE source='live_success'").fetchone()["n"]
        latest_rd = conn.execute("SELECT MAX(run_date) d FROM predictions").fetchone()
        cat_today = {}
        if latest_rd and latest_rd["d"]:
            rows = conn.execute(
                "SELECT category, COUNT(*) n FROM predictions WHERE run_date=%s GROUP BY category",
                (latest_rd["d"],)).fetchall()
            cat_today = {r["category"]: r["n"] for r in rows}
        # 材料ソース別件数 (直近30日)
        src_rows = conn.execute(
            "SELECT source, COUNT(*) n FROM materials WHERE date >= %s GROUP BY source ORDER BY n DESC",
            (cutoff_30d,)).fetchall()
        mat_by_source = {r["source"]: r["n"] for r in src_rows}
        # 最新パイプライン実行情報
        last_pipeline = conn.execute(
            "SELECT status, started_at, counts, message FROM job_logs "
            "WHERE job='daily_pipeline' ORDER BY id DESC LIMIT 1").fetchone()
        # 材料が取れている銘柄数 (今日)
        today_str = datetime.now().strftime("%Y-%m-%d")
        mat_codes_today = conn.execute(
            "SELECT COUNT(DISTINCT code) n FROM materials WHERE date=%s", (today_str,)
        ).fetchone()["n"]
    return {
        "securities": sec,
        "priced_codes": pr,
        "predictions": preds,
        "judged": judged,
        "materials": mats,
        "last_material_date": (last_mat["d"] if last_mat and last_mat["d"] else None),
        "last_material_ts": (last_mat_ts["ts"] if last_mat_ts else None),
        "mat_by_source": mat_by_source,
        "mat_codes_today": mat_codes_today,
        "teacher_total": teacher,
        "live_fail": live_fail,
        "live_success": live_ok,
        "latest_run_date": latest_rd["d"] if latest_rd else None,
        "cat_today": cat_today,
        "last_pipeline": dict(last_pipeline) if last_pipeline else None,
    }
