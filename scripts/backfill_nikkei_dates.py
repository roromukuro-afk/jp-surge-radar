"""
日経材料の日付を実公開日に補正する一回限りのバックフィル。

2026-09-17まで fetch_nikkei_news() は全見出しを「取得日」で保存していたため
(materials.py の _parse_nikkei_date 追加で修正済み)、既存行の date は
「その記事を初めてスクレイプした日」になっている。日経の銘柄別ニュース一覧を
再取得し、一覧に残っている記事については実際の日付に更新する。

一覧から落ちた古い記事は補正できないため、そのまま残す(=日付は実際より新しい
ままになる)。その分は鮮度を過大評価する方向の誤差として残る。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from surge_radar import db, materials  # noqa: E402


def main(pause: float = 0.4, limit: int | None = None) -> None:
    conn = db.connect()
    codes = [r["code"] for r in conn.execute(
        "SELECT DISTINCT code FROM materials WHERE source='nikkei' ORDER BY code").fetchall()]
    if limit:
        codes = codes[:limit]
    print(f"targets: {len(codes)} codes", flush=True)

    checked = updated = unmatched = 0
    for i, code in enumerate(codes, 1):
        items = materials.fetch_nikkei_news(code, max_items=25)
        if items:
            real = {it["title"]: it["date"] for it in items if it.get("title") and it.get("date")}
            rows = conn.execute(
                "SELECT id, title, date FROM materials WHERE code=%s AND source='nikkei'",
                (code,)).fetchall()
            for r in rows:
                checked += 1
                d = real.get(r["title"])
                if d is None:
                    unmatched += 1
                elif d != r["date"]:
                    conn.execute("UPDATE materials SET date=%s WHERE id=%s", (d, r["id"]))
                    updated += 1
            conn.commit()
        if i % 100 == 0:
            print(f"  {i}/{len(codes)} checked={checked} updated={updated} "
                  f"unmatched={unmatched}", flush=True)
        time.sleep(pause)

    print(f"DONE codes={len(codes)} checked={checked} updated={updated} unmatched={unmatched}",
          flush=True)


if __name__ == "__main__":
    main(limit=int(sys.argv[1]) if len(sys.argv) > 1 else None)
