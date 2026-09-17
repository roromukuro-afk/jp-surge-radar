"""
深掘り分析(deep_analysis)の追跡・成否判定。

predictions とは独立した第2の予測系列として、同じ +20%/20営業日 の基準で
判定する。判定クラスは labeling.classify_result をそのまま使うため、
エンジンの判定と深掘りの判定を同じ物差しで比較できる。

predictions 側との意図的な2つの違い:

1. 基準価格は deep_analysis.base_price(分析時に見ていた値)を正とする。
   labeling.forward_outcome() は判定時の prices から終値を引き直すため、
   遡及的な株価修正や取得タイミングのズレで分析時と違う基準になることが
   あった(2026-09にB判定の307件中93件で乖離を確認)。ここでは分析が実際に
   前提とした価格で評価する。

2. status='judged' は bars_tracked >= JUDGE_WINDOW を満たしたときだけ。
   predictions 側は「+20%到達で即確定」も finalize 条件に含むため、確定済み
   サンプルが早期成功に偏る(この偏りが2026-09にB判定の成功率を78%と
   誤表示させた原因)。深掘り側は満期のみを確定とし、集計した時点で
   バイアスが入らないようにする。早期到達は days_to_20pct に記録されるので
   情報は失われない。
"""
from __future__ import annotations

from datetime import datetime

from . import db, ingest, labeling
from .config import JUDGE_WINDOW


def _outcome_from_stored_base(df, idx: int, base_price: float) -> dict | None:
    """保存済み base_price を基準に、T0以降の値動きから成否指標を計算する。"""
    if idx is None or idx >= len(df) - 1 or not base_price or base_price <= 0:
        return None
    fwd = df.iloc[idx + 1: idx + 1 + JUDGE_WINDOW].reset_index(drop=True)
    if fwd.empty:
        return None
    highs = fwd["high"].astype(float)
    lows = fwd["low"].astype(float)
    ups = (highs / base_price) - 1.0
    downs = (lows / base_price) - 1.0

    days_to_20 = None
    for i, u in enumerate(ups, start=1):
        if u >= 0.20:
            days_to_20 = i
            break
    return {
        "bars_tracked": len(fwd),
        "max_up_5d": float(ups.head(5).max()),
        "max_up_10d": float(ups.head(10).max()),
        "max_up_20d": float(ups.max()),
        "days_to_20pct": days_to_20,
        "max_drawdown": float(downs.min()),
        "_highs": highs,
        "_lows": lows,
    }


def track_deep(asof: str | None = None) -> dict:
    asof = asof or datetime.now().strftime("%Y-%m-%d")
    with db.cursor() as conn:
        rows = conn.execute("SELECT * FROM deep_analysis WHERE status='open'").fetchall()
    if not rows:
        return {"open": 0, "updated": 0, "judged": 0}

    codes = sorted({r["code"] for r in rows})
    hist_map = ingest.load_history_bulk(codes, lookback_days=60, as_of=asof)

    updated = judged = 0
    result_counts: dict[str, int] = {}
    with db.cursor() as conn:
        for r in rows:
            df = hist_map.get(r["code"])
            if df is None or df.empty:
                continue
            base_date = r["base_date"] or r["run_date"]
            prior = df.index[df["date"] <= base_date]
            if len(prior) == 0:
                continue
            idx = int(prior[-1])
            oc = _outcome_from_stored_base(df, idx, float(r["base_price"] or 0))
            if not oc:
                continue

            res, tags = labeling.classify_result(oc)
            highs, lows = oc.pop("_highs"), oc.pop("_lows")
            hit_reach = int(bool(r["reachable_low"]) and (highs >= float(r["reachable_low"])).any())
            hit_fail = int(bool(r["failure_line"]) and (lows <= float(r["failure_line"])).any())

            conn.execute(
                """INSERT INTO deep_analysis_outcomes
                   (analysis_id,judged_date,bars_tracked,max_up_5d,max_up_10d,max_up_20d,
                    days_to_20pct,max_drawdown,result_class,failure_tags,
                    hit_reachable,hit_failure_line)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(analysis_id) DO UPDATE SET
                     judged_date=excluded.judged_date,bars_tracked=excluded.bars_tracked,
                     max_up_5d=excluded.max_up_5d,max_up_10d=excluded.max_up_10d,
                     max_up_20d=excluded.max_up_20d,days_to_20pct=excluded.days_to_20pct,
                     max_drawdown=excluded.max_drawdown,result_class=excluded.result_class,
                     failure_tags=excluded.failure_tags,hit_reachable=excluded.hit_reachable,
                     hit_failure_line=excluded.hit_failure_line,
                     updated_at=CURRENT_TIMESTAMP""",
                (r["id"], asof, oc["bars_tracked"], oc["max_up_5d"], oc["max_up_10d"],
                 oc["max_up_20d"], oc["days_to_20pct"], oc["max_drawdown"], res,
                 db.j(tags), hit_reach, hit_fail))
            updated += 1

            # 満期(20営業日完走)のみ確定。早期到達でも確定させない。
            if oc["bars_tracked"] >= JUDGE_WINDOW:
                conn.execute("UPDATE deep_analysis SET status='judged' WHERE id=%s", (r["id"],))
                judged += 1
                result_counts[res] = result_counts.get(res, 0) + 1

    return {"open": len(rows), "updated": updated, "judged": judged,
            "results": result_counts}


def performance(min_n: int = 5) -> dict:
    """満期済みの深掘り分析の成績。entry_type / driver_score 帯ごとに集計する。

    エンジン(predictions)の同期間・同基準の成績も並べて返し、どちらの判定が
    当たっているかを比較できるようにする。
    """
    success = ("S", "A", "B")
    out: dict = {}
    with db.cursor() as conn:
        rows = conn.execute(
            """SELECT d.entry_type, d.driver_score, d.risk_score, d.reachable_ok,
                      d.catalyst_type, o.result_class, o.days_to_20pct,
                      o.hit_reachable, o.hit_failure_line
               FROM deep_analysis d
               JOIN deep_analysis_outcomes o ON o.analysis_id = d.id
               WHERE d.status='judged'""").fetchall()
    if not rows:
        return {"n": 0, "note": "満期済みの深掘り分析はまだありません"}

    def agg(key_fn) -> dict:
        buckets: dict[str, list] = {}
        for r in rows:
            k = key_fn(r)
            if k is None:
                continue
            buckets.setdefault(str(k), []).append(r)
        return {k: {"n": len(v),
                    "success": sum(1 for x in v if x["result_class"] in success),
                    "rate": round(sum(1 for x in v if x["result_class"] in success) / len(v), 3)}
                for k, v in buckets.items() if len(v) >= min_n}

    out["n"] = len(rows)
    out["overall_rate"] = round(
        sum(1 for r in rows if r["result_class"] in success) / len(rows), 3)
    out["by_entry_type"] = agg(lambda r: r["entry_type"])
    out["by_catalyst"] = agg(lambda r: r["catalyst_type"])
    out["by_reachable_ok"] = agg(lambda r: r["reachable_ok"])
    out["by_driver_score"] = agg(
        lambda r: None if r["driver_score"] is None
        else f"{(r['driver_score'] // 10) * 10}s")
    # 分析が置いた Reachable Zone / Failure Line が実際に機能したか
    out["reachable_hit_rate"] = round(
        sum(1 for r in rows if r["hit_reachable"]) / len(rows), 3)
    out["failure_line_hit_rate"] = round(
        sum(1 for r in rows if r["hit_failure_line"]) / len(rows), 3)
    return out
