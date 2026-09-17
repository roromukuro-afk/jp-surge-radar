"""
chart_score 是正後の配分を測り直す。

optimize_weights.py は predictions に保存済みの chart_score(旧実装=逆指標、
top10で6.9%)を使っていたため、chart の重みの評価が信用できない。ここでは
features から現行の chart_score を再計算する。

材料も汚染除去後の値で再計算し、日中値幅は価格から算出する。
評価は top-K precision、過学習を見るため run_date のホールドアウトを併記。
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from itertools import product
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from surge_radar import db, materials  # noqa: E402
from surge_radar.scoring import chart_score, volume_score, theme_score, fundamental_score  # noqa: E402

SUCCESS = {"S", "A", "B"}
KEYS = ["material", "chart", "volume", "theme", "similarity", "fundamental",
        "prob", "volatility"]


def load() -> list[dict]:
    conn = db.connect()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
    cutoff = dates[-21]
    preds = conn.execute("""
        SELECT p.code, p.run_date, p.probability, p.similarity_score, p.features,
               o.result_class, o.max_drawdown
        FROM predictions p JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE p.run_date <= %s AND o.result_class IS NOT NULL
    """, (cutoff,)).fetchall()
    print(f"満期済み {len(preds)}件 (cutoff {cutoff})", flush=True)

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
            dr = float(((t20["high"] - t20["low"]) / t20["close"]).mean())
            rows.append({
                "run_date": rd, "code": p["code"],
                "success": p["result_class"] in SUCCESS,
                "danger": p["result_class"] == "danger_fail",
                "dd": float(p["max_drawdown"] or 0),
                "material": float(clean.get(p["code"], {}).get("material_raw", 0.0)),
                "chart": chart_score(ft),        # 是正後の実装で再計算
                "volume": volume_score(ft),
                "theme": theme_score(ft),
                "fundamental": fundamental_score(ft),
                "similarity": float(p["similarity_score"] or 0),
                "prob": float(p["probability"] or 0),
                "volatility": min(dr / 0.08, 1.0),   # 8%で1.0に正規化
            })
        print(f"  {rd}", flush=True)
    return rows


def topk(rows, fn, k=10):
    bd = defaultdict(list)
    for r in rows:
        bd[r["run_date"]].append(r)
    pick = []
    for rs in bd.values():
        pick.extend(sorted(rs, key=fn, reverse=True)[:k])
    if not pick:
        return 0.0, 0.0, 0
    succ = sum(1 for r in pick if r["success"]) / len(pick)
    dang = sum(1 for r in pick if r["danger"]) / len(pick)
    return succ, dang, len(pick)


def main() -> None:
    rows = load()
    base = sum(1 for r in rows if r["success"]) / len(rows)
    print(f"\nsamples={len(rows)} baseline={base*100:.1f}%\n")

    print("=== 是正後の各成分 単独 top10 ===")
    for k in KEYS:
        s, d, n = topk(rows, lambda r, k=k: r[k], 10)
        print(f"  {k:>12} 成功{s*100:5.1f}%  danger{d*100:5.1f}%")

    cur_w = {"material": .26, "chart": .22, "volume": .22,
             "theme": .12, "similarity": .12, "fundamental": .06}

    def cur(r):
        w = sum(cur_w[k] * r[k] for k in cur_w)
        top = max(r["material"], r["chart"], r["volume"], r["theme"], r["similarity"])
        return 0.42 * w + 0.28 * r["prob"] + 0.18 * top + 0.12 * 0.5

    s, d, n = topk(rows, cur, 10)
    print(f"\n現行配分(chart是正後): 成功{s*100:.1f}% danger{d*100:.1f}% (n={n})")

    dates = sorted({r["run_date"] for r in rows})
    half = len(dates) // 2
    tr = [r for r in rows if r["run_date"] in set(dates[:half])]
    te = [r for r in rows if r["run_date"] in set(dates[half:])]

    steps = [0.0, 0.1, 0.2, 0.3]
    vols = [0.2, 0.3, 0.4, 0.5]
    cand = []
    for wv, wc, wm, ws in product(vols, steps, steps, steps):
        used = wv + wc + wm + ws
        if used > 0.95:
            continue
        rest = 1.0 - used
        ws_ = {"volatility": wv, "chart": wc, "material": wm, "similarity": ws,
               "prob": rest * 0.5, "volume": rest * 0.2,
               "theme": rest * 0.2, "fundamental": rest * 0.1}
        cand.append(ws_)

    full = []
    for w in cand:
        s, d, _ = topk(rows, lambda r, w=w: sum(w[k] * r[k] for k in w), 10)
        full.append((s, d, w))
    full.sort(key=lambda x: -x[0])
    print("\n=== 全期間 上位6配分 ===")
    for s, d, w in full[:6]:
        t = " ".join(f"{k}={v:.2f}" for k, v in sorted(w.items(), key=lambda x: -x[1]) if v > 0.01)
        print(f"  成功{s*100:5.1f}% danger{d*100:5.1f}%  {t}")

    tro = []
    for w in cand:
        s, _, _ = topk(tr, lambda r, w=w: sum(w[k] * r[k] for k in w), 10)
        tro.append((s, w))
    tro.sort(key=lambda x: -x[0])
    best = tro[0][1]
    s_te, d_te, n_te = topk(te, lambda r: sum(best[k] * r[k] for k in best), 10)
    s_cur, d_cur, _ = topk(te, cur, 10)
    t = " ".join(f"{k}={v:.2f}" for k, v in sorted(best.items(), key=lambda x: -x[1]) if v > 0.01)
    print(f"\n=== ホールドアウト(学習{half}日/検証{len(dates)-half}日) ===")
    print(f"  学習側最良: {t}")
    print(f"  学習側 成功{tro[0][0]*100:.1f}%")
    print(f"  検証側 成功{s_te*100:.1f}% danger{d_te*100:.1f}% (n={n_te})")
    print(f"  検証側 現行配分 成功{s_cur*100:.1f}% danger{d_cur*100:.1f}%")


if __name__ == "__main__":
    main()
