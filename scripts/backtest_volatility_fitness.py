"""
「20%値幅適性」(ボラティリティ)が realistic_upside に欠けている件の検証。

composite = 0.42*weighted + 0.28*prob + 0.18*top + 0.12*upside のうち、
weighted(材料/チャート/出来高/テーマ/類似度/ファンダ)と top はボラティリティを
一切見ていない。prob(ML)は range_ratio を特徴量として持つが、composite 全体の
28%にとどまる。realistic_upside にも値幅の項がない。

ここでは range_ratio(直近20本の日中値幅平均/終値)が、既存の判定カテゴリや
52週高値距離で条件付けしても効果を保つかを確認し、閾値を決める材料にする。
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

SUCCESS = {"S", "A", "B"}
RR_BUCKETS = [(0, 0.02), (0.02, 0.03), (0.03, 0.04), (0.04, 0.06), (0.06, 1e9)]
RR_LABEL = ["<2%", "2-3%", "3-4%", "4-6%", "6%超"]


def line(rows, label):
    out = f"{label:>16}"
    for lo, hi in RR_BUCKETS:
        sel = [r for r in rows if lo <= r["range_ratio"] < hi]
        if len(sel) < 20:
            out += f"{'n<20':>17}"
        else:
            p = sum(1 for r in sel if r["success"]) / len(sel)
            out += f"{p*100:10.1f}% (n={len(sel)})".rjust(17)
    print(out)


def main() -> None:
    conn = db.connect()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
    cutoff = dates[-21]
    preds = conn.execute("""
        SELECT p.code, p.run_date, p.category, o.result_class
        FROM predictions p JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE p.run_date <= %s AND o.result_class IS NOT NULL
    """, (cutoff,)).fetchall()

    by_code = defaultdict(list)
    for r in preds:
        by_code[r["code"]].append(r)

    rows = []
    for i, (code, plist) in enumerate(by_code.items(), 1):
        prows = conn.execute(
            "SELECT date,high,low,close FROM prices WHERE code=%s ORDER BY date",
            (code,)).fetchall()
        if not prows:
            continue
        df = pd.DataFrame([dict(x) for x in prows])
        for p in plist:
            hist = df[df["date"] <= p["run_date"]]
            if len(hist) < 30:
                continue
            t20 = hist.tail(20)
            rr = float(((t20["high"] - t20["low"]) / t20["close"]).mean())
            w = hist.tail(250)
            hi52 = float(w["high"].max())
            close = float(hist["close"].iloc[-1])
            rows.append({
                "success": p["result_class"] in SUCCESS,
                "range_ratio": rr, "category": p["category"],
                "p52": (close - hi52) / hi52 if hi52 else 0.0,
            })
        if i % 300 == 0:
            print(f"  {i}/{len(by_code)}", flush=True)

    n = len(rows)
    print(f"\nsamples={n} baseline={sum(1 for r in rows if r['success'])/n*100:.1f}%\n")
    print(f"{'':>16}" + "".join(f"{c:>17}" for c in RR_LABEL))
    line(rows, "全体")
    print()
    for cat in ("A", "B", "C", "D", "E"):
        sel = [r for r in rows if r["category"] == cat]
        if len(sel) >= 40:
            line(sel, f"判定{cat} (n={len(sel)})")
    print()
    for (lo, hi), lab in [((-1e9, -0.30), "52wH -30%超"), ((-0.30, -0.15), "-30〜-15%"),
                          ((-0.15, -0.05), "-15〜-5%"), ((-0.05, 1e9), "高値圏")]:
        line([r for r in rows if lo <= r["p52"] < hi], lab)

    print("\n--- 現行 realistic_upside の入力には値幅項が無い ---")
    lowvol = [r for r in rows if r["range_ratio"] < 0.025]
    abc_lowvol = [r for r in lowvol if r["category"] in ("A", "B", "C")]
    print(f"日中値幅<2.5% の予測: {len(lowvol)}件 "
          f"成功率{sum(1 for r in lowvol if r['success'])/max(len(lowvol),1)*100:.1f}%")
    if abc_lowvol:
        print(f"  うち A/B/C 判定で表に出たもの: {len(abc_lowvol)}件 "
              f"成功率{sum(1 for r in abc_lowvol if r['success'])/len(abc_lowvol)*100:.1f}%")


if __name__ == "__main__":
    main()
