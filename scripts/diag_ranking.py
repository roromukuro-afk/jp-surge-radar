"""Verify the latest full prediction ranking. No secrets printed.

Usage: python scripts/diag_ranking.py
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _envload import load_env
load_env()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from surge_radar import db


def loadj(s, fb):
    try:
        return json.loads(s) if s else fb
    except Exception:
        return fb


with db.cursor() as conn:
    def rows(q, *a):
        return conn.execute(q, a).fetchall()
    def one(q, *a):
        return conn.execute(q, a).fetchone()

    rd = one("SELECT MAX(run_date) d FROM predictions")["d"]
    print(f"=== latest run_date: {rd} ===")
    tot = one("SELECT COUNT(*) n FROM predictions WHERE run_date=%s", rd)["n"]
    print(f"stored predictions: {tot}")

    print("=== category distribution ===")
    for r in rows("SELECT category, COUNT(*) n FROM predictions WHERE run_date=%s GROUP BY category ORDER BY category", rd):
        print(f"  {r['category']}: {r['n']}")

    print("=== material coverage ===")
    mat = one("SELECT COUNT(*) n FROM predictions WHERE run_date=%s AND material_score>0", rd)["n"]
    print(f"  material_score>0: {mat}/{tot}")
    # material_quality from flags
    allp = rows("SELECT category,material_score,similarity_score,flags FROM predictions WHERE run_date=%s", rd)
    mq0 = mq3 = bc_total = bc_mat = bc_nomat = bc_simonly = trifecta = 0
    for r in allp:
        f = loadj(r["flags"], {})
        mqual = f.get("material_quality", 0) or 0
        if mqual > 0:
            mq0 += 1
        if mqual > 0.3:
            mq3 += 1
        if r["category"] in ("B", "C"):
            bc_total += 1
            if (r["material_score"] or 0) > 0.05:
                bc_mat += 1
            else:
                bc_nomat += 1
            if (r["material_score"] or 0) <= 0.05 and (r["similarity_score"] or 0) >= 0.9:
                bc_simonly += 1
        sub = f.get("sub", {})
        if (r["material_score"] or 0) > 0.05 and (sub.get("chart", 0) or 0) > 0.3 and (sub.get("volume", 0) or 0) > 0.3:
            trifecta += 1
    print(f"  material_quality>0: {mq0}   >0.3: {mq3}")
    print(f"  B/C total: {bc_total}  | material-backed: {bc_mat}  | no-material: {bc_nomat}  | AI-similarity-only: {bc_simonly}")
    print(f"  material+chart+volume all present: {trifecta}")

    print("=== classify_path distribution (from flags) ===")
    path_counts = {}
    for r in rows("SELECT flags FROM predictions WHERE run_date=%s", rd):
        f = loadj(r["flags"], {})
        p = f.get("classify_path", "(none)")
        path_counts[p] = path_counts.get(p, 0) + 1
    for p, n in sorted(path_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {p}: {n}")

    print("=== TOP 10 candidates (excluding E) ===")
    top = rows(
        "SELECT code,name,category,score,probability,base_price,material_score,chart_score,"
        "volume_score,similarity_score,reasons,failure_conditions,top_material "
        "FROM predictions WHERE run_date=%s AND category<>'E' ORDER BY rank ASC LIMIT 10", rd)
    if not top:
        top = rows("SELECT code,name,category,score,probability,base_price,material_score,chart_score,"
                   "volume_score,similarity_score,reasons,failure_conditions,top_material "
                   "FROM predictions WHERE run_date=%s ORDER BY rank ASC LIMIT 10", rd)
    for i, r in enumerate(top, 1):
        bp = r["base_price"] or 0
        tgt = round(bp * 1.2, 1)
        print(f"\n#{i} {r['code']} {str(r['name'])[:18]} [{r['category']}] score={r['score']:.3f} prob={r['probability']:.3f}")
        print(f"   base={bp} +20%target={tgt} mat={r['material_score']:.2f} chart={r['chart_score']:.2f} vol={r['volume_score']:.2f} sim={r['similarity_score']:.2f}")
        reasons = loadj(r["reasons"], [])
        for rs in reasons[:3]:
            print(f"   reason: {rs}")
        fc = loadj(r["failure_conditions"], [])
        for f in fc[:2]:
            print(f"   fail-if: {f}")
        if r["top_material"]:
            print(f"   material: {str(r['top_material'])[:80]}")
