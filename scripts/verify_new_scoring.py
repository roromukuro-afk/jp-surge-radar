"""
実装後の score_candidate() が、検証で得た数値を再現するかを確認する。

compare_composite_forms.py は式を手書きで再現して測っていた。ここでは本番の
score_candidate() をそのまま呼び、ホールドアウトの検証側で top10 成功率が
48.8%前後、danger が18.8%前後になることを確かめる。ずれていれば実装漏れ。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
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
    print(f"sim_thresholds={SIM_TH}")
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
            # 過去予測の features には daily_range_20 が無いので補う
            ft["daily_range_20"] = float(((t20["high"] - t20["low"]) / t20["close"]).mean())
            m = clean.get(p["code"], {})
            ft["material_raw"] = float(m.get("material_raw", 0.0))
            ft["pos_impact"] = float(m.get("pos_impact", 0.0))
            ft["neg_impact"] = float(m.get("neg_impact", 0.0))
            ft["has_fresh_material"] = int(m.get("has_fresh_material", 0))
            ft["n_materials"] = int(m.get("n_materials", 0))
            # sim_thresholds を渡さないと既定の 0.68/0.78 が使われ、実モデルの
            # 自己較正値(2026-09-17時点で 0.9253/0.9418)より大幅に緩くなる。
            # 渡し忘れると strong_ai が多発して B判定が58%まで膨張し、閾値が
            # 壊れているように見える(実際に一度そう誤判定した)。
            res = scoring.score_candidate(
                ft, ml_prob=float(p["probability"] or 0),
                similarity=float(p["similarity_score"] or 0),
                sim_thresholds=SIM_TH)
            rows.append({
                "run_date": rd,
                "success": p["result_class"] in SUCCESS,
                "danger": p["result_class"] == "danger_fail",
                "score": res["score"],
                "category": res["category"],
            })
        print(f"  {rd}", flush=True)

    dts = sorted({r["run_date"] for r in rows})
    half = len(dts) // 2
    te = [r for r in rows if r["run_date"] in set(dts[half:])]
    base = sum(1 for r in rows if r["success"]) / len(rows)
    print(f"\nsamples={len(rows)} baseline={base*100:.1f}%")

    for label, data in (("全期間", rows), ("検証側(後半8日)", te)):
        bd = defaultdict(list)
        for r in data:
            bd[r["run_date"]].append(r)
        for k in (10, 20):
            pick = []
            for rs in bd.values():
                pick.extend(sorted(rs, key=lambda r: r["score"], reverse=True)[:k])
            s = sum(1 for r in pick if r["success"]) / len(pick)
            d = sum(1 for r in pick if r["danger"]) / len(pick)
            print(f"  {label} top{k}: 成功{s*100:.1f}% danger{d*100:.1f}% (n={len(pick)})")

    from collections import Counter
    print("\nカテゴリ分布:", dict(Counter(r["category"] for r in rows)))
    for cat in ("A", "B", "C", "D", "E"):
        sel = [r for r in rows if r["category"] == cat]
        if len(sel) >= 20:
            s = sum(1 for r in sel if r["success"]) / len(sel)
            print(f"  {cat}: n={len(sel):4d} 成功{s*100:5.1f}%")


if __name__ == "__main__":
    main()
