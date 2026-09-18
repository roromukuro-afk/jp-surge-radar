"""
スコアリング層の自己較正。

これまで WEIGHTS・composite係数・chart_score の各係数はすべて手で置いた定数で、
「何が効いているか」を実績と突き合わせる仕組みが無かった。その結果 chart_score は
プロジェクト開始から2026-09-17まで逆指標(上位10件の成功率6.9% / 下位10件15.0%)の
まま動き続け、material は予測力ゼロのまま全サブスコア中最大の0.26を持ち続けていた。
人間が手で測らない限り永遠に気づけない構造だったので、モデル側と同じ
「測る → 学習 → ホールドアウト検証 → 改善したときだけ採用」のループを持たせる。

設計:
- 満期済み(20営業日完走)の予測だけを使う。早期成功バイアスを避けるため。
- 評価指標は top-K precision。アプリは日ごとの順位付きリストを出すので、
  「各run_dateで上位K件に入れた銘柄の成功率」が実運用に最も近い。
- run_date を前半/後半に分け、前半で選び後半で検証する。全期間で最良の配分は
  16 run_date への過学習を含む(実測で学習77.5%→検証48.8%と20pt落ちた)。
- 現行配分を検証側で上回り、かつ改善幅が TOLERANCE を超えたときだけ採用する。
  model.train() の昇格ガードと同じ考え方。
- 採用した配分は scoring_weights テーブルに版として残し、scoring.py が読む。
"""
from __future__ import annotations

import json
from datetime import datetime
from itertools import product

from . import db

SUCCESS = ("S", "A", "B")

# 検証側で現行をこれだけ上回らないと採用しない(top-K precision の絶対差)
TOLERANCE = 0.02
# 較正に必要な最小の満期 run_date 数。これ未満なら何もしない
MIN_RUN_DATES = 10
# 評価に使う上位件数
EVAL_K = 10

SUB_KEYS = ("material", "chart", "volume", "theme", "similarity",
            "fundamental", "volatility")


def _table_ready(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scoring_weights (
            version      TEXT PRIMARY KEY,
            created_at   TEXT,
            weights      TEXT,
            coeffs       TEXT,
            metrics      TEXT,
            n_run_dates  INTEGER,
            promoted     BOOLEAN DEFAULT FALSE,
            notes        TEXT
        )""")


def current() -> dict | None:
    """採用中の重みと係数。未較正なら None (scoring.py のデフォルトが使われる)。"""
    try:
        with db.cursor() as conn:
            _table_ready(conn)
            r = conn.execute(
                "SELECT weights, coeffs, version FROM scoring_weights "
                "WHERE promoted ORDER BY created_at DESC LIMIT 1").fetchone()
    except Exception:
        return None
    if not r:
        return None
    try:
        return {"weights": json.loads(r["weights"]),
                "coeffs": json.loads(r["coeffs"]),
                "version": r["version"]}
    except Exception:
        return None


def _topk(rows: list[dict], score_fn, k: int = EVAL_K) -> tuple[float, float, int]:
    by_date: dict[str, list] = {}
    for r in rows:
        by_date.setdefault(r["run_date"], []).append(r)
    picked = []
    for rs in by_date.values():
        picked.extend(sorted(rs, key=score_fn, reverse=True)[:k])
    if not picked:
        return 0.0, 0.0, 0
    succ = sum(1 for r in picked if r["success"]) / len(picked)
    dang = sum(1 for r in picked if r["danger"]) / len(picked)
    return succ, dang, len(picked)


def _score_with(r: dict, w: dict, c: dict) -> float:
    weighted = sum(w.get(k, 0.0) * r["sub"].get(k, 0.0) for k in SUB_KEYS)
    top = max(r["sub"].get(k, 0.0) for k in
              ("material", "chart", "volume", "theme", "similarity"))
    return (c["weighted"] * weighted + c["prob"] * r["prob"]
            + c["top"] * top + c["upside"] * r["upside"])


def load_matured_samples(max_run_dates: int = 60) -> list[dict]:
    """満期済み予測を、サブスコアを再計算した形で返す。

    保存済みのサブスコアは「その時点のコードで計算した値」なので、
    scoring.py を直した後の較正には使えない。features から計算し直す。

    run_date が多いと計算に時間がかかる(1日300件ぶん score_candidate を回す)。
    バックフィルで数百日ぶんになったため、日次パイプラインの時間内に収まるよう
    期間全体から均等に max_run_dates 日を間引いて使う。直近だけに寄せないのは、
    特定の地合いの期間に較正が偏るのを避けるため。
    """
    import pandas as pd

    from . import materials, model, scoring

    with db.cursor() as conn:
        dates = [x["date"] for x in conn.execute(
            "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]
        if len(dates) < 25:
            return []
        cutoff = dates[-21]
        preds = conn.execute("""
            SELECT p.code, p.run_date, p.probability, p.similarity_score, p.features,
                   p.origin, o.result_class
            FROM predictions p JOIN prediction_outcomes o ON o.prediction_id = p.id
            WHERE p.run_date <= %s AND o.result_class IS NOT NULL
            ORDER BY p.run_date DESC
        """, (cutoff,)).fetchall()
    if not preds:
        return []

    all_dates = sorted({p["run_date"] for p in preds})
    if len(all_dates) > max_run_dates:
        stride = len(all_dates) / max_run_dates
        keep = {all_dates[int(i * stride)] for i in range(max_run_dates)}
    else:
        keep = set(all_dates)
    preds = [p for p in preds if p["run_date"] in keep]

    codes = sorted({p["code"] for p in preds})
    px: dict[str, list] = {}
    with db.cursor() as conn:
        for i in range(0, len(codes), 400):
            part = codes[i:i + 400]
            ph = ",".join(["%s"] * len(part))
            for r in conn.execute(
                    f"SELECT code,date,high,low,close FROM prices "
                    f"WHERE code IN ({ph}) ORDER BY code,date", tuple(part)).fetchall():
                px.setdefault(r["code"], []).append(r)
    px = {c: pd.DataFrame(v) for c, v in px.items()}

    by_date: dict[str, list] = {}
    for p in preds:
        by_date.setdefault(p["run_date"], []).append(p)

    sim_th = model.Predictor().sim_thresholds
    out = []
    for rd, plist in sorted(by_date.items()):
        mat = materials.recent_material_scores_bulk([p["code"] for p in plist], rd)
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
            m = mat.get(p["code"], {})
            ft["material_raw"] = float(m.get("material_raw", 0.0))
            ft["pos_impact"] = float(m.get("pos_impact", 0.0))
            ft["n_materials"] = int(m.get("n_materials", 0))
            res = scoring.score_candidate(
                ft, ml_prob=float(p["probability"] or 0),
                similarity=float(p["similarity_score"] or 0), sim_thresholds=sim_th)
            out.append({
                "run_date": rd,
                "origin": p["origin"] or "live",
                "success": p["result_class"] in SUCCESS,
                "danger": p["result_class"] == "danger_fail",
                "sub": res["sub"],
                "prob": float(p["probability"] or 0),
                "upside": res["upside"],
                "gated": bool(res["gates"]),
            })
    return out


def measure_components(rows: list[dict]) -> dict:
    """各サブスコアを単独で順位付けしたときの成績。符号の異常検出に使う。"""
    out = {}
    for k in SUB_KEYS:
        hi_s, hi_d, n = _topk(rows, lambda r, k=k: r["sub"].get(k, 0.0))
        lo_s, _, _ = _topk(rows, lambda r, k=k: -r["sub"].get(k, 0.0))
        out[k] = {"top_success": round(hi_s, 4), "top_danger": round(hi_d, 4),
                  "bottom_success": round(lo_s, 4), "n": n,
                  # 下位のほうが成功しているなら、そのサブスコアは逆指標
                  "inverted": lo_s > hi_s}
    return out


def _grid(sim_weight: float):
    """価格由来の成分の重みだけを探索する。similarity は sim_weight に固定する。

    バックフィル予測の similarity と prob は先読みで汚染されている: 過去日を
    採点したモデルはその日より後の結果を学習済みで、similarity の正例プールにも
    その銘柄の将来の急騰が入りうる。これらの重みをバックフィル込みで学習すると
    楽観的に過大評価される。chart/volatility/volume/theme/fundamental/material は
    特徴量が価格(と当時の材料)から作られるので汚染されない。
    """
    steps = (0.0, 0.1, 0.2, 0.3)
    vols = (0.1, 0.2, 0.3, 0.4)
    budget = 1.0 - sim_weight
    for wc, wv, wm in product(steps, vols, steps):
        used = wc + wv + wm
        if used > budget - 0.02 or used < budget * 0.5:
            continue
        rest = budget - used
        yield {"chart": wc, "similarity": sim_weight, "volatility": wv, "material": wm,
               "volume": rest * 0.4, "theme": rest * 0.4, "fundamental": rest * 0.2}


def _coeff_grid(prob_coeff: float):
    """composite 係数の探索。prob は prob_coeff に固定する(理由は _grid と同じ)。"""
    for top in (0.08, 0.13, 0.18):
        for upside in (0.05, 0.10, 0.15):
            weighted = 1.0 - prob_coeff - top - upside
            if weighted <= 0.3:
                continue
            yield {"weighted": round(weighted, 4), "prob": prob_coeff,
                   "top": top, "upside": upside}


def calibrate(store: bool = True, notes: str = "") -> dict:
    """満期データから重みを再学習し、検証側で改善したときだけ採用する。"""
    from . import scoring

    rows = load_matured_samples()
    if not rows:
        return {"calibrated": False, "reason": "満期済みサンプルなし"}
    dates = sorted({r["run_date"] for r in rows})
    if len(dates) < MIN_RUN_DATES:
        return {"calibrated": False, "n_run_dates": len(dates),
                "reason": f"満期run_dateが{len(dates)}日で較正には不足"}

    # ゲート該当はカテゴリ付与で除外されるので較正からも外す
    rows = [r for r in rows if not r["gated"]]
    half = len(dates) // 2
    train = [r for r in rows if r["run_date"] in set(dates[:half])]
    test = [r for r in rows if r["run_date"] in set(dates[half:])]

    cur_w, cur_c = scoring.active_weights()
    cur_s, cur_d, _ = _topk(test, lambda r: _score_with(r, cur_w, cur_c))

    # similarity の重みと prob 係数は現行値に固定して探索する(_grid の説明を参照)
    best = None
    for w in _grid(cur_w.get("similarity", 0.0)):
        for c in _coeff_grid(cur_c.get("prob", 0.10)):
            s, _, _ = _topk(train, lambda r, w=w, c=c: _score_with(r, w, c))
            if best is None or s > best[0]:
                best = (s, w, c)
    _, bw, bc = best
    new_s, new_d, n_te = _topk(test, lambda r: _score_with(r, bw, bc))

    # 実運用に近い live 由来の日だけでも同じ比較を出しておく。バックフィル分は
    # prob/similarity が先読み込みの値なので、絶対水準は楽観的に出る。
    live_test = [r for r in test if r["origin"] == "live"]
    live_cur, _, n_live = _topk(live_test, lambda r: _score_with(r, cur_w, cur_c))
    live_new, _, _ = _topk(live_test, lambda r: _score_with(r, bw, bc))

    comps = measure_components(rows)
    inverted = [k for k, v in comps.items() if v["inverted"] and v["n"] >= 50]

    improved = new_s - cur_s
    promote = improved > TOLERANCE
    # バックフィル込みで改善していても、実運用(live)の日で悪化するなら採用しない。
    # live の検証日が少なすぎる間は判定材料にならないので、この条件は掛けない。
    if promote and n_live >= EVAL_K * 4 and live_new < live_cur:
        promote = False
    version = f"w{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    metrics = {
        "test_success": round(new_s, 4), "test_danger": round(new_d, 4),
        "current_success": round(cur_s, 4), "current_danger": round(cur_d, 4),
        "improvement": round(improved, 4), "n_test": n_te,
        "live_test_current": round(live_cur, 4), "live_test_new": round(live_new, 4),
        "n_live_test": n_live,
        "origins": {o: sum(1 for r in rows if r["origin"] == o)
                    for o in sorted({r["origin"] for r in rows})},
        "components": comps, "inverted_components": inverted,
    }

    if store:
        with db.cursor() as conn:
            _table_ready(conn)
            if promote:
                conn.execute("UPDATE scoring_weights SET promoted=FALSE WHERE promoted")
            conn.execute(
                "INSERT INTO scoring_weights"
                "(version,created_at,weights,coeffs,metrics,n_run_dates,promoted,notes)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
                (version, datetime.now().isoformat(timespec="seconds"),
                 db.j(bw), db.j(bc), db.j(metrics), len(dates), promote, notes))

    return {"calibrated": True, "promoted": promote, "version": version,
            "n_run_dates": len(dates), "weights": bw, "coeffs": bc, **metrics}
