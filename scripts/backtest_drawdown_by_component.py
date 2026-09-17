"""
daily_range で上位を取る戦略の実用性を、成功率ではなく下落側から検証する。

optimize_weights.py で daily_range 単独の top10 precision が 62.5% と出たが、
「+20%動きやすい」ことは「-20%動きやすい」ことと表裏のはずで、成功の定義が
上昇のみである以上この指標は構造的に有利に出る。保有できるかどうかは
最大ドローダウン・danger_fail率・価格帯・流動性で決まるので、そちらを測る。
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from surge_radar import db, materials  # noqa: E402

SUCCESS = {"S", "A", "B"}


def main() -> None:
    conn = db.connect()
    dates = [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
    cutoff = dates[-21]
    preds = conn.execute("""
        SELECT p.code, p.run_date, p.base_price, p.probability, p.chart_score,
               p.volume_score, p.theme_score, p.fundamental_score, p.similarity_score,
               p.flags, p.features,
               o.result_class, o.max_drawdown, o.max_up_20d, o.days_to_20pct
        FROM predictions p JOIN prediction_outcomes o ON o.prediction_id = p.id
        WHERE p.run_date <= %s AND o.result_class IS NOT NULL
    """, (cutoff,)).fetchall()
    print(f"満期済み {len(preds)}件 (cutoff {cutoff})", flush=True)

    codes = sorted({p["code"] for p in preds})
    price = {}
    for i in range(0, len(codes), 400):
        part = codes[i:i + 400]
        ph = ",".join(["%s"] * len(part))
        for r in conn.execute(
                f"SELECT code,date,high,low,close FROM prices WHERE code IN ({ph}) ORDER BY code,date",
                tuple(part)).fetchall():
            price.setdefault(r["code"], []).append(r)
    price = {c: pd.DataFrame(v) for c, v in price.items()}
    print(f"価格ロード {len(price)}銘柄", flush=True)

    by_date = defaultdict(list)
    for p in preds:
        by_date[p["run_date"]].append(p)

    rows = []
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
            ft = json.loads(p["features"] or "{}")
            rows.append({
                "run_date": rd, "code": p["code"],
                "success": p["result_class"] in SUCCESS,
                "danger": p["result_class"] == "danger_fail",
                "dd": float(p["max_drawdown"] or 0),
                "base_price": float(p["base_price"] or 0),
                "liquidity_ok": int(ft.get("liquidity_ok", 0)),
                "turnover_log": float(ft.get("turnover_log", 0)),
                "daily_range": dr,
                "prob": float(p["probability"] or 0),
                "material": float(clean.get(p["code"], {}).get("material_raw", 0.0)),
                "chart": float(p["chart_score"] or 0),
                "upside": float(fl.get("upside", 0.5)),
            })

    def report(sel: list[dict], label: str) -> None:
        if not sel:
            print(f"{label}: 0件")
            return
        n = len(sel)
        succ = sum(1 for r in sel if r["success"]) / n
        dang = sum(1 for r in sel if r["danger"]) / n
        dd = [r["dd"] for r in sel]
        deep = sum(1 for r in sel if r["dd"] <= -0.20) / n
        liq = sum(r["liquidity_ok"] for r in sel) / n
        px = statistics.median(r["base_price"] for r in sel)
        print(f"{label:>22} n={n:4d} 成功{succ*100:5.1f}% danger_fail{dang*100:5.1f}% "
              f"DD中央{statistics.median(dd)*100:6.1f}% DD最悪{min(dd)*100:6.1f}% "
              f"-20%超{deep*100:5.1f}% 流動性OK{liq*100:5.0f}% 中央値¥{px:,.0f}")

    print()
    print("各 run_date で上位10件を取ったときの下落側の実態")
    print(f"{'':>22} {'n':>6} {'成功':>8} {'danger':>12} {'DD中央':>9} {'DD最悪':>8} {'-20%超':>8} {'流動性':>8} {'株価':>10}")
    for key in ("daily_range", "prob", "upside", "material", "chart"):
        picked = []
        for rd, rs in by_date.items():
            sub = [r for r in rows if r["run_date"] == rd]
            picked.extend(sorted(sub, key=lambda r: r[key], reverse=True)[:10])
        report(picked, f"top10 by {key}")
    report(rows, "全体")

    print()
    print("=== daily_range 上位10件を流動性・価格で絞った場合 ===")
    for cond, lab in (
        (lambda r: r["liquidity_ok"] == 1, "流動性OKのみ"),
        (lambda r: r["base_price"] >= 300, "300円以上"),
        (lambda r: r["liquidity_ok"] == 1 and r["base_price"] >= 300, "流動性OK & 300円以上"),
    ):
        picked = []
        for rd in by_date:
            sub = [r for r in rows if r["run_date"] == rd and cond(r)]
            picked.extend(sorted(sub, key=lambda r: r["daily_range"], reverse=True)[:10])
        report(picked, lab)

    print()
    print("=== daily_range 帯ごとの上下対称性 ===")
    print(f"{'帯':>10} {'n':>6} {'成功':>8} {'danger':>9} {'+20%到達':>10} {'-20%到達':>10} {'比':>7}")
    for lo, hi, lab in ((0, .02, "<2%"), (.02, .03, "2-3%"), (.03, .04, "3-4%"),
                        (.04, .06, "4-6%"), (.06, 1, "6%超")):
        sel = [r for r in rows if lo <= r["daily_range"] < hi]
        if len(sel) < 20:
            continue
        n = len(sel)
        up = sum(1 for r in sel if r["success"]) / n
        dn = sum(1 for r in sel if r["dd"] <= -0.20) / n
        dg = sum(1 for r in sel if r["danger"]) / n
        print(f"{lab:>10} {n:6d} {up*100:7.1f}% {dg*100:8.1f}% {up*100:9.1f}% {dn*100:9.1f}% "
              f"{(up/dn if dn else 0):6.2f}")


if __name__ == "__main__":
    main()
