"""
重み配分の候補をホールドアウトで比較する。

optimize_weights_v2.py の全期間グリッドで上位に来た配分は、そのままでは
16 run_date への過学習を含む(前回の測定で学習77.5%→検証48.8%と20pt落ちた)。
候補を絞って学習側/検証側の両方を並べ、検証側で通用するものを選ぶ。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from optimize_weights_v2 import load, topk  # noqa: E402

CANDS = {
    "A案(全期間1位)": {"similarity": .30, "volatility": .20, "chart": .20,
                     "material": .20, "prob": .05, "volume": .02,
                     "theme": .02, "fundamental": 0.0},
    "B案(全期間2位)": {"chart": .30, "similarity": .30, "volatility": .20,
                     "prob": .10, "volume": .04, "theme": .04,
                     "fundamental": .02, "material": 0.0},
    "C案(chart厚め)": {"chart": .35, "volatility": .20, "similarity": .20,
                      "prob": .10, "theme": .05, "fundamental": .05,
                      "volume": .03, "material": .02},
    "D案(vol厚め)": {"volatility": .35, "chart": .25, "similarity": .20,
                    "prob": .10, "theme": .04, "fundamental": .03,
                    "volume": .02, "material": .01},
}

CUR_W = {"material": .26, "chart": .22, "volume": .22,
         "theme": .12, "similarity": .12, "fundamental": .06}


def cur(r):
    w = sum(CUR_W[k] * r[k] for k in CUR_W)
    top = max(r["material"], r["chart"], r["volume"], r["theme"], r["similarity"])
    return 0.42 * w + 0.28 * r["prob"] + 0.18 * top + 0.12 * 0.5


def main() -> None:
    rows = load()
    dates = sorted({r["run_date"] for r in rows})
    half = len(dates) // 2
    tr = [r for r in rows if r["run_date"] in set(dates[:half])]
    te = [r for r in rows if r["run_date"] in set(dates[half:])]
    base = sum(1 for r in rows if r["success"]) / len(rows)
    print(f"\nbaseline={base*100:.1f}%  学習{half}日 / 検証{len(dates)-half}日\n")

    hdr = f"{'配分':>18} {'学習top10':>10} {'検証top10':>10} {'検証danger':>11} {'検証top20':>10} {'過学習幅':>9}"
    print(hdr)
    for lab, w in CANDS.items():
        def fn(r, w=w):
            return sum(w[k] * r[k] for k in w)
        s_tr, _, _ = topk(tr, fn, 10)
        s_te, d_te, _ = topk(te, fn, 10)
        s20, _, _ = topk(te, fn, 20)
        print(f"{lab:>18} {s_tr*100:9.1f}% {s_te*100:9.1f}% {d_te*100:10.1f}% "
              f"{s20*100:9.1f}% {(s_tr-s_te)*100:8.1f}pt")
    s_tr, _, _ = topk(tr, cur, 10)
    s_te, d_te, _ = topk(te, cur, 10)
    s20, _, _ = topk(te, cur, 20)
    print(f"{'現行(chart是正後)':>18} {s_tr*100:9.1f}% {s_te*100:9.1f}% {d_te*100:10.1f}% "
          f"{s20*100:9.1f}% {(s_tr-s_te)*100:8.1f}pt")


if __name__ == "__main__":
    main()
