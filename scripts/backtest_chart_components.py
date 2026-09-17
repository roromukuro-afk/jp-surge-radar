"""
chart_score が baseline を下回る(top10 precision 6.9% vs 14.8%)原因を構成要素別に測る。

chart_score は「下落止まり→ボラ縮小→横ばい→安値切り上げ→ブレイク」という
底固めの型を高く評価する設計だが、2026-09-17の実測では日中値幅(ボラ)が
最強の正の予測因子だった。つまり volatility_contraction(重み0.14)と
sideways(0.12)は、測定された方向と逆を向いている可能性がある。

各構成要素で単独に順位付けし、上位10件の成功率を見る。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from surge_radar import db  # noqa: E402

SUCCESS = {"S", "A", "B"}

# chart_score の構成要素と、現行の重み(符号つき)
PARTS = [
    ("downtrend_stopped", +0.18),
    ("volatility_contraction", +0.14),
    ("sideways", +0.12),
    ("higher_lows", +0.16),
    ("lower_highs_stopped", +0.10),
    ("near_breakout", +0.16),
    ("broke_resistance", +0.08),
    ("price_above_ma25", +0.06),
    ("downtrend_risk", -0.30),
    ("rebound_capped", -0.15),
    ("high_zone_upper_wick", -0.15),
]


def main() -> None:
    conn = db.connect()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
    cutoff = dates[-21]
    preds = conn.execute("""
        SELECT p.run_date, p.chart_score, p.features, o.result_class
        FROM predictions p JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE p.run_date <= %s AND o.result_class IS NOT NULL
    """, (cutoff,)).fetchall()

    rows = []
    for p in preds:
        ft = json.loads(p["features"] or "{}")
        if not ft:
            continue
        rows.append({"run_date": p["run_date"],
                     "success": p["result_class"] in SUCCESS,
                     "chart": float(p["chart_score"] or 0),
                     **{k: float(ft.get(k, 0)) for k, _ in PARTS}})
    n = len(rows)
    base = sum(1 for r in rows if r["success"]) / n
    print(f"samples={n} baseline={base*100:.1f}%\n")

    by_date = defaultdict(list)
    for r in rows:
        by_date[r["run_date"]].append(r)

    def topk(key: str, k: int = 10, reverse: bool = True) -> tuple[float, int]:
        picked = []
        for rs in by_date.values():
            picked.extend(sorted(rs, key=lambda r: r[key], reverse=reverse)[:k])
        if not picked:
            return 0.0, 0
        return sum(1 for r in picked if r["success"]) / len(picked), len(picked)

    print("=== chart_score の構成要素を単独で順位付けしたときの top10 成功率 ===")
    print(f"{'構成要素':>24} {'現行重み':>9} {'高い順':>9} {'低い順':>9} {'判定':>16}")
    for key, w in PARTS:
        hi, _ = topk(key, 10, True)
        lo, _ = topk(key, 10, False)
        # 現行の重みが正なら「高いほど良い」を期待している
        expect_hi = w > 0
        actual_hi = hi > lo
        verdict = "OK" if expect_hi == actual_hi else "★符号が逆"
        print(f"{key:>24} {w:+9.2f} {hi*100:8.1f}% {lo*100:8.1f}% {verdict:>16}")

    print()
    hi, m = topk("chart", 10, True)
    lo, _ = topk("chart", 10, False)
    print(f"chart_score 合成: 高い順 {hi*100:.1f}% / 低い順 {lo*100:.1f}% (n={m})")
    print("→ 低い順のほうが高ければ、合成スコアは逆向きに効いている")


if __name__ == "__main__":
    main()
