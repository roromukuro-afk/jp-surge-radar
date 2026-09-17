"""
composite のスコア配分を満期実績で検証する。

現行:
  WEIGHTS = {material .26, chart .22, volume .22, theme .12, similarity .12, fundamental .06}
  composite = 0.42*weighted + 0.28*prob + 0.18*top + 0.12*upside

2026-09-17の実測で判明していること:
- material は予測力ゼロ〜わずかに逆相関(材料あり13.8% vs なし15.1%)なのに最大の重み0.26
- 日中値幅は単独で 1.9%→45.5% の24倍差だが、upside(全体の12%)の一項目でしかない

評価指標は top-K precision。アプリは順位付きリストを出すので、
「各run_dateで上位K件に入れた銘柄の成功率」が実運用に一番近い。

注意: 満期済みは16 run_date しかない。グリッドサーチは過学習しやすいので、
run_date を前半/後半に分けた簡易ホールドアウトも併記する。
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

SUCCESS = {"S", "A", "B"}
COMPONENTS = ["material", "chart", "volume", "theme", "similarity", "fundamental",
              "prob", "upside", "daily_range"]


def load_samples() -> list[dict]:
    conn = db.connect()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
    cutoff = dates[-21]
    preds = conn.execute("""
        SELECT p.code, p.run_date, p.probability, p.chart_score, p.volume_score,
               p.theme_score, p.fundamental_score, p.similarity_score, p.flags,
               o.result_class
        FROM predictions p JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE p.run_date <= %s AND o.result_class IS NOT NULL
    """, (cutoff,)).fetchall()
    print(f"満期済み予測 {len(preds)}件 (cutoff {cutoff})", flush=True)

    by_date = defaultdict(list)
    for p in preds:
        by_date[p["run_date"]].append(p)

    # 日中値幅は銘柄ごとに価格から再計算(過去予測には保存されていない)
    codes = sorted({p["code"] for p in preds})
    price = {}
    for i in range(0, len(codes), 400):
        part = codes[i:i + 400]
        ph = ",".join(["%s"] * len(part))
        rows = conn.execute(
            f"SELECT code,date,high,low,close FROM prices WHERE code IN ({ph}) ORDER BY code,date",
            tuple(part)).fetchall()
        for r in rows:
            price.setdefault(r["code"], []).append(r)
        print(f"  価格ロード {min(i+400,len(codes))}/{len(codes)}", flush=True)
    price = {c: pd.DataFrame(v) for c, v in price.items()}

    out = []
    for rd, plist in sorted(by_date.items()):
        clean = materials.recent_material_scores_bulk([p["code"] for p in plist], rd)
        for p in plist:
            df = price.get(p["code"])
            if df is None:
                continue
            hist = df[df["date"] <= rd]
            if len(hist) < 20:
                continue
            t20 = hist.tail(20)
            dr = float(((t20["high"] - t20["low"]) / t20["close"]).mean())
            fl = json.loads(p["flags"] or "{}")
            out.append({
                "run_date": rd, "code": p["code"],
                "success": p["result_class"] in SUCCESS,
                "material": float(clean.get(p["code"], {}).get("material_raw", 0.0)),
                "chart": float(p["chart_score"] or 0),
                "volume": float(p["volume_score"] or 0),
                "theme": float(p["theme_score"] or 0),
                "similarity": float(p["similarity_score"] or 0),
                "fundamental": float(p["fundamental_score"] or 0),
                "prob": float(p["probability"] or 0),
                "upside": float(fl.get("upside", 0.5)),
                # 日中値幅は 0..1 に正規化して他成分とスケールを揃える(8%で1.0)
                "daily_range": min(dr / 0.08, 1.0),
            })
        print(f"  {rd}: {len(plist)}件", flush=True)
    return out


def topk_precision(rows: list[dict], score_fn, k: int = 10) -> tuple[float, int]:
    """各run_dateで上位K件を取り、その成功率を返す。"""
    by_date = defaultdict(list)
    for r in rows:
        by_date[r["run_date"]].append(r)
    picked = []
    for rd, rs in by_date.items():
        rs = sorted(rs, key=score_fn, reverse=True)[:k]
        picked.extend(rs)
    if not picked:
        return 0.0, 0
    return sum(1 for r in picked if r["success"]) / len(picked), len(picked)


def main() -> None:
    rows = load_samples()
    n = len(rows)
    base = sum(1 for r in rows if r["success"]) / n
    print(f"\nsamples={n}  baseline={base*100:.1f}%\n")

    print("=== 各成分の単独 top-K precision (その成分だけで順位付け) ===")
    print(f"{'成分':>14} {'top5':>10} {'top10':>10} {'top20':>10}")
    for c in COMPONENTS:
        cells = []
        for k in (5, 10, 20):
            p, m = topk_precision(rows, lambda r, c=c: r[c], k)
            cells.append(f"{p*100:5.1f}%({m})")
        print(f"{c:>14} " + " ".join(f"{x:>10}" for x in cells))

    cur_w = {"material": .26, "chart": .22, "volume": .22,
             "theme": .12, "similarity": .12, "fundamental": .06}

    def cur_score(r):
        w = sum(cur_w[k] * r[k] for k in cur_w)
        top = max(r["material"], r["chart"], r["volume"], r["theme"], r["similarity"])
        return 0.42 * w + 0.28 * r["prob"] + 0.18 * top + 0.12 * r["upside"]

    print("\n=== 現行配分 ===")
    for k in (5, 10, 20):
        p, m = topk_precision(rows, cur_score, k)
        print(f"  top{k}: {p*100:.1f}% (n={m})")

    # 粗いグリッド。material を下げ daily_range を入れる方向を確認する
    print("\n=== グリッドサーチ (粗い格子) ===", flush=True)
    grid = []
    steps = [0.0, 0.1, 0.2, 0.3, 0.4]
    for wm, wc, wv, wd in product(steps, steps, steps, [0.0, 0.2, 0.4, 0.6]):
        if wm + wc + wv + wd > 1.0:
            continue
        rest = 1.0 - (wm + wc + wv + wd)
        ws = {"material": wm, "chart": wc, "volume": wv, "daily_range": wd,
              "similarity": rest * 0.6, "prob": rest * 0.4}

        def fn(r, ws=ws):
            return sum(ws[k] * r[k] for k in ws)

        p10, _ = topk_precision(rows, fn, 10)
        grid.append((p10, ws))
    grid.sort(key=lambda x: -x[0])
    print("上位8配分 (top10 precision):")
    for p, ws in grid[:8]:
        s = " ".join(f"{k}={v:.2f}" for k, v in ws.items() if v > 0.001)
        print(f"  {p*100:5.1f}%  {s}")

    # 簡易ホールドアウト: run_date 前半で選び後半で検証
    dates = sorted({r["run_date"] for r in rows})
    half = len(dates) // 2
    tr = [r for r in rows if r["run_date"] in set(dates[:half])]
    te = [r for r in rows if r["run_date"] in set(dates[half:])]
    print(f"\n=== ホールドアウト (学習{len(dates[:half])}日 / 検証{len(dates[half:])}日) ===")
    gtr = []
    for wm, wc, wv, wd in product(steps, steps, steps, [0.0, 0.2, 0.4, 0.6]):
        if wm + wc + wv + wd > 1.0:
            continue
        rest = 1.0 - (wm + wc + wv + wd)
        ws = {"material": wm, "chart": wc, "volume": wv, "daily_range": wd,
              "similarity": rest * 0.6, "prob": rest * 0.4}
        p, _ = topk_precision(tr, lambda r, ws=ws: sum(ws[k] * r[k] for k in ws), 10)
        gtr.append((p, ws))
    gtr.sort(key=lambda x: -x[0])
    best_tr = gtr[0][1]
    p_te, m_te = topk_precision(te, lambda r: sum(best_tr[k] * r[k] for k in best_tr), 10)
    p_cur_te, _ = topk_precision(te, cur_score, 10)
    s = " ".join(f"{k}={v:.2f}" for k, v in best_tr.items() if v > 0.001)
    print(f"  学習側最良配分: {s}")
    print(f"  学習側 top10: {gtr[0][0]*100:.1f}%")
    print(f"  検証側 top10: {p_te*100:.1f}% (n={m_te})")
    print(f"  検証側 現行配分: {p_cur_te*100:.1f}%")


if __name__ == "__main__":
    main()
