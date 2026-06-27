"""
日次 AI 急騰予測の生成。

対象(3000円以下・流動性・データ十分)を全件スコアリングし、総合評価でランキング化。
材料(DB) + テーマ地合い + ML確率 + 類似度 を統合。予測理由/失敗条件も保存。
"""
from __future__ import annotations

from datetime import datetime, timedelta

from . import db, features, ingest, materials, model, scoring, themes
from .config import PRICE_CAP, TOP_N_DEFAULT
from .universe import get_target_codes


def _is_backfill(asof: str) -> bool:
    """asof が今日から2日以上過去なら backfill と判定。"""
    try:
        return (datetime.now() - datetime.strptime(asof, "%Y-%m-%d")).days > 2
    except Exception:
        return False


def generate(run_date: str | None = None, *, store_top: int = TOP_N_DEFAULT,
             use_materials: bool = True, asof: str | None = None, on_progress=None) -> dict:
    """
    asof を指定すると、その日付(以前の直近営業日)時点で評価する過去バックフィル予測。
    run_date は予測の保存日(=T0)。指定なしは asof と同じ/本日。
    """
    asof = asof or run_date or datetime.now().strftime("%Y-%m-%d")
    run_date = run_date or asof
    origin = "backfill" if _is_backfill(asof) else "live"
    predictor = model.Predictor()
    market = themes.market_regime(run_date)
    market_score = market.get("score", 0.0)

    codes = get_target_codes()
    if not codes:
        codes = ingest.available_codes()

    # 価格データのある銘柄だけを評価対象にする。
    # universe 全体(~3500)を回すと no-price 銘柄ごとに無駄なDB往復が発生し
    # クラウドの daily 制限時間を超過する。priced は1クエリで取得。
    priced = set(ingest.available_codes())
    n_before = len(codes)
    codes = [c for c in codes if c in priced]
    print(f"    [predict] {len(codes)} priced codes (of {n_before} universe)", flush=True)

    # バルクプリロード: 銘柄ごとのDB往復を排除 (load_history/materials/sectors)。
    # これがないと daily(~3000銘柄)で1万回近いDB往復が発生しタイムアウトする。
    name_map = _names()
    sector_map = _sectors_map()
    hist_map = ingest.load_history_bulk(codes)
    mat_map = materials.recent_material_scores_bulk(codes, run_date) if use_materials else {}
    print(f"    [predict] preloaded: {len(hist_map)} histories, {len(mat_map)} material scores", flush=True)

    scored = []
    skipped = 0
    for i, code in enumerate(codes, 1):
        df = hist_map.get(code)
        if df is None or df.empty or len(df) < 60:
            skipped += 1
            continue
        # asof時点のインデックス(過去バックフィル対応)
        if asof:
            prior = df.index[df["date"] <= asof]
            if len(prior) == 0:
                skipped += 1
                continue
            idx = int(prior[-1])
        else:
            idx = len(df) - 1
        if idx < 60:
            skipped += 1
            continue
        close = float(df.iloc[idx]["close"])
        if close <= 0 or close > PRICE_CAP:
            skipped += 1
            continue

        mat = mat_map.get(code, materials._empty_material_score()) if use_materials else None
        sectors = sector_map.get(code, [])
        theme_tw, matched_themes = themes.theme_tailwind_for(sectors, (mat or {}).get("themes", []))

        feats = features.build_features(df, idx, material=mat,
                                        theme_tailwind=theme_tw, market_score=market_score)
        if feats is None:
            skipped += 1
            continue

        prob = predictor.predict_proba(feats)
        sim = predictor.similarity(feats)
        extra_info = {
            "top_category": (mat or {}).get("top_category", ""),
            "top_title": (mat or {}).get("top_title", ""),
            "themes_matched": matched_themes,
            "name": name_map.get(code, ""),
        }
        res = scoring.score_candidate(feats, ml_prob=prob, similarity=sim, extra_info=extra_info)
        res["_code"] = code
        res["_close"] = close
        res["_feats"] = feats
        res["_themes"] = matched_themes
        res["_mat"] = mat
        scored.append(res)
        if on_progress and i % 200 == 0:
            on_progress(i, len(codes), len(scored))

    # ランキング: 除外(E/ゲート)を下げ、composite降順
    scored.sort(key=lambda r: (r["category"] == "E", -r["score"]))

    # 保存: 全評価銘柄 (A/B/C/D/E全対象銘柄分類要件)
    _clear_today(run_date)
    stored = 0
    with db.cursor() as conn:
        for rank, r in enumerate(scored[:store_top], 1):
            f = r["_feats"]
            mat = r.get("_mat") or {}
            top_mat = mat.get("top_category", "")
            conn.execute(
                """INSERT INTO predictions
                   (run_date,code,name,base_price,rank,score,probability,category,
                    material_score,chart_score,volume_score,theme_score,fundamental_score,
                    similarity_score,reasons,failure_conditions,features,flags,model_version,
                    status,origin,top_material)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s)""",
                (run_date, r["_code"], name_map.get(r["_code"], ""), r["_close"], rank,
                 r["score"], r["probability"], r["category"],
                 r["sub"]["material"], r["sub"]["chart"], r["sub"]["volume"], r["sub"]["theme"],
                 r["sub"]["fundamental"], r["sub"]["similarity"],
                 db.j(r["reasons"]), db.j(r["failure_conditions"]),
                 db.j({k: v for k, v in f.items() if not k.startswith("_")}),
                 db.j({"gates": r["gates"], "themes": r["_themes"], "top_driver": r["top_driver"],
                       "upside": r["upside"], "market_score": market_score, "sub": r["sub"],
                       "classify_path": r.get("classify_path", "")}),
                 predictor.version or "rules", origin, top_mat),
            )
            stored += 1

    cat_counts = {}
    for r in scored[:store_top]:
        cat_counts[r["category"]] = cat_counts.get(r["category"], 0) + 1
    return {"run_date": run_date, "evaluated": len(scored), "skipped": skipped,
            "stored": stored, "model_version": predictor.version or "rules",
            "categories": cat_counts, "market_score": market_score}


def _sectors(code: str) -> list[str]:
    with db.cursor() as conn:
        r = conn.execute("SELECT sector33,sector17 FROM securities WHERE code=%s", (code,)).fetchone()
    if not r:
        return []
    return [x for x in [r["sector33"], r["sector17"]] if x]


def _sectors_map() -> dict[str, list[str]]:
    """全銘柄の業種を1クエリで取得。predict のループ用。"""
    with db.cursor() as conn:
        rows = conn.execute("SELECT code,sector33,sector17 FROM securities").fetchall()
    return {r["code"]: [x for x in [r["sector33"], r["sector17"]] if x] for r in rows}


def _names() -> dict:
    with db.cursor() as conn:
        rows = conn.execute("SELECT code,name FROM securities").fetchall()
    return {r["code"]: r["name"] for r in rows}


def _clear_today(run_date: str) -> None:
    with db.cursor() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM predictions WHERE run_date=%s", (run_date,)).fetchall()]
        if ids:
            qs = ",".join("%s" for _ in ids)
            conn.execute(f"DELETE FROM prediction_outcomes WHERE prediction_id IN ({qs})", ids)
            conn.execute("DELETE FROM predictions WHERE run_date=%s", (run_date,))
