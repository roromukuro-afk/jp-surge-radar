"""
overhead_ratio が既存の pct_from_52w_high と別の情報を持っているかを検証する。

1次元の集計では「上値供給が重いほど成功率が高い」と出たが、これは
「高値から離れている＝出遅れ銘柄ほど上がる」という既知の知見
(realistic_upside の pct_from_52w_high ペナルティ) と同じものを別角度から
測っているだけの可能性がある。2次元クロス集計で条件付きの効果を見る。
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
P52_BUCKETS = [(-1e9, -0.30), (-0.30, -0.15), (-0.15, -0.05), (-0.05, 1e9)]
P52_LABEL = ["52wH -30%超", "-30〜-15%", "-15〜-5%", "高値圏(-5%〜)"]
OH_BUCKETS = [(0, 0.15), (0.15, 0.35), (0.35, 1.01)]
OH_LABEL = ["上値薄", "中", "上値厚"]


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
            rows.append({
                "success": p["result_class"] in SUCCESS,
                "overhead_ratio": f["overhead_ratio"],
                "p52": (close - hi52) / hi52 if hi52 else 0.0,
            })
        if i % 300 == 0:
            print(f"  {i}/{len(by_code)}", flush=True)

    print(f"\nsamples={len(rows)}  baseline="
          f"{sum(1 for r in rows if r['success'])/len(rows)*100:.1f}%\n")

    header = f"{'':>16}" + "".join(f"{lab:>18}" for lab in OH_LABEL)
    print(header)
    for (plo, phi), plab in zip(P52_BUCKETS, P52_LABEL):
        line = f"{plab:>16}"
        for olo, ohi in OH_BUCKETS:
            sel = [r for r in rows if plo <= r["p52"] < phi and olo <= r["overhead_ratio"] < ohi]
            if len(sel) < 20:
                line += f"{'n<20':>18}"
            else:
                p = sum(1 for r in sel if r["success"]) / len(sel)
                line += f"{p*100:11.1f}% (n={len(sel)})".rjust(18)
        print(line)

    print("\n各52週高値バンド内で overhead_ratio が単調に効いていれば独立した情報。")
    print("効いていなければ pct_from_52w_high の言い換えにすぎない。")


if __name__ == "__main__":
    main()
