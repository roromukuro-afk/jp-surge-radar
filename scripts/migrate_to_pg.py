"""
SQLite → PostgreSQL データ移行スクリプト。

DATABASE_URL 環境変数を設定した状態で実行すること:
  $env:DATABASE_URL = "postgresql://postgres:..."
  python scripts/migrate_to_pg.py

テーブル順 (外部キー制約を考慮):
  securities → prices → materials → theme_regime →
  predictions → prediction_outcomes →
  teacher_samples → model_meta → job_logs
"""
from __future__ import annotations

import json
import sqlite3
import sys
import os

# プロジェクトルートを sys.path に追加
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from surge_radar.config import DB_PATH

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL 環境変数が設定されていません")
    sys.exit(1)

import psycopg2
import psycopg2.extras

BATCH = 500  # 一度に INSERT するバッチサイズ


def get_sqlite() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_pg() -> psycopg2.extensions.connection:
    return psycopg2.connect(DATABASE_URL)


def copy_table(pg_conn, sqlite_conn, table: str, columns: list[str],
               pg_columns: list[str] | None = None, batch: int = BATCH) -> int:
    """SQLite テーブルを PostgreSQL にコピー。返り値は挿入行数。"""
    pg_cols = pg_columns or columns
    cur_lite = sqlite_conn.cursor()
    cur_lite.execute(f"SELECT {','.join(columns)} FROM {table}")
    cur_pg = pg_conn.cursor()
    ph = ",".join(["%s"] * len(pg_cols))
    sql = f"INSERT INTO {table}({','.join(pg_cols)}) VALUES({ph}) ON CONFLICT DO NOTHING"
    total = 0
    while True:
        rows = cur_lite.fetchmany(batch)
        if not rows:
            break
        data = [tuple(r[c] for c in columns) for r in rows]
        psycopg2.extras.execute_batch(cur_pg, sql, data)
        total += len(rows)
        print(f"  {table}: {total} rows...", end="\r")
    pg_conn.commit()
    print(f"  {table}: {total} rows OK          ")
    return total


def main():
    print(f"SOURCE: {DB_PATH}")
    print(f"TARGET: {DATABASE_URL[:40]}...")
    print()

    sqlite_conn = get_sqlite()
    pg_conn = get_pg()

    # スキーマ初期化 (テーブル・インデックスを作成)
    print("[0] PG スキーマ初期化...")
    from surge_radar import db as surge_db
    surge_db.init_db()
    print("    OK")

    # 各テーブルを順番にコピー
    tables = [
        ("securities", ["code","name","market","sector33","sector17","shares_out","listed_date"]),
        ("prices", ["code","date","open","high","low","close","volume","turnover"]),
        ("materials", ["code","date","source","category","title","url","body",
                       "sentiment","impact","persistence","unpriced","connect","raw"]),
        ("theme_regime", ["date","theme","trend","above_ma","vol_up","note"]),
        ("predictions", ["run_date","code","name","base_price","rank","score","probability",
                         "category","material_score","chart_score","volume_score","theme_score",
                         "fundamental_score","similarity_score","reasons","failure_conditions",
                         "features","flags","model_version","status","origin","top_material"]),
        ("prediction_outcomes", ["prediction_id","judged_date","bars_tracked","max_up_5d",
                                  "max_up_10d","max_up_20d","days_to_20pct","max_drawdown",
                                  "close_up_maintained","faded_after_high","material_continued",
                                  "volume_continued","result_class","failure_tags","notes","next_learning"]),
        ("teacher_samples", ["source","code","t0_date","label","features","tags","prediction_id"]),
        ("model_meta", ["version","trained_at","n_samples","n_pos","n_neg",
                        "metrics","feature_importance","notes"]),
        ("job_logs", ["job","started_at","finished_at","status","counts","message"]),
        ("push_subscriptions", ["endpoint","p256dh","auth","user_agent"]),
    ]

    total_rows = 0
    for table, columns in tables:
        print(f"[{table}]")
        try:
            n = copy_table(pg_conn, sqlite_conn, table, columns)
            total_rows += n
        except Exception as e:
            print(f"  ERROR: {e}")
            pg_conn.rollback()

    # モデルファイルを DB に保存
    print("[model_data] モデルファイルを PG にアップロード...")
    try:
        from surge_radar.config import MODEL_DIR
        cur_lite = sqlite_conn.execute("SELECT version FROM model_meta ORDER BY trained_at DESC")
        cur_pg = pg_conn.cursor()
        for row in cur_lite.fetchall():
            v = row[0]
            path = MODEL_DIR / f"model_{v}.joblib"
            if path.exists():
                with open(path, "rb") as f:
                    data = f.read()
                cur_pg.execute("UPDATE model_meta SET model_data=%s WHERE version=%s",
                               (psycopg2.Binary(data), v))
                print(f"  uploaded model_{v}.joblib ({len(data)//1024}KB)")
        pg_conn.commit()
    except Exception as e:
        print(f"  model_data upload error: {e}")
        pg_conn.rollback()

    sqlite_conn.close()
    pg_conn.close()

    print(f"\n完了: 合計 {total_rows:,} 行を PostgreSQL に移行しました。")
    print("次のステップ: Render にデプロイして DATABASE_URL を設定する")


if __name__ == "__main__":
    main()
