"""
_classify() の composite 閾値を新しいスコア分布に合わせて較正する。

重みと式を変えた結果 composite の分布が上にずれ、B判定が 4,798件中 2,763件
(58%)まで膨張した。旧分布で各閾値が占めていたパーセンタイルを求め、新分布で
同じパーセンタイルにあたる値を出す。候補の出現数を保ったまま中身だけ
入れ替えるのが狙い。
"""
from __future__ import annotations

import bisect
import json
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from surge_radar import db, materials, scoring  # noqa: E402
from surge_radar.scoring import _clip01  # noqa: E402

OLD_W = {"material": .26, "chart": .22, "volume": .22,
         "theme": .12, "similarity": .12, "fundamental": .06}
THRESHOLDS = [0.62, 0.53, 0.52, 0.50, 0.45, 0.43, 0.38]


def old_chart(f):
    s = 0.0
    s += 0.18 * _clip01(f.get("downtrend_stopped", 0) + 0.5)
    s += 0.14 * _clip01(f.get("volatility_contraction", 0))
    s += 0.12 * _clip01(f.get("sideways", 0))
    s += 0.16 * _clip01(f.get("higher_lows", 0) + 0.3)
    s += 0.10 * _clip01(f.get("lower_highs_stopped", 0) + 0.3)
    s += 0.16 * _clip01(f.get("near_breakout", 0))
    s += 0.08 * f.get("broke_resistance", 0)
    s += 0.06 * f.get("price_above_ma25", 0)
    if f.get("ma25_slope", 0) > 0 and f.get("price_above_ma25", 0):
        s += 0.05
    s -= 0.30 * _clip01(f.get("downtrend_risk", 0))
    s -= 0.15 * f.get("rebound_capped", 0)
    s -= 0.15 * f.get("high_zone_upper_wick", 0)
    return _clip01(s)


def main() -> None:
    conn = db.connect()
    preds = conn.execute("""
        SELECT p.code, p.run_date, p.probability, p.similarity_score, p.features
        FROM predictions p WHERE p.run_date >= '2026-08-01'
    """).fetchall()
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

    old_c, new_c = [], []
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
            ft["daily_range_20"] = float(((t20["high"] - t20["low"]) / t20["close"]).mean())
            m = clean.get(p["code"], {})
            ft["material_raw"] = float(m.get("material_raw", 0.0))
            ft["pos_impact"] = float(m.get("pos_impact", 0.0))
            ft["n_materials"] = int(m.get("n_materials", 0))
            prob = float(p["probability"] or 0)
            sim = float(p["similarity_score"] or 0)

            # 旧: 旧chart・旧重み・旧式
            osub = {"material": scoring.material_score(ft), "chart": old_chart(ft),
                    "volume": scoring.volume_score(ft), "theme": scoring.theme_score(ft),
                    "similarity": sim, "fundamental": scoring.fundamental_score(ft)}
            ow = sum(OLD_W[k] * osub[k] for k in OLD_W)
            otop = max(osub["material"], osub["chart"], osub["volume"],
                       osub["theme"], osub["similarity"])
            old_c.append(0.42 * ow + 0.28 * prob + 0.18 * otop + 0.12 * 0.5)

            new_c.append(scoring.score_candidate(ft, ml_prob=prob, similarity=sim)["score"])
        print(f"  {rd}", flush=True)

    old_c.sort()
    new_c.sort()
    n = len(old_c)
    print(f"\n標本 {n}件")
    print(f"旧composite 中央値 {old_c[n//2]:.3f} / 新 {new_c[n//2]:.3f}")
    print(f"\n{'旧閾値':>8} {'旧パーセンタイル':>14} {'該当率':>8} {'→ 新閾値':>10}")
    for th in THRESHOLDS:
        p = bisect.bisect_left(old_c, th) / n * 100
        nv = new_c[min(int(n * p / 100), n - 1)]
        above = sum(1 for x in old_c if x >= th) / n * 100
        print(f"{th:8.2f} {p:13.1f}% {above:7.1f}% {nv:9.3f}")


if __name__ == "__main__":
    main()
