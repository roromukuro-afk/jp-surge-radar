"""
教師データの構成と重み付けを変えたときに、モデルの順位付けが良くなるかを測る。

問題意識 (2026-09-21):
- sample_weights() は実データ(live)が少なかった頃の設計で、live の影響力を最大30%に
  抑える。バックフィルで live は6.5万件になり合成データ(historical 3.6万件)より多いが、
  今は合成データが7割の影響力を持つ。
- historical は古いコードで特徴量を計算しており、daily_range_20 が全件欠落(=0)。
  「この特徴量が0なら historical 側のパターン」という無意味な区別を覚えうる。

比較する3案(本番の学習と同じ GradientBoostingClassifier 設定):
  A  現行      : historical + live、現行の sample_weights
  B  重み均一  : historical + live、重みなし
  C  live のみ : live だけ、重みなし

評価は時系列で分ける。
  学習: t0_date < 2026-04-01
  検証: 2026-05-01 <= t0_date <= 2026-07-31 の live(バックフィル由来)
間の4月は捨てる(ラベルは先20営業日の値動きなので、境界をまたぐと検証期間の
情報が学習側に漏れる)。2026-08 以降の live は、早期成功が教師データに入らない
不具合(2e03309 で修正)で成功例が9割欠けているので使わない。

指標は日ごとの上位K件の成功率。教師データの live は各日のエンジン上位300件なので、
「その300件をモデルの確率で並べ替えたとき上位が当たるか」がそのまま測れる。
本番DBには一切書き込まない。
"""
from __future__ import annotations

import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from surge_radar import db  # noqa: E402
from surge_radar.features import FEATURE_KEYS, to_vector  # noqa: E402
from surge_radar.model import sample_weights  # noqa: E402

TRAIN_END = "2026-04-01"
TEST_START = "2026-05-01"
TEST_END = "2026-07-31"


def load() -> list[dict]:
    with db.cursor() as conn:
        rows = conn.execute(
            """SELECT t.source, t.t0_date, t.label, t.features, o.bars_tracked
               FROM teacher_samples t
               LEFT JOIN prediction_outcomes o ON o.prediction_id = t.prediction_id"""
        ).fetchall()
    out = []
    for r in rows:
        feats = db.loadj(r["features"], {})
        if not feats:
            continue
        out.append({
            "source": r["source"], "t0": r["t0_date"], "y": int(r["label"]),
            "x": to_vector(feats),
            "bars": r["bars_tracked"] if r["bars_tracked"] is not None else np.nan,
        })
    return out


def fit(rows: list[dict], weighted: bool):
    from sklearn.ensemble import GradientBoostingClassifier
    from sklearn.preprocessing import StandardScaler

    X = np.nan_to_num(np.array([r["x"] for r in rows], dtype=float))
    y = np.array([r["y"] for r in rows], dtype=int)
    scaler = StandardScaler().fit(X)
    w = None
    if weighted:
        w = sample_weights([r["source"] for r in rows],
                           np.array([r["bars"] for r in rows], dtype=float))
    clf = GradientBoostingClassifier(n_estimators=200, max_depth=3, learning_rate=0.05,
                                     subsample=0.85, random_state=42)
    clf.fit(scaler.transform(X), y, sample_weight=w)
    return scaler, clf, w


def evaluate(scaler, clf, test: list[dict]) -> dict:
    from sklearn.metrics import roc_auc_score

    X = np.nan_to_num(np.array([r["x"] for r in test], dtype=float))
    y = np.array([r["y"] for r in test], dtype=int)
    p = clf.predict_proba(scaler.transform(X))[:, 1]
    by_day = defaultdict(list)
    for r, pi in zip(test, p):
        by_day[r["t0"]].append((pi, r["y"]))
    res = {"auc": round(float(roc_auc_score(y, p)), 4), "days": len(by_day)}
    for k in (5, 10, 20):
        picks = []
        for rs in by_day.values():
            picks.extend(sorted(rs, key=lambda t: -t[0])[:k])
        res[f"top{k}"] = round(sum(t[1] for t in picks) / len(picks), 4)
    res["mean_prob"] = round(float(p.mean()), 4)
    return res


def main() -> None:
    t0 = time.time()
    rows = load()
    print(f"教師データ {len(rows)}件を読み込み ({time.time()-t0:.0f}s)", flush=True)

    live = ("live_success", "live_fail")
    train_all = [r for r in rows if r["t0"] < TRAIN_END]
    train_live = [r for r in train_all if r["source"] in live]
    test = [r for r in rows if r["source"] in live and TEST_START <= r["t0"] <= TEST_END]

    def desc(rs):
        n = len(rs)
        return f"{n}件 正例率{sum(r['y'] for r in rs)/n*100:.1f}%"

    print(f"学習(全体)  : {desc(train_all)}  historical {sum(r['source'] not in live for r in train_all)}件")
    print(f"学習(liveのみ): {desc(train_live)}")
    print(f"検証        : {desc(test)} / {len({r['t0'] for r in test})}日")
    base = sum(r["y"] for r in test) / len(test)

    variants = [("A 現行(混合+現行重み)", train_all, True),
                ("B 混合・重み均一", train_all, False),
                ("C liveのみ・重み均一", train_live, False)]
    results = []
    for name, tr, weighted in variants:
        ts = time.time()
        scaler, clf, w = fit(tr, weighted)
        res = evaluate(scaler, clf, test)
        if w is not None:
            src = np.array([r["source"] for r in tr])
            share = w[np.isin(src, live)].sum() / w.sum()
            res["live_weight_share"] = round(float(share), 3)
        imp = dict(zip(FEATURE_KEYS, clf.feature_importances_))
        res["imp_daily_range_20"] = round(float(imp.get("daily_range_20", 0.0)), 4)
        results.append((name, res))
        print(f"  {name}: {res}  ({time.time()-ts:.0f}s)", flush=True)

    print(f"\n検証の成功率(ベースライン): {base*100:.1f}%")
    print(f"{'案':<24} {'top5':>7} {'top10':>7} {'top20':>7} {'AUC':>7} {'平均確率':>8}")
    for name, r in results:
        print(f"{name:<24} {r['top5']*100:6.1f}% {r['top10']*100:6.1f}% {r['top20']*100:6.1f}% "
              f"{r['auc']:7.4f} {r['mean_prob']*100:7.1f}%")


if __name__ == "__main__":
    main()
