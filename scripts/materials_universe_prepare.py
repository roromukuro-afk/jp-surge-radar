"""
materials_fullscan.yml の matrix ジョブが銘柄一覧を毎回クエリし直していた無駄を
無くすため、対象母集団(3000円以下の全銘柄)を1回だけクエリしてファイルに書き出す。

以前は15並列シャードそれぞれが独立に
  SELECT DISTINCT ON (code) code, close FROM prices ORDER BY code, date DESC
を実行しており、prices テーブル全体を毎晩15回スキャンしていた
(2026-08-24、Neonデータ転送クォータ超過で本番500エラーの一因と判明)。
このスクリプトを1つの準備ジョブとして実行し、結果をartifactで各シャードに配る
ことで、クエリ回数を15回→1回に減らす。

Usage: python scripts/materials_universe_prepare.py <output_path>
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _envload import load_env
load_env()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from surge_radar import db
from surge_radar.config import PRICE_CAP

output_path = sys.argv[1]

db.init_db()

with db.cursor() as conn:
    rows = conn.execute(
        "SELECT DISTINCT ON (code) code, close FROM prices ORDER BY code, date DESC"
    ).fetchall()
universe = sorted(r["code"] for r in rows if r["close"] and 0 < r["close"] <= PRICE_CAP)

with open(output_path, "w", encoding="utf-8") as f:
    json.dump(universe, f)

print(f"universe: {len(universe)} codes -> {output_path}", flush=True)
