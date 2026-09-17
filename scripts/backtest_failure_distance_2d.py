"""
failure_distance(直近安値までの距離)が既存特徴量と独立した情報を持つかを検証。

1次元では単調(0-10%:10.6% → 20%超:25.7%)だったが、これは「+20%動くには
そもそもボラティリティが要る」という機械的な関係の裏返しである可能性が高い。
52週高値からの距離と、既存のボラティリティ特徴量(range_ratio)で条件付けして
効果が残るかを見る。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from surge_radar import db  # noqa: E402
from surge_radar.indicators import supply_zone_features  # noqa: E402

SUCCESS = {"S", "A", "B"}


def xtab(rows, row_key, row_buckets, row_labels, col_key, col_buckets, col_labels, title):
    print(f"\n===== {title} =====")
    print(f"{'':>16}" + "".join(f"{c:>18}" for c in col_labels))
    for (rlo, rhi), rlab in zip(row_buckets, row_labels):
        line = f"{rlab:>16}"
        for clo, chi in col_buckets:
            sel = [r for r in rows if rlo <= r[row_key] < rhi and clo <= r[col_key] < chi]
            if len(sel) < 20:
                line += f"{'n<20':>18}"
            else:
                p = sum(1 for r in sel if r["success"]) / len(sel)
                line += f"{p*100:11.1f}% (n={len(sel)})".rjust(18)
        print(line)


def main() -> None:
    conn = db.connect()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
    cutoff = dates[-21]
    preds = conn.execute("""
        SELECT p.code, p.run_date, o.result_class
        FROM predictions p JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE p.run_date <= %s AND o.result_class IS NOT NULL
    """, (cutoff,)).fetchall()

    by_code = defaultdict(list)
    for r in preds:
        by_code[r["code"]].append(r)

    rows = []
    for i, (code, plist) in enumerate(by_code.items(), 1):
        prows = conn.execute(
            "SELECT date,open,high,low,close,volume FROM prices WHERE code=%s ORDER BY date",
            (code,)).fetchall()
        if not prows:
            continue
        df = pd.DataFrame([dict(x) for x in prows])
        for p in plist:
            hist = df[df["date"] <= p["run_date"]]
            if len(hist) < 30:
                continue
            f = supply_zone_features(hist)
            w = hist.tail(250)
            hi52 = float(w["high"].max())
            close = float(hist["close"].iloc[-1])
            t20 = hist.tail(20)
            rng = float(((t20["high"] - t20["low"]) / t20["close"]).mean())
            rows.append({
                "success": p["result_class"] in SUCCESS,
                "failure_distance": f["failure_distance"],
                "p52": (close - hi52) / hi52 if hi52 else 0.0,
                "range_ratio": rng,
            })
        if i % 300 == 0:
            print(f"  {i}/{len(by_code)}", flush=True)

    print(f"\nsamples={len(rows)} baseline="
          f"{sum(1 for r in rows if r['success'])/len(rows)*100:.1f}%")

    fd_b = [(0, 0.07), (0.07, 0.12), (0.12, 0.20), (0.20, 1e9)]
    fd_l = ["FD<7%", "7-12%", "12-20%", "20%超"]
    xtab(rows, "p52", [(-1e9, -0.30), (-0.30, -0.15), (-0.15, -0.05), (-0.05, 1e9)],
         ["52wH -30%超", "-30〜-15%", "-15〜-5%", "高値圏"],
         "failure_distance", fd_b, fd_l,
         "52週高値距離 × failure_distance")
    xtab(rows, "range_ratio", [(0, 0.025), (0.025, 0.04), (0.04, 0.06), (0.06, 1e9)],
         ["日中値幅<2.5%", "2.5-4%", "4-6%", "6%超"],
         "failure_distance", fd_b, fd_l,
         "日中値幅(既存ボラ指標) × failure_distance")


if __name__ == "__main__":
    main()
