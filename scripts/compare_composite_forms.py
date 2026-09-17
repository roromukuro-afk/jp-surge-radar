"""
composite の式そのものをホールドアウトで比較する。

グリッドが検証したのは
    sum(w[k] * sub[k])   (prob も同じ線形結合の一項)
という平坦な形だが、本番は
    composite = 0.42*weighted + 0.28*prob + 0.18*top + 0.12*upside
という入れ子構造で、prob の実効重みが大きく違う(B案では0.10相当、本番は0.28相当)。
新しい WEIGHTS をそのまま入れても検証した通りにはならないので、式の形を含めて測る。

top(火種=サブスコアの最大値)と upside(現実到達余地)はグリッドに入れていない
ので、それぞれ有無で比較する。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from optimize_weights_v2 import load, topk  # noqa: E402

NEW_W = {"chart": .33, "similarity": .33, "volatility": .22,
         "volume": .05, "theme": .05, "fundamental": .02, "material": .00}
OLD_W = {"material": .26, "chart": .22, "volume": .22,
         "theme": .12, "similarity": .12, "fundamental": .06}


def weighted(r, w):
    return sum(w[k] * r[k] for k in w)


def top_of(r):
    # volatility は「材料」ではなく「動く能力」なので火種には含めない
    return max(r["material"], r["chart"], r["volume"], r["theme"], r["similarity"])


FORMS = {
    "旧式+旧重み(現行)":
        lambda r: 0.42 * weighted(r, OLD_W) + 0.28 * r["prob"] + 0.18 * top_of(r) + 0.12 * 0.5,
    "旧式+新重み":
        lambda r: 0.42 * weighted(r, NEW_W) + 0.28 * r["prob"] + 0.18 * top_of(r) + 0.12 * 0.5,
    "prob下げ(0.62/0.10/0.18/0.10)":
        lambda r: 0.62 * weighted(r, NEW_W) + 0.10 * r["prob"] + 0.18 * top_of(r) + 0.10 * 0.5,
    "top削除(0.80/0.10/-/0.10)":
        lambda r: 0.80 * weighted(r, NEW_W) + 0.10 * r["prob"] + 0.10 * 0.5,
    "平坦B案(グリッド検証形)":
        lambda r: (.30 * r["chart"] + .30 * r["similarity"] + .20 * r["volatility"]
                   + .10 * r["prob"] + .04 * r["volume"] + .04 * r["theme"]
                   + .02 * r["fundamental"]),
}


def main() -> None:
    rows = load()
    dates = sorted({r["run_date"] for r in rows})
    half = len(dates) // 2
    tr = [r for r in rows if r["run_date"] in set(dates[:half])]
    te = [r for r in rows if r["run_date"] in set(dates[half:])]
    base = sum(1 for r in rows if r["success"]) / len(rows)
    print(f"\nbaseline={base*100:.1f}%  学習{half}日 / 検証{len(dates)-half}日\n")
    print(f"{'式':>30} {'学習top10':>10} {'検証top10':>10} {'検証danger':>11} {'検証top20':>10}")
    for lab, fn in FORMS.items():
        s_tr, _, _ = topk(tr, fn, 10)
        s_te, d_te, _ = topk(te, fn, 10)
        s20, _, _ = topk(te, fn, 20)
        print(f"{lab:>30} {s_tr*100:9.1f}% {s_te*100:9.1f}% {d_te*100:10.1f}% {s20*100:9.1f}%")


if __name__ == "__main__":
    main()
