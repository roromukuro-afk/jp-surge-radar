"""
3000円以下の全銘柄を並列ジョブ(GitHub Actions matrix)で分割して見出し材料を
完全スクリーニングする。

1ジョブ(=1シャード)で全銘柄を回すと実測ベースで長時間かかり、GitHub Actions
ホスト型ランナーの1ジョブ6時間上限を超えるおそれがある。そこで対象母集団を
shard_count個に分割し、matrixで並列実行することで、1ジョブあたりの処理量を
抑えたまま全銘柄を1晩で完全カバーする(2026-08-22、ユーザー指摘「材料を完全に
スクリーニングして」に対応)。

Usage: python scripts/materials_shard_scan.py <shard_index> <shard_count> [asof]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _envload import load_env
load_env()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime

from surge_radar import db, materials
from surge_radar.config import PRICE_CAP

shard_index = int(sys.argv[1])
shard_count = int(sys.argv[2])
asof = sys.argv[3] if len(sys.argv) > 3 else datetime.now().strftime("%Y-%m-%d")

db.init_db()

with db.cursor() as conn:
    rows = conn.execute(
        "SELECT DISTINCT ON (code) code, close FROM prices ORDER BY code, date DESC"
    ).fetchall()
universe = sorted(r["code"] for r in rows if r["close"] and 0 < r["close"] <= PRICE_CAP)
shard_codes = [c for i, c in enumerate(universe) if i % shard_count == shard_index]

print(f"shard {shard_index}/{shard_count}: {len(shard_codes)} codes "
      f"(of {len(universe)} universe, asof={asof})", flush=True)

result = materials.enrich_top_codes(shard_codes, asof, max_codes=len(shard_codes))
print(f"shard {shard_index}/{shard_count} done: {result}", flush=True)
