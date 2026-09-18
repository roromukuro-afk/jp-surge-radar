"""
assign_categories() を通したカテゴリが、測定どおりの成功率になるかを確認する。

順位帯の実測は 1-5位70.0% / 6-20位31.2-40.0% / 21-50位26.9-28.1% /
51-100位14.6% / 101位以下10.1% だった。実装後の A/B/C/D/E がこれに一致するか、
またゲート該当が E に落ちているかを見る。
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

    all_rows = []
    for rd, plist in sorted(by_date.items()):
        clean = materials.recent_material_scores_bulk([p["code"] for p in plist], rd)
        day = []
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
            res["_success"] = p["result_class"] in SUCCESS
            res["_danger"] = p["result_class"] == "danger_fail"
            day.append(res)
        scoring.assign_categories(day)
        all_rows.extend(day)
        print(f"  {rd}", flush=True)

    n = len(all_rows)
    base = sum(1 for r in all_rows if r["_success"]) / n
    print(f"\nsamples={n} baseline={base*100:.1f}%  (16 run_date)")
    print(f"\n{'カテゴリ':>8} {'n':>6} {'1日あたり':>10} {'成功':>8} {'danger':>9}")
    for cat in ("A", "B", "C", "D", "E"):
        sel = [r for r in all_rows if r["category"] == cat]
        if not sel:
            continue
        s = sum(1 for r in sel if r["_success"]) / len(sel)
        d = sum(1 for r in sel if r["_danger"]) / len(sel)
        print(f"{cat:>8} {len(sel):6d} {len(sel)/16:9.1f}件 {s*100:7.1f}% {d*100:8.1f}%")

    gated = [r for r in all_rows if r.get("gates")]
    print(f"\nゲート該当 {len(gated)}件 "
          f"(すべてE: {all(r['category'] == 'E' for r in gated)})")
    print("rule_category の分布:",
          dict(Counter(r.get("rule_category", "") for r in all_rows)))


if __name__ == "__main__":
    main()
