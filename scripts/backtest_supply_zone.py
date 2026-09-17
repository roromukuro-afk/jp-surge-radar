"""
supply_zone_features (上値供給 / Failure Line) を満期済み予測で検証する。

スコアへ組み込む前に「本当に成功率と関係があるか」を実測する。満期の定義は
run_date が直近から数えて21営業日以前(=20営業日の判定窓を完走している)。
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


def rate_table(rows: list[dict], key: str, buckets: list[tuple[float, float]]) -> None:
    print(f"\n--- {key} ---")
    print(f"{'帯':>14} {'n':>6} {'成功':>5} {'成功率':>8} {'95%CI':>16}")
    for lo, hi in buckets:
        sel = [r for r in rows if lo <= r[key] < hi]
        n = len(sel)
        if n == 0:
            print(f"{lo:7.1f}-{hi:5.1f}      0")
            continue
        s = sum(1 for r in sel if r["success"])
        p = s / n
        se = (p * (1 - p) / n) ** 0.5
        print(f"{lo:7.1f}-{hi:5.1f} {n:6d} {s:5d} {p*100:7.1f}% "
              f"[{max(0,p-1.96*se)*100:4.1f}-{min(1,p+1.96*se)*100:4.1f}%]")


def main() -> None:
    conn = db.connect()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
    cutoff = dates[-21]
    print(f"maturity cutoff (run_date <=): {cutoff}")

    preds = conn.execute("""
        SELECT p.code, p.run_date, p.category, p.score, o.result_class
        FROM predictions p
        JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE p.run_date <= %s AND o.result_class IS NOT NULL
    """, (cutoff,)).fetchall()
    print(f"matured predictions: {len(preds)}")

    by_code: dict[str, list] = defaultdict(list)
    for r in preds:
        by_code[r["code"]].append(r)
    print(f"distinct codes: {len(by_code)}")

    rows: list[dict] = []
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
            rows.append({
                "code": code, "run_date": p["run_date"], "category": p["category"],
                "success": p["result_class"] in SUCCESS,
                **f,
            })
        if i % 200 == 0:
            print(f"  {i}/{len(by_code)} codes, {len(rows)} samples", flush=True)

    n = len(rows)
    base = sum(1 for r in rows if r["success"]) / n
    print(f"\nsamples: {n}  baseline success rate: {base*100:.1f}%")

    rate_table(rows, "overhead_supply_days",
               [(0, 5), (5, 10), (10, 20), (20, 40), (40, 80), (80, 160), (160, 1e9)])
    rate_table(rows, "overhead_ratio",
               [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 1.01)])
    rate_table(rows, "failure_distance",
               [(0, 0.05), (0.05, 0.10), (0.10, 0.15), (0.15, 0.25), (0.25, 1e9)])
    rate_table(rows, "reward_failure_ratio",
               [(0, 1), (1, 2), (2, 3), (3, 5), (5, 1e9)])

    # A/B/C 候補だけに絞った場合
    abc = [r for r in rows if r["category"] in ("A", "B", "C")]
    if abc:
        b = sum(1 for r in abc if r["success"]) / len(abc)
        print(f"\n===== A/B/C候補のみ: n={len(abc)} baseline={b*100:.1f}% =====")
        rate_table(abc, "overhead_supply_days",
                   [(0, 5), (5, 10), (10, 20), (20, 40), (40, 80), (80, 1e9)])


if __name__ == "__main__":
    main()
