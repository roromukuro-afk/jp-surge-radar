"""
自律学習ループ: 成功/失敗パターンの分析結果を分類・スコアリングへ反映する。

設計方針 (完全自律・承認ゲートなし。ただし安全性はベイズ的shrinkageで担保):
  - classify_path (B_very_strong_ai 等) ごとの実績勝率・危険失敗率を集計し、
    "trust_multiplier" (0.80〜1.20) としてスコアに反映する。
  - 件数が少ないpathほど、全体平均(prior)に強く引き寄せられる(shrinkage)ため、
    n=2件のような統計的に無意味な差で暴走することはない。件数が増えるほど、
    そのpath固有の実績が multiplier に強く反映されるようになる。
  - 承認は挟まない。毎日の daily パイプラインで自動的に再計算・反映される。

このモジュールは「読み取り専用の診断(diag_outcomes.py)」とは別物:
  diag_outcomes.py = 人間向けレビュー (何も変更しない)
  learning.py      = 本番スコアリングに実際に反映される自律フィードバック
"""
from __future__ import annotations

import json
from datetime import datetime

from . import db

SUCCESS = {"S", "A", "B"}
DANGER = {"danger_fail"}

# ベイズ的shrinkage: 疑似観測数。小さいpathほどpriorに引き寄せられる。
PRIOR_STRENGTH = 30.0
# trust_multiplier の可動域 (完全自律でも暴走しないための上下限)
MULT_MIN, MULT_MAX = 0.80, 1.20
# shrunk_rate と prior の差にかける感度 (maturity_rampで実効値はさらに減衰)
GAIN_HIT = 1.2
GAIN_DANGER = 1.5
# システム全体の成熟度ランプ: 何本のrun_dateコホートが20営業日(概算28暦日)経過したら
# フル感度にするか。稼働直後は「+20%に早く到達したpathだけが確定し、遅く失敗する
# pathの負けがまだ確定していない」生存者バイアスが強いため、per-path shrinkageとは
# 別に、システム全体の運用歴が浅いうちは trust_multiplier の振れ幅そのものを抑える。
MATURITY_RAMP_COHORTS = 20.0


def _loadj(s, fallback):
    try:
        return json.loads(s) if s else fallback
    except Exception:
        return fallback


def _maturity_ramp(conn) -> float:
    """システム運用歴の成熟度 (0..1)。

    run_date が28暦日(概算20営業日)以上前のコホート数が MATURITY_RAMP_COHORTS に
    達するまでは、trust_multiplier の振れ幅を比例的に抑える。稼働直後は
    「早期成功だけが確定し、遅い失敗はまだ未確定」という生存者バイアスが強いため。
    """
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(days=28)).strftime("%Y-%m-%d")
    r = conn.execute(
        "SELECT COUNT(DISTINCT run_date) n FROM predictions "
        "WHERE COALESCE(origin,'live')='live' AND run_date <= %s", (cutoff,)
    ).fetchone()
    matured = r["n"] if r else 0
    return min(1.0, matured / MATURITY_RAMP_COHORTS)


# 満期とみなす追跡日数。+20%到達は即確定するため、全judgedをそのまま使うと
# 「早く決着した勝ちパターン」が過大代表され (実測: 早期成功の88%がbars<18)、
# trust_multiplier が生存者バイアスで歪む (2026-07-27に実際発生・確認済み)。
# bars>=18 (ほぼ満期まで追跡できた) だけに絞ることで、勝ち負け双方に公平な母集団にする。
MATURE_BARS = 18


def compute_path_performance() -> dict:
    """live判定済み予測のうち"満期"(bars_tracked>=18)のみから classify_path 別の
    trust_multiplier を再計算し、path_performance テーブルへ upsert する。
    毎日の daily パイプラインから呼ばれる。

    早期に+20%到達して確定したもの(大半がbars<18)は意図的に除外する。含めると
    「早く動く」pathの勝率が実態以上に高く見える生存者バイアスがかかるため
    (model.py の sample_weights と同じ理由・同じ閾値)。
    """
    with db.cursor() as conn:
        rows = conn.execute(
            """SELECT p.flags, o.result_class
               FROM predictions p JOIN prediction_outcomes o ON o.prediction_id=p.id
               WHERE p.status='judged' AND COALESCE(p.origin,'live')='live'
                 AND o.bars_tracked >= %s""", (MATURE_BARS,)
        ).fetchall()
        ramp = _maturity_ramp(conn)

    if not rows:
        return {"paths": 0, "reason": "no judged live predictions yet"}

    by_path: dict[str, list[str]] = {}
    for r in rows:
        f = _loadj(r["flags"], {})
        path = f.get("classify_path") or "(none)"
        by_path.setdefault(path, []).append(r["result_class"])

    total = len(rows)
    global_success = sum(1 for r in rows if r["result_class"] in SUCCESS)
    global_danger = sum(1 for r in rows if r["result_class"] in DANGER)
    prior_hit = global_success / total
    prior_danger = global_danger / total

    updates = []
    summary = []
    for path, results in by_path.items():
        n = len(results)
        n_succ = sum(1 for r in results if r in SUCCESS)
        n_danger = sum(1 for r in results if r in DANGER)
        raw_hit = n_succ / n
        shrunk_hit = (n_succ + prior_hit * PRIOR_STRENGTH) / (n + PRIOR_STRENGTH)
        shrunk_danger = (n_danger + prior_danger * PRIOR_STRENGTH) / (n + PRIOR_STRENGTH)

        trust = 1.0 + (shrunk_hit - prior_hit) * GAIN_HIT * ramp - (shrunk_danger - prior_danger) * GAIN_DANGER * ramp
        trust = max(MULT_MIN, min(MULT_MAX, trust))

        updates.append((path, n, n_succ, n_danger, round(raw_hit, 4),
                        round(shrunk_hit, 4), round(shrunk_danger, 4), round(trust, 4)))
        summary.append({"path": path, "n": n, "hit_rate": round(raw_hit, 3),
                        "trust_multiplier": round(trust, 3)})

    UPSERT_PG = """
        INSERT INTO path_performance
          (classify_path,n_judged,n_success,n_danger_fail,raw_hit_rate,
           shrunk_hit_rate,shrunk_danger_rate,trust_multiplier,updated_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,CURRENT_TIMESTAMP)
        ON CONFLICT(classify_path) DO UPDATE SET
          n_judged=excluded.n_judged, n_success=excluded.n_success,
          n_danger_fail=excluded.n_danger_fail, raw_hit_rate=excluded.raw_hit_rate,
          shrunk_hit_rate=excluded.shrunk_hit_rate, shrunk_danger_rate=excluded.shrunk_danger_rate,
          trust_multiplier=excluded.trust_multiplier, updated_at=CURRENT_TIMESTAMP"""

    with db.cursor() as conn:
        for u in updates:
            conn.execute(UPSERT_PG, u)

    summary.sort(key=lambda s: -s["n"])
    return {
        "paths": len(updates),
        "total_judged": total,
        "prior_hit_rate": round(prior_hit, 3),
        "prior_danger_rate": round(prior_danger, 3),
        "maturity_ramp": round(ramp, 3),
        "by_path": summary,
        "computed_at": datetime.now().isoformat(timespec="seconds"),
    }


def get_trust_multipliers() -> dict[str, float]:
    """predict.generate() から呼ばれる。path -> trust_multiplier の辞書を返す。
    未計算/未知のpathは呼び出し側で 1.0 (無調整) として扱うこと。
    """
    try:
        with db.cursor() as conn:
            rows = conn.execute(
                "SELECT classify_path, trust_multiplier FROM path_performance"
            ).fetchall()
        return {r["classify_path"]: float(r["trust_multiplier"]) for r in rows}
    except Exception:
        return {}


def live_sample_weight_fraction(n_live: int, n_hist: int,
                                target_cap: float = 0.30, ramp: float = 500.0) -> float:
    """live教師データが全体に占めるべき"重み"の目標比率。

    件数が少ないうちは低め(過学習防止)、live_sampleが増えるほど target_cap まで
    自動的に引き上がる。人手でのチューニングを要さない自己スケール設計。
    """
    if n_hist <= 0 or n_live <= 0:
        return 0.0
    return min(target_cap, 0.05 + n_live / ramp)
