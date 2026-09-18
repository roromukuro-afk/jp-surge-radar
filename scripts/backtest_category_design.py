"""
カテゴリ設計をどう直すかを決めるための測定。

新スコアリングでは順位(top10 42.5%)は当たるがラベル(B16.3%/D13.9%/E15.3%)は
当たらない。二つの案があり、どちらが成立するかはデータ次第:

  案1 スコアの分位でカテゴリを定義する
  案2 classify_path 別の実績を測り、当たっている経路だけ残す

案2が成立するには経路ごとに十分な件数と有意な差が要る。ここでは
  (a) run_date 内のスコア順位帯ごとの成功率
  (b) classify_path 別の成功率
を並べて、どちらの軸が実際に分離するかを見る。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from surge_radar import db, materials, model, scoring  # noqa: E402

SUCCESS = {"S", "A", "B"}
SIM_TH = model.Predictor().sim_thresholds


def main() -> None:
    conn = db.connect()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
    cutoff = dates[-21]
    preds = conn.execute("""
        SELECT p.code, p.run_date, p.probability, p.similarity_score, p.features,
               o.result_class
        FROM predictions p JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE p.run_date <= %s AND o.result_class IS NOT NULL
    """, (cutoff,)).fetchall()

    codes = sorted({p["code"] for p in preds})
    px = {}
    for i in range(0, len(codes), 400):
        part = codes[i:i + 400]
        ph = ",".join(["%s"] * len(part))
        for r in conn.execute(
                f"SELECT code,date,high,low,close FROM prices WHERE code IN ({ph}) ORDER BY code,date",
                tuple(part)).fetchall():
            px.setdefault(r["code"], []).append(r)
    px = {c: pd.DataFrame(v) for c, v in px.items()}

    by_date = defaultdict(list)
    for p in preds:
        by_date[p["run_date"]].append(p)

    rows = []
    for rd, plist in sorted(by_date.items()):
        clean = materials.recent_material_scores_bulk([p["code"] for p in plist], rd)
        for p in plist:
            ft = json.loads(p["features"] or "{}")
            df = px.get(p["code"])
            if not ft or df is None:
                continue
            hist = df[df["date"] <= rd]
            if len(hist) < 20:
                continue
            t20 = hist.tail(20)
            ft["daily_range_20"] = float(((t20["high"] - t20["low"]) / t20["close"]).mean())
            m = clean.get(p["code"], {})
            ft["material_raw"] = float(m.get("material_raw", 0.0))
            ft["pos_impact"] = float(m.get("pos_impact", 0.0))
            ft["n_materials"] = int(m.get("n_materials", 0))
            res = scoring.score_candidate(
                ft, ml_prob=float(p["probability"] or 0),
                similarity=float(p["similarity_score"] or 0), sim_thresholds=SIM_TH)
            rows.append({
                "run_date": rd,
                "success": p["result_class"] in SUCCESS,
                "danger": p["result_class"] == "danger_fail",
                "score": res["score"],
                "category": res["category"],
                "path": res.get("classify_path", ""),
            })
        print(f"  {rd}", flush=True)

    n = len(rows)
    base = sum(1 for r in rows if r["success"]) / n
    print(f"\nsamples={n} baseline={base*100:.1f}%")

    # (a) run_date 内のスコア順位帯
    bd = defaultdict(list)
    for r in rows:
        bd[r["run_date"]].append(r)
    ranked = []
    for rd, rs in bd.items():
        rs = sorted(rs, key=lambda r: r["score"], reverse=True)
        for i, r in enumerate(rs):
            r["rank"] = i + 1
            r["pct"] = i / len(rs)
            ranked.append(r)

    print("\n=== (a) スコア順位帯ごと (各run_date内) ===")
    print(f"{'順位':>12} {'n':>6} {'成功':>8} {'danger':>9}")
    for lo, hi, lab in ((0, 5, "1-5位"), (5, 10, "6-10位"), (10, 20, "11-20位"),
                        (20, 30, "21-30位"), (30, 50, "31-50位"),
                        (50, 100, "51-100位"), (100, 10000, "101位以下")):
        sel = [r for r in ranked if lo < r["rank"] <= hi]
        if len(sel) < 20:
            continue
        s = sum(1 for r in sel if r["success"]) / len(sel)
        d = sum(1 for r in sel if r["danger"]) / len(sel)
        print(f"{lab:>12} {len(sel):6d} {s*100:7.1f}% {d*100:8.1f}%")

    # (b) classify_path 別
    print("\n=== (b) classify_path 別 ===")
    print(f"{'path':>26} {'n':>6} {'成功':>8} {'danger':>9}")
    cnt = Counter(r["path"] for r in rows)
    for path, c in cnt.most_common():
        sel = [r for r in rows if r["path"] == path]
        s = sum(1 for r in sel if r["success"]) / len(sel)
        d = sum(1 for r in sel if r["danger"]) / len(sel)
        flag = "" if c >= 30 else "  (件数不足)"
        print(f"{path:>26} {c:6d} {s*100:7.1f}% {d*100:8.1f}%{flag}")


if __name__ == "__main__":
    main()
