"""
コマンドラインインターフェース。

  python -m surge_radar.cli init-db
  python -m surge_radar.cli universe
  python -m surge_radar.cli ingest --limit 200 [--range 2y]
  python -m surge_radar.cli materials --limit 200
  python -m surge_radar.cli themes
  python -m surge_radar.cli seed-teacher        # 過去データから教師生成
  python -m surge_radar.cli train               # 再学習
  python -m surge_radar.cli predict             # ランキング生成
  python -m surge_radar.cli track               # 成否追跡
  python -m surge_radar.cli daily --limit 300   # 日次フル
  python -m surge_radar.cli serve               # Webサーバ
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime


def main():
    ap = argparse.ArgumentParser(prog="surge_radar")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")
    sub.add_parser("universe")

    p = sub.add_parser("ingest"); p.add_argument("--limit", type=int); p.add_argument("--range", default="2y"); p.add_argument("--pause", type=float, default=0.25)
    p = sub.add_parser("materials"); p.add_argument("--limit", type=int); p.add_argument("--pause", type=float, default=0.2)
    sub.add_parser("themes")
    p = sub.add_parser("seed-teacher"); p.add_argument("--step", type=int, default=10); p.add_argument("--max-per-code", type=int, default=8)
    p = sub.add_parser("train")
    p = sub.add_parser("predict"); p.add_argument("--asof"); p.add_argument("--run-date")
    sub.add_parser("track")
    p = sub.add_parser("daily"); p.add_argument("--limit", type=int); p.add_argument("--range", default="2y"); p.add_argument("--skip-materials", action="store_true"); p.add_argument("--no-retrain", action="store_true")
    p = sub.add_parser("serve"); p.add_argument("--host", default="127.0.0.1"); p.add_argument("--port", type=int, default=8000); p.add_argument("--reload", action="store_true")
    sub.add_parser("stats")

    a = ap.parse_args()
    from . import db
    db.init_db()
    asof = datetime.now().strftime("%Y-%m-%d")

    if a.cmd == "init-db":
        print("DB ready")
    elif a.cmd == "universe":
        from . import universe
        rows = universe.load_universe(); n = universe.save_universe(rows)
        print(f"universe saved: {n}")
    elif a.cmd == "ingest":
        from . import ingest, universe
        codes = universe.get_target_codes()
        if a.limit: codes = codes[:a.limit]
        print(f"ingesting {len(codes)} codes...")
        res = ingest.fetch_many(codes, range_=a.range, pause=a.pause,
                                on_progress=lambda i,t,o,f: print(f"  {i}/{t} ok={o} fail={f}"))
        print(json.dumps(res, ensure_ascii=False))
    elif a.cmd == "materials":
        from . import pipeline, universe
        codes = universe.get_target_codes()
        if a.limit: codes = codes[:a.limit]
        print(json.dumps(pipeline.collect_materials_step(codes, a.pause), ensure_ascii=False))
    elif a.cmd == "themes":
        from . import themes
        print(json.dumps(themes.update_theme_regime(asof), ensure_ascii=False, indent=2))
    elif a.cmd == "seed-teacher":
        from . import teacher, ingest
        codes = ingest.available_codes()
        print(f"building teacher from {len(codes)} codes...")
        res = teacher.build_historical(codes, step=a.step, max_per_code=a.max_per_code,
                                       on_progress=lambda i,t,p,n: print(f"  {i}/{t} pos={p} neg={n}"))
        print(json.dumps(res, ensure_ascii=False))
        print("counts:", json.dumps(teacher.counts(), ensure_ascii=False))
    elif a.cmd == "train":
        from . import train
        train.ensure_historical()
        print(json.dumps(train.retrain(f"cli {asof}"), ensure_ascii=False, indent=2, default=str))
    elif a.cmd == "predict":
        from . import predict
        print(json.dumps(predict.generate(run_date=a.run_date, asof=a.asof),
                         ensure_ascii=False, indent=2))
    elif a.cmd == "track":
        from . import track
        print(json.dumps(track.track_all(asof), ensure_ascii=False, indent=2))
    elif a.cmd == "daily":
        from . import pipeline
        out = pipeline.run_daily(limit=a.limit, price_range=a.range,
                                 skip_materials=a.skip_materials, retrain_if_needed=not a.no_retrain)
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    elif a.cmd == "stats":
        from . import track, teacher, model
        print(json.dumps({"accuracy": track.accuracy_stats(), "teacher": teacher.counts(),
                          "model": (model.latest_meta() or {}).get("version")},
                         ensure_ascii=False, indent=2))
    elif a.cmd == "serve":
        import uvicorn
        uvicorn.run("surge_radar.web.app:app", host=a.host, port=a.port, reload=a.reload)


if __name__ == "__main__":
    main()
