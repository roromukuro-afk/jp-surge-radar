"""
過去日付の予測を生成して、学習データを一気に増やす。

価格データは510営業日ぶんあるのに、予測は36営業日ぶんしか作っていなかった
(うち満期は16日)。過去日付で予測を生成すれば、その先20営業日の値動きは既に
DBにあるので即座に満期判定でき、較正のホールドアウトが意味を持つ規模になる。

先読みについて:
  過去日の予測に使うモデルは、その日より後の結果を学習済みである。よって
  prob と similarity は未来を見た値になる。用途で影響が分かれる:
    - 教師データ(特徴量→結果)   : ラベルは実際の株価から来るので問題なし
    - chart/volatility/volume 等 : 価格由来なので問題なし
    - prob/similarity の重み較正 : 汚染される(楽観的に出る)
  そのため origin='backfill' で区別できるようにしてある(predict._is_backfill が
  2日以上過去の asof を自動で backfill と印付ける)。較正側で扱いを分けること。

材料は2026-08以降しか存在しないため use_materials=False で走らせる。
material の重みは現在 0.00 なので composite への影響はない。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from surge_radar import db, ingest, predict, track  # noqa: E402
from surge_radar.universe import get_target_codes  # noqa: E402


def trading_dates() -> list[str]:
    with db.cursor() as conn:
        return [r["date"] for r in conn.execute(
            "SELECT DISTINCT date FROM prices ORDER BY date").fetchall()]


def existing_run_dates() -> set[str]:
    with db.cursor() as conn:
        return {r["run_date"] for r in conn.execute(
            "SELECT DISTINCT run_date FROM predictions").fetchall()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=20, help="生成する営業日数")
    ap.add_argument("--step", type=int, default=5, help="何営業日おきに生成するか")
    ap.add_argument("--min-forward", type=int, default=21,
                    help="この営業日数ぶん先の価格が無い日は対象外(満期にできないため)")
    ap.add_argument("--track", action="store_true", help="生成後に判定まで走らせる")
    args = ap.parse_args()

    dates = trading_dates()
    done = existing_run_dates()
    # 満期にできる = 先に min_forward 日ぶんの価格がある日だけ
    eligible = dates[:-args.min_forward] if len(dates) > args.min_forward else []
    # 特徴量に260営業日ぶん使うので、序盤すぎる日は除く
    eligible = eligible[260:]
    targets = [d for d in eligible[::-1][::args.step] if d not in done][:args.count]
    targets.sort()

    print(f"価格 {len(dates)}営業日 / 生成済み {len(done)}日 / "
          f"対象候補 {len(eligible)}日 → 今回 {len(targets)}日", flush=True)
    if not targets:
        print("対象なし")
        return

    # 価格履歴は全期間を一度だけ読み、各日付へ使い回す。
    # predict.generate は asof でメモリ上スライスするので結果は同一。
    t_load = time.time()
    codes = get_target_codes()
    hist_map = ingest.load_history_bulk(codes)
    print(f"価格履歴を一括ロード: {len(hist_map)}銘柄 {time.time()-t_load:.0f}s", flush=True)

    ok = fail = 0
    t0 = time.time()
    for i, d in enumerate(targets, 1):
        t = time.time()
        try:
            r = predict.generate(run_date=d, asof=d, store=True, use_materials=False,
                                 hist_map=hist_map)
            ok += 1
            print(f"  [{i}/{len(targets)}] {d} stored={r['stored']} "
                  f"{r['categories']} {time.time()-t:.0f}s", flush=True)
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(targets)}] {d} FAILED {type(e).__name__}: {e}", flush=True)

    print(f"生成完了 ok={ok} fail={fail} 所要{(time.time()-t0)/60:.1f}分", flush=True)

    if args.track:
        print("判定を実行...", flush=True)
        res = track.track_all()
        print("track:", {k: v for k, v in res.items() if k != "results"}, flush=True)


if __name__ == "__main__":
    main()
