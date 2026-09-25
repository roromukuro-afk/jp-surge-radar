"""
「買いが約定しきらない高値引け」を加点した場合に、日ごとの上位K件が良くなるかを測る。

背景 (2026-09-25 実測、2024-06以降・終値3000円以下・翌20営業日で+20%):
  全体 9.2% / +10%かつ高値引けかつ薄商い 67.8% (n=425)
この条件に該当した225件を、予測のある日でエンジンの順位と突き合わせると:
  1-5位 2件 / 6-20位 29件 / 21-50位 40件 / 51-100位 35件 / 101-300位 47件
  / 300位圏外 72件 — 到達率はどの順位帯でも59〜77%で、順位が見分けていない。
volume_score が出来高急増を加点する設計なので、薄商いのこの条件は逆に沈む。

ここでは保存済みの composite に定数を足して並べ替え直し、加点の大きさを
run_date の前半で選んで後半で検証する。全期間で最良の値を選ぶと過学習になる
(2026-09-17 に chart_score の配分で実際に起きた)。
本番DBには書き込まない。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from surge_radar import db  # noqa: E402

SUCCESS = ("S", "A", "B")
BONUSES = (0.0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50)

SQL = """
WITH d AS (
  SELECT code, date, close, high, volume,
    LAG(close) OVER (PARTITION BY code ORDER BY date) pc,
    AVG(volume) OVER (PARTITION BY code ORDER BY date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) v5
  FROM prices
), flag AS (
  SELECT code, date,
    CASE WHEN close >= pc*1.10 AND close >= high*0.999 AND v5 > 0 AND volume < 0.5*v5
         THEN 1 ELSE 0 END f
  FROM d WHERE pc > 0
)
SELECT p.run_date, p.code, p.score, COALESCE(fl.f, 0) flag,
       CASE WHEN o.result_class IN ('S','A','B') THEN 1 ELSE 0 END hit
FROM predictions p
JOIN prediction_outcomes o ON o.prediction_id = p.id
LEFT JOIN flag fl ON fl.code = p.code AND fl.date = p.run_date
WHERE o.result_class IS NOT NULL AND o.bars_tracked >= 20 AND p.status <> 'dup_hold'
"""


def topk(rows, bonus: float, k: int) -> tuple[float, int]:
    by_day = defaultdict(list)
    for r in rows:
        by_day[r["run_date"]].append(r)
    picks = []
    for rs in by_day.values():
        picks.extend(sorted(rs, key=lambda r: -(r["score"] + bonus * r["flag"]))[:k])
    if not picks:
        return 0.0, 0
    return sum(r["hit"] for r in picks) / len(picks), len(picks)


def main() -> None:
    with db.cursor() as conn:
        rows = [dict(r) for r in conn.execute(SQL).fetchall()]
    dates = sorted({r["run_date"] for r in rows})
    n_flag = sum(r["flag"] for r in rows)
    print(f"満期済み {len(rows)}件 / {len(dates)}日  条件該当 {n_flag}件 "
          f"(到達率 {sum(r['hit'] for r in rows if r['flag'])/max(n_flag,1)*100:.1f}%)")
    print(f"全体の到達率: {sum(r['hit'] for r in rows)/len(rows)*100:.1f}%\n")

    half = len(dates) // 2
    train = [r for r in rows if r["run_date"] in set(dates[:half])]
    test = [r for r in rows if r["run_date"] in set(dates[half:])]

    print(f"{'加点':>6} {'学習top5':>10} {'学習top10':>10} {'検証top5':>10} {'検証top10':>10}")
    best = None
    for b in BONUSES:
        tr5, _ = topk(train, b, 5)
        tr10, _ = topk(train, b, 10)
        te5, _ = topk(test, b, 5)
        te10, n = topk(test, b, 10)
        print(f"{b:6.2f} {tr5*100:9.1f}% {tr10*100:9.1f}% {te5*100:9.1f}% {te10*100:9.1f}%")
        if best is None or tr10 > best[1]:
            best = (b, tr10)
    b = best[0]
    te10, n = topk(test, b, 10)
    base10, _ = topk(test, 0.0, 10)
    print(f"\n学習側で最良の加点: {b:.2f}")
    print(f"  検証側 top10: 加点なし {base10*100:.1f}% → 加点あり {te10*100:.1f}% "
          f"({(te10-base10)*100:+.1f}pt, n={n})")


if __name__ == "__main__":
    main()
