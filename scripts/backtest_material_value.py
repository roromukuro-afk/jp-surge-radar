"""
材料スコアは本当に成功を予測するのか、そして汚染除去で改善したのかを測る。

背景:
- scoring.WEIGHTS は material に 0.26 と全サブスコア中で最大の重みを置いている
- しかし教師データ40,202件の89.7%は historical で material_raw が全て0のため、
  MLモデルの材料系特徴量の重要度は合計 0.000 = 学習で裏付けられていない
- さらに2026-09-17に材料の26〜29%が他社記事・市場全体ダイジェストと判明した

そこで満期済み予測について、
  (a) 予測時に保存された material_score(汚染込み)
  (b) 汚染を除外して同じ as-of で再計算した material_raw(クリーン)
の2つを、それぞれ成功率と突き合わせる。
(a)に予測力が無く(b)にあれば、汚染除去が効いたことになる。
どちらにも無ければ、material の重み0.26自体が正当化されていない。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from surge_radar import db, materials  # noqa: E402

SUCCESS = {"S", "A", "B"}
BUCKETS = [(0.0, 0.001), (0.001, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.5), (0.5, 1.01)]
LABELS = ["0(材料なし)", "0-0.1", "0.1-0.2", "0.2-0.3", "0.3-0.5", "0.5+"]


def table(rows: list[dict], key: str, title: str) -> None:
    print(f"\n--- {title} ---")
    print(f"{'帯':>12} {'n':>6} {'成功':>5} {'成功率':>8} {'95%CI':>15}")
    for (lo, hi), lab in zip(BUCKETS, LABELS):
        sel = [r for r in rows if lo <= r[key] < hi]
        n = len(sel)
        if n < 20:
            print(f"{lab:>12} {n:6d}   n<20")
            continue
        s = sum(1 for r in sel if r["success"])
        p = s / n
        se = (p * (1 - p) / n) ** 0.5
        print(f"{lab:>12} {n:6d} {s:5d} {p*100:7.1f}% "
              f"[{max(0,p-1.96*se)*100:4.1f}-{min(1,p+1.96*se)*100:4.1f}%]")


def main() -> None:
    conn = db.connect()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
    cutoff = dates[-21]
    preds = conn.execute("""
        SELECT p.code, p.run_date, p.category, p.material_score, o.result_class
        FROM predictions p JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE p.run_date <= %s AND o.result_class IS NOT NULL
    """, (cutoff,)).fetchall()
    print(f"満期済み予測: {len(preds)}件 (cutoff {cutoff})")

    by_date: dict[str, list] = defaultdict(list)
    for p in preds:
        by_date[p["run_date"]].append(p)

    rows = []
    for i, (rd, plist) in enumerate(sorted(by_date.items()), 1):
        codes = [p["code"] for p in plist]
        clean = materials.recent_material_scores_bulk(codes, rd)
        for p in plist:
            c = clean.get(p["code"], {})
            rows.append({
                "success": p["result_class"] in SUCCESS,
                "stored": float(p["material_score"] or 0.0),
                "clean": float(c.get("material_raw", 0.0)),
                "category": p["category"],
            })
        if i % 5 == 0:
            print(f"  {i}/{len(by_date)} run_dates", flush=True)

    n = len(rows)
    base = sum(1 for r in rows if r["success"]) / n
    print(f"\nsamples={n} baseline={base*100:.1f}%")

    table(rows, "stored", "予測時に保存された material_score (汚染込み)")
    table(rows, "clean", "汚染除去後に再計算した material_raw")

    # 材料の有無で二分した単純比較
    for key, lab in (("stored", "汚染込み"), ("clean", "クリーン")):
        has = [r for r in rows if r[key] > 0.05]
        non = [r for r in rows if r[key] <= 0.05]
        if len(has) >= 20 and len(non) >= 20:
            ph = sum(1 for r in has if r["success"]) / len(has)
            pn = sum(1 for r in non if r["success"]) / len(non)
            print(f"\n{lab}: 材料あり(>0.05) {ph*100:.1f}% (n={len(has)}) vs "
                  f"材料なし {pn*100:.1f}% (n={len(non)})  差 {(ph-pn)*100:+.1f}pt")


if __name__ == "__main__":
    main()
