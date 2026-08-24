"""
3000円以下の全銘柄を並列ジョブ(GitHub Actions matrix)で分割して見出し材料を
完全スクリーニングする。

1ジョブ(=1シャード)で全銘柄を回すと実測ベースで長時間かかり、GitHub Actions
ホスト型ランナーの1ジョブ6時間上限を超えるおそれがある。そこで対象母集団を
shard_count個に分割し、matrixで並列実行することで、1ジョブあたりの処理量を
抑えたまま全銘柄を1晩で完全カバーする(2026-08-22、ユーザー指摘「材料を完全に
スクリーニングして」に対応)。

銘柄一覧は materials_universe_prepare.py が1回だけクエリしてartifact化した
JSONファイルを受け取る(--universe-file)。指定が無い場合のみ、このスクリプト
単体でも動くよう自前でクエリするフォールバックを残す(手動実行・デバッグ用。
2026-08-24: 以前はシャードごとに毎回このクエリを打っており、15並列で prices
テーブル全体を毎晩15回スキャンしていたことがNeonデータ転送クォータ超過の
一因と判明したため、通常経路ではファイル受け渡しに変更)。

Usage: python scripts/materials_shard_scan.py <shard_index> <shard_count> [asof] [universe_file]
"""
import json
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
asof = (sys.argv[3] if len(sys.argv) > 3 else "") or datetime.now().strftime("%Y-%m-%d")
universe_file = sys.argv[4] if len(sys.argv) > 4 and sys.argv[4] else None

db.init_db()

if universe_file:
    with open(universe_file, encoding="utf-8") as f:
        universe = json.load(f)
else:
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
