"""
ローカル専用: kabutan/みんかぶ材料の追加取得 (predict/train/trackには触れない)。

kabutan/みんかぶはGitHub Actionsのクラウド実行環境からのアクセスをBot対策で
ブロックしている(2026-08-25判明、surge_radar/materials.py の enrich_top_codes
docstring参照)。このマシン(ローカルPC)からは正常に取得できるため、この
スクリプトだけをWindowsタスクスケジューラで日次実行し、materials テーブルへの
書き込みのみ行う。predict/train/track(=predictionsテーブルへの書き込み)は
GitHub Actions の daily.yml 側だけが担当する(2026-09-02: ローカルとクラウドの
二重パイプラインが同じ本番DBへ競合書き込みしていた問題の是正、
scripts/run_daily.bat 参照)。

流れ:
  1. 当日分の価格を先に軽く更新(momentum pool 判定に当日の値動きが必要。
     クラウド daily.yml がまだ走っていないタイミングでも動くように)。
  2. momentum pool (値動き/出来高が立ち上がった銘柄) を安価に抽出。
  3. yahoojp/nikkei に加えて kabutan/みんかぶも含めて見出しを取得・保存。

実行例: .venv\\Scripts\\python.exe scripts\\materials_local_extra.py
"""
from __future__ import annotations

from datetime import datetime

from surge_radar import db, ingest, materials, predict, universe


def main() -> None:
    db.init_db()
    asof = datetime.now().strftime("%Y-%m-%d")
    print(f"[{datetime.now():%H:%M:%S}] materials_local_extra start asof={asof}", flush=True)

    codes = universe.get_target_codes()
    print(f"[{datetime.now():%H:%M:%S}] universe: {len(codes)} codes", flush=True)

    # 当日分の価格が未取得のままだと momentum pool が前日基準になってしまう
    # (クラウド daily.yml は不定期発火で、このタスクより後になることが多い)。
    # 差分取得なので既に新しい銘柄は即スキップされ、軽量。
    to_fetch = ingest.stale_codes(codes)
    if to_fetch:
        print(f"[{datetime.now():%H:%M:%S}] refreshing {len(to_fetch)} stale price codes (5d)...", flush=True)
        ingest.fetch_many(to_fetch, range_="5d", pause=0.25, log_every=200)

    priced = set(ingest.available_codes())
    codes = [c for c in codes if c in priced]
    hist_map = ingest.load_history_bulk(codes, lookback_days=450, as_of=asof)
    momentum_codes = predict._momentum_pool(hist_map, asof)
    print(f"[{datetime.now():%H:%M:%S}] momentum pool: {len(momentum_codes)} codes", flush=True)

    if not momentum_codes:
        print(f"[{datetime.now():%H:%M:%S}] nothing to enrich, exiting", flush=True)
        return

    res = materials.enrich_top_codes(momentum_codes, asof, max_codes=len(momentum_codes),
                                     include_blocked_sources=True)
    print(f"[{datetime.now():%H:%M:%S}] done: {res}", flush=True)


if __name__ == "__main__":
    main()
