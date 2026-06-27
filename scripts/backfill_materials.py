"""
既存材料に品質分析フィールドを後付けする (再実行可能・冪等)。

- material_type / unpriced / connection / risk / ai_comment を analyze() で付与
- impact / persistence / sentiment が0の見出し材料も最低限スコア化
- chart_reaction / volume_reaction / 出尽くしリスク を prices から事後計算
  (価格データが無い銘柄はreaction=0のまま。bootstrap完了後に再実行で埋まる)

Usage: python scripts/backfill_materials.py
"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _envload import load_env
load_env()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from surge_radar import db, ingest, materials_analysis as ma

db.init_db()  # スキーマ移行 (新カラム追加)

t0 = time.monotonic()
with db.cursor() as conn:
    mats = conn.execute(
        "SELECT id,code,date,source,category,title FROM materials ORDER BY code"
    ).fetchall()
print(f"materials to backfill: {len(mats)}", flush=True)

codes = sorted({m["code"] for m in mats if m["code"]})
print(f"loading prices for {len(codes)} codes...", flush=True)
hist = ingest.load_history_bulk(codes)
print(f"  price histories available: {len(hist)}", flush=True)

updates = []
reacted = 0
for m in mats:
    a = ma.analyze(m["title"] or "", source=m["source"] or "", code=m["code"] or "",
                   fallback_category=m["category"] or "")
    df = hist.get(m["code"])
    prices = None
    if df is not None and not df.empty:
        prices = df.to_dict("records")
    reactions = ma.compute_reactions(prices, m["date"]) if prices else {
        "chart_reaction": 0.0, "volume_reaction": 0.0, "exhaust_risk": 0.0, "reaction_known": 0}
    if reactions["reaction_known"]:
        reacted += 1
    risk = max(a["dilution_risk"], reactions["exhaust_risk"])
    ai_comment = ma.make_ai_comment(a, reactions)
    updates.append((
        a["material_type"], a["unpriced"], a["connection"], a["impact"],
        a["persistence"], a["sentiment"], reactions["chart_reaction"],
        reactions["volume_reaction"], round(risk, 3), ai_comment, m["id"],
    ))

print(f"computed {len(updates)} updates ({reacted} with price reaction); writing...", flush=True)
SQL = ("UPDATE materials SET material_type=%s, unpriced=%s, connect=%s, impact=%s, "
       "persistence=%s, sentiment=%s, chart_reaction=%s, volume_reaction=%s, "
       "risk=%s, ai_comment=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s")
B = 500
for i in range(0, len(updates), B):
    db.executemany(SQL, updates[i:i + B])
    print(f"  written {min(i+B, len(updates))}/{len(updates)}", flush=True)

print(f"backfill done in {time.monotonic()-t0:.0f}s", flush=True)

# サマリ
with db.cursor() as conn:
    def one(q):
        return conn.execute(q).fetchone()["n"]
    print("typed:", one("SELECT COUNT(*) n FROM materials WHERE material_type<>''"))
    print("unpriced>0:", one("SELECT COUNT(*) n FROM materials WHERE unpriced>0"))
    print("connect>0:", one("SELECT COUNT(*) n FROM materials WHERE connect>0"))
    print("chart_reaction>0:", one("SELECT COUNT(*) n FROM materials WHERE chart_reaction>0"))
    print("volume_reaction>0:", one("SELECT COUNT(*) n FROM materials WHERE volume_reaction>0"))
    print("risk>0:", one("SELECT COUNT(*) n FROM materials WHERE risk>0"))
    print("ai_comment set:", one("SELECT COUNT(*) n FROM materials WHERE ai_comment<>''"))
