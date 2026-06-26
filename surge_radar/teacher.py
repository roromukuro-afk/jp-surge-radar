"""
教師データ生成。

初期教師データ = 過去株価から、短期で+20%上がった銘柄のT0状態(正例)を大量に収集し、
急騰前パターンを学習。比較用に同時期/同価格帯で上がらなかった例(負例)も収集。

本命の失敗教師データは運用後にAI自身が外した予測(live_fail)。これは track.py が追加する。
"""
from __future__ import annotations

import json
import random
from datetime import datetime

from . import db, features, ingest, labeling
from .config import HISTORY_MIN_BARS, JUDGE_WINDOW, PRICE_CAP


def build_historical(codes: list[str], *, step: int = 10, max_per_code: int = 8,
                     neg_ratio: float = 2.0, seed: int = 42,
                     on_progress=None) -> dict:
    """
    各銘柄の履歴を step 日刻みでサンプリングし、T0特徴+20日先成否でラベル付け。
    正例: +20%到達 / 負例: 未到達。負例は正例の neg_ratio 倍まで間引いて保存。
    tags に S/A/B/near/fail/danger_fail 結果クラス・最大上昇率・到達日数を記録。
    """
    rng = random.Random(seed)
    pos_rows, neg_rows = [], []
    n_codes = len(codes)

    for ci, code in enumerate(codes, 1):
        df = ingest.load_history(code)
        if df is None or df.empty or len(df) < HISTORY_MIN_BARS + JUDGE_WINDOW:
            continue
        # T0候補: 30 .. len-21 全インデックスを生成してシャッフル
        idxs = list(range(30, len(df) - JUDGE_WINDOW - 1, step))
        rng.shuffle(idxs)
        kept = 0
        for idx in idxs:
            if kept >= max_per_code:
                break
            close = float(df.iloc[idx]["close"])
            if close <= 0 or close > PRICE_CAP:
                continue
            feats = features.build_features(df, idx)
            if feats is None:
                continue
            oc = labeling.forward_outcome(df, idx)
            if oc is None or oc.get("partial"):
                continue
            label = labeling.is_success(oc)
            result_class, fail_tags = labeling.classify_result(oc)
            # S/A/B を結果クラスへ
            tags = {
                "result_class": result_class,
                "max_up_5d": oc.get("max_up_5d", 0),
                "max_up_10d": oc.get("max_up_10d", 0),
                "max_up_20d": oc["max_up_20d"],
                "days_to_20pct": oc["days_to_20pct"],
                "max_drawdown": oc.get("max_drawdown", 0),
                "faded_after_high": oc.get("faded_after_high", 0),
                "fail_tags": fail_tags,
            }
            row = {
                "source": "historical_pos" if label else "historical_neg",
                "code": code,
                "t0_date": feats["_date"],
                "label": label,
                "features": _clean_feats(feats),
                "tags": tags,
            }
            (pos_rows if label else neg_rows).append(row)
            kept += 1
        if on_progress and ci % 100 == 0:
            on_progress(ci, n_codes, len(pos_rows), len(neg_rows))

    # 負例を間引き(正例の neg_ratio 倍まで)
    rng.shuffle(neg_rows)
    max_neg = int(len(pos_rows) * neg_ratio) if pos_rows else 500
    neg_rows = neg_rows[:max_neg]

    _store(pos_rows + neg_rows)
    return {"pos": len(pos_rows), "neg": len(neg_rows), "codes_scanned": n_codes}


def _clean_feats(feats: dict) -> dict:
    return {k: v for k, v in feats.items() if not k.startswith("_")}


def clear_historical() -> int:
    """historical_pos/neg のみ削除 (live_fail/live_success は残す)。"""
    with db.cursor() as conn:
        n = conn.execute(
            "SELECT COUNT(*) n FROM teacher_samples WHERE source IN ('historical_pos','historical_neg')"
        ).fetchone()["n"]
        conn.execute("DELETE FROM teacher_samples WHERE source IN ('historical_pos','historical_neg')")
    return n


def _store(rows: list[dict]) -> None:
    if not rows:
        return
    with db.cursor() as conn:
        # INSERT OR IGNORE → idx_teacher_code_date (UNIQUE) が重複を自動スキップ
        conn.executemany(
            "INSERT OR IGNORE INTO teacher_samples(source,code,t0_date,label,features,tags) VALUES(%s,%s,%s,%s,%s,%s)",
            [(r["source"], r["code"], r["t0_date"], r["label"],
              db.j(r["features"]), db.j(r.get("tags", {}))) for r in rows],
        )


def counts() -> dict:
    """教師データの詳細統計を返す。"""
    with db.cursor() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) n, SUM(label) pos FROM teacher_samples GROUP BY source"
        ).fetchall()
        tot = conn.execute("SELECT COUNT(*) n FROM teacher_samples").fetchone()["n"]
        pos_tot = conn.execute("SELECT COUNT(*) n FROM teacher_samples WHERE label=1").fetchone()["n"]

        # result_class 別集計
        rc_rows = conn.execute("""
            SELECT json_extract(tags,'$.result_class') rc, COUNT(*) n
            FROM teacher_samples
            WHERE source IN ('historical_pos','historical_neg')
              AND json_extract(tags,'$.result_class') IS NOT NULL
            GROUP BY rc
        """).fetchall()

        # 年別集計
        yr_rows = conn.execute("""
            SELECT substr(t0_date,1,4) yr, COUNT(*) n, SUM(label) pos
            FROM teacher_samples
            WHERE source IN ('historical_pos','historical_neg')
            GROUP BY yr ORDER BY yr
        """).fetchall()

        # 失敗タグ集計(live_fail)
        lf = conn.execute("""
            SELECT tags FROM teacher_samples WHERE source='live_fail'
        """).fetchall()

    source_stats = {r["source"]: {"n": r["n"], "pos": r["pos"] or 0} for r in rows}
    rc_stats = {(r["rc"] or "?"): r["n"] for r in rc_rows}
    yr_stats = [{"year": r["yr"], "n": r["n"], "pos": r["pos"] or 0} for r in yr_rows]

    # fail_tags 集計
    fail_tag_counts: dict[str, int] = {}
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
        "by_result_class": rc_stats,
        "by_year": yr_stats,
        "fail_tag_counts": fail_tag_counts,
    }
