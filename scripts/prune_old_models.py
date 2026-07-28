"""
既存の古いモデルバージョンのBYTEA本体を即座に整理する (Neon Storageクォータ対策)。

再学習のたびに自動実行される model.prune_old_model_blobs() と同じ処理だが、
次回の自動再学習を待たずに今すぐストレージを解放したい場合に手動実行する。
メタデータ(version/trained_at/metrics等)は全件残り、model_data(重い本体)だけ
直近N世代を除いて削除する。予測精度には影響しない(Predictorは常に最新版のみ参照)。

Usage: python scripts/prune_old_models.py [keep]
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _envload import load_env
load_env()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

keep = int(sys.argv[1]) if len(sys.argv) > 1 else 4

from surge_radar import db, model

with db.cursor() as c:
    before = c.execute(
        "SELECT COUNT(*) n, SUM(LENGTH(model_data)) bytes FROM model_meta WHERE model_data IS NOT NULL"
    ).fetchone()
print(f"整理前: model_data保持件数={before['n']} 合計サイズ={round((before['bytes'] or 0)/1024/1024, 2)}MB")

result = model.prune_old_model_blobs(keep=keep)
print(f"整理結果: kept={result['kept']} cleared={result['cleared']}")

with db.cursor() as c:
    after = c.execute(
        "SELECT COUNT(*) n, SUM(LENGTH(model_data)) bytes FROM model_meta WHERE model_data IS NOT NULL"
    ).fetchone()
print(f"整理後: model_data保持件数={after['n']} 合計サイズ={round((after['bytes'] or 0)/1024/1024, 2)}MB")
