"""
未ラベルの見出しを、同じ見出しを 1 件にまとめて出力する(Claude のラベル付け用)。

株価・候補・成否は出さない(procedures/label.md: ラベルが値動きに引きずられないように)。

使い方: python scripts/news_to_label.py [--limit 300] [--count]
"""
from __future__ import annotations

import argparse
import json

import _boot  # noqa: F401

from surge_radar import db


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--count", action="store_true", help="未ラベルの件数だけ出す")
    a = ap.parse_args()

    with db.cursor() as conn:
        if a.count:
            r = conn.execute(
                """SELECT COUNT(DISTINCT n.title_key) n FROM news n
                   LEFT JOIN news_labels l ON l.title_key = n.title_key
                   WHERE l.title_key IS NULL""").fetchone()
            print(json.dumps({"unlabeled_titles": r["n"]}))
            return
        rows = conn.execute(
            """WITH pending AS (
                 SELECT n.title_key, MAX(n.date) d FROM news n
                 LEFT JOIN news_labels l ON l.title_key = n.title_key
                 WHERE l.title_key IS NULL
                 GROUP BY n.title_key ORDER BY d DESC, n.title_key LIMIT %s)
               SELECT p.title_key, p.d, n.title, n.source, n.code, s.name
               FROM pending p
               JOIN news n ON n.title_key = p.title_key
               LEFT JOIN securities s ON s.code = n.code
               ORDER BY p.d DESC, p.title_key""", (a.limit,)).fetchall()

    items: dict[str, dict] = {}
    for r in rows:
        it = items.setdefault(r["title_key"], {
            "title_key": r["title_key"], "title": r["title"], "date": r["d"],
            "sources": set(), "linked": {}})
        it["sources"].add(r["source"])
        it["linked"][r["code"]] = r["name"] or ""
    out = [{**it, "sources": sorted(it["sources"])} for it in items.values()]
    # 1 見出し 1 行(読む側のトークンを節約する)
    lines = [json.dumps(x, ensure_ascii=False) for x in out]
    print("[")
    print(",\n".join(lines))
    print("]")


if __name__ == "__main__":
    main()
