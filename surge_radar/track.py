"""
予測の追跡・成否判定・教師データ化 (学習ループの本丸)。

予測 → 追跡 → 成否判定 → 失敗理由分析 → 教師データ追加 → 再学習 → 的中率改善。
- open な予測について、T0以降の値動きから 5/10/20営業日の成否を判定。
- +20%到達でS/A/B、未達はnear/fail/danger_fail。失敗は具体的タグ付け。
- 20営業日分の追跡が完了したら status=judged にし、live_fail/live_success 教師データを追加。
"""
from __future__ import annotations

from datetime import datetime

from . import db, ingest, labeling, materials, themes
from .config import JUDGE_WINDOW
from .db import loadj


def _bars_since(df, t0_date: str) -> int:
    after = df[df["date"] > t0_date]
    return len(after)


def track_all(asof: str | None = None) -> dict:
    asof = asof or datetime.now().strftime("%Y-%m-%d")
    with db.cursor() as conn:
        preds = conn.execute("SELECT * FROM predictions WHERE status='open'").fetchall()

    judged = 0; updated = 0; live_fail = 0; live_success = 0
    market_now = themes.market_regime(asof).get("score", 0.0)

    for p in preds:
        df = ingest.load_history(p["code"])
        if df.empty:
            continue
        # T0インデックス
        t0 = df.index[df["date"] == p["run_date"]]
        if len(t0) == 0:
            # run_date が休場等。直近のそれ以前の足を採用
            prior = df[df["date"] <= p["run_date"]]
            if prior.empty:
                continue
            idx = prior.index[-1]
        else:
            idx = int(t0[0])

        oc = labeling.forward_outcome(df, idx)
        if oc is None:
            continue

        result, base_tags = labeling.classify_result(oc)
        feats = loadj(p["features"], {})

        # 材料/出来高/テーマの継続確認(事後)
        mat_now = materials.recent_material_score(p["code"], asof)
        material_continued = int(mat_now.get("has_fresh_material", 0) or mat_now.get("n_materials", 0) > 0)
        volume_continued = int(oc["max_up_10d"] > 0 and not oc["faded_after_high"])
        theme_followed = None

        fail_tags = base_tags + labeling.derive_failure_tags(
            oc, feats, material_continued=material_continued,
            volume_continued=volume_continued, theme_followed=theme_followed,
            market_score_now=market_now)
        fail_tags = sorted(set(fail_tags))

        next_learning = _next_learning(result, fail_tags)

        with db.cursor() as conn:
            conn.execute(
                """INSERT INTO prediction_outcomes
                   (prediction_id,judged_date,bars_tracked,max_up_5d,max_up_10d,max_up_20d,
                    days_to_20pct,max_drawdown,close_up_maintained,faded_after_high,
                    material_continued,volume_continued,result_class,failure_tags,notes,next_learning)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(prediction_id) DO UPDATE SET
                     judged_date=excluded.judged_date,bars_tracked=excluded.bars_tracked,
                     max_up_5d=excluded.max_up_5d,max_up_10d=excluded.max_up_10d,
                     max_up_20d=excluded.max_up_20d,days_to_20pct=excluded.days_to_20pct,
                     max_drawdown=excluded.max_drawdown,close_up_maintained=excluded.close_up_maintained,
                     faded_after_high=excluded.faded_after_high,material_continued=excluded.material_continued,
                     volume_continued=excluded.volume_continued,result_class=excluded.result_class,
                     failure_tags=excluded.failure_tags,next_learning=excluded.next_learning,
                     updated_at=CURRENT_TIMESTAMP""",
                (p["id"], asof, oc["bars_tracked"], oc["max_up_5d"], oc["max_up_10d"], oc["max_up_20d"],
                 oc["days_to_20pct"], oc["max_drawdown"], oc["close_up_maintained"], oc["faded_after_high"],
                 material_continued, volume_continued, result, db.j(fail_tags), "", next_learning),
            )
        updated += 1

        # 20営業日追跡完了 or 既に+20%到達 → 確定
        finalize = (oc["bars_tracked"] >= JUDGE_WINDOW) or (oc["days_to_20pct"] is not None)
        if finalize:
            label = labeling.is_success(oc)
            _add_teacher(p, feats, label, fail_tags, result)
            with db.cursor() as conn:
                conn.execute("UPDATE predictions SET status='judged' WHERE id=%s", (p["id"],))
            judged += 1
            if label:
                live_success += 1
            else:
                live_fail += 1

    return {"asof": asof, "open_evaluated": len(preds), "updated": updated,
            "judged": judged, "live_fail": live_fail, "live_success": live_success}


def _add_teacher(pred, feats: dict, label: int, fail_tags: list[str], result: str) -> None:
    if not feats:
        return
    source = "live_success" if label else "live_fail"
    with db.cursor() as conn:
        # 重複防止
        exists = conn.execute(
            "SELECT 1 FROM teacher_samples WHERE prediction_id=%s", (pred["id"],)).fetchone()
        if exists:
            return
        conn.execute(
            "INSERT INTO teacher_samples(source,code,t0_date,label,features,tags,prediction_id)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (source, pred["code"], pred["run_date"], label,
             db.j(feats), db.j({"failure_tags": fail_tags, "result": result}), pred["id"]),
        )


def _next_learning(result: str, tags: list[str]) -> str:
    if result in ("S", "A", "B"):
        return f"成功({result}): この急騰前パターンを正例として強化"
    if result == "near":
        return "惜しい(+10〜20%): 到達余地/出来高継続の重みを微調整"
    msgs = {
        "quick_fail": "予測直後の下落を弾くため初動性/支持線条件を強化",
        "material_fail": "材料の一過性検出(続報・出来高継続)の重みを上げる",
        "chart_fail": "騙しブレイク検出(出来高裏付け)を強化",
        "volume_fail": "天井大商い/上ヒゲの減点を強化",
        "trend_fail": "右肩下がり除外ゲートを強化",
        "theme_fail": "テーマ波及の客観確認を厳格化",
        "market_fail": "地合い悪化時の全体慎重度を上げる",
        "trap_fail": "高値圏下落トラップ除外を強化",
        "dilution_fail": "希薄化/資金調達リスクの減点を強化",
        "liquidity_fail": "流動性ゲートを厳格化",
    }
    picked = [msgs[t] for t in tags if t in msgs]
    return " / ".join(picked) if picked else "失敗例として負例に追加し再学習"


def accuracy_stats() -> dict:
    """的中率・失敗率の集計。live/backfill 別も返す。"""
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
        path_rows = conn.execute(
            """SELECT json_extract(p.flags,'$.classify_path') path,
                      o.result_class, COUNT(1) n
               FROM prediction_outcomes o JOIN predictions p ON p.id=o.prediction_id
               WHERE p.status='judged' AND p.category IN ('A','B','C')
                 AND json_extract(p.flags,'$.classify_path') IS NOT NULL
               GROUP BY path, o.result_class""").fetchall()

    by_class = {r["result_class"]: r["n"] for r in rows}
    total = sum(by_class.values())
    success = by_class.get("S", 0) + by_class.get("A", 0) + by_class.get("B", 0)
    near = by_class.get("near", 0)

    # 分類別
    by_cat: dict = {}
    for r in cat_rows:
        by_cat.setdefault(r["category"], {})[r["result_class"]] = r["n"]

    # 失敗タグ集計
    tag_count: dict = {}
    for r in tag_rows:
        for t in loadj(r["failure_tags"], []) or []:
            tag_count[t] = tag_count.get(t, 0) + 1

    # origin 別集計
    by_origin_raw: dict = {}
    for r in origin_rows:
        by_origin_raw.setdefault(r["origin"], {})[r["result_class"]] = r["n"]

    # B/C 条件パス別成功率
    by_path_raw: dict = {}
    for r in path_rows:
        by_path_raw.setdefault(r["path"] or "unknown", {})[r["result_class"]] = r["n"]
    by_path: dict = {}
    for path, d in by_path_raw.items():
        tot = sum(d.values())
        s = d.get("S", 0) + d.get("A", 0) + d.get("B", 0)
        by_path[path] = {
            "total": tot, "by_class": d,
            "hit_rate": round(s / tot, 3) if tot else None,
        }

    def _origin_stats(d: dict) -> dict:
        tot = sum(d.values())
        s = d.get("S", 0) + d.get("A", 0) + d.get("B", 0)
        n = d.get("near", 0)
        return {
            "total": tot,
            "by_class": d,
            "hit_rate": round(s / tot, 4) if tot else None,
            "hit_or_near_rate": round((s + n) / tot, 4) if tot else None,
        }

    by_origin = {k: _origin_stats(v) for k, v in by_origin_raw.items()}

    return {
        "total_judged": total,
        "by_class": by_class,
        "hit_rate": round(success / total, 4) if total else None,
        "hit_or_near_rate": round((success + near) / total, 4) if total else None,
        "by_category": by_cat,
        "failure_tags": dict(sorted(tag_count.items(), key=lambda kv: -kv[1])),
        "by_origin": by_origin,
        "by_classify_path": by_path,
    }
