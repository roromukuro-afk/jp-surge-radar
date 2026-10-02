"""
未処理の見出しを、同じ見出しを 1 件にまとめて出力する(Claude の材料ラベル付け用。procedures/label.md)。

Material Window 内(基準日の終値の時刻以降に公開)の見出しを先に出す。次に基準日で公開時刻が不明なもの、
最後にそれより前のもの。各グループ内は新しい日付から。

株価・候補・成否は出さない(ラベルが値動きに引きずられないように)。

--oldest-first は逆順(古い背景から)。日次の実行(新しい順)と同時に動いても同じ見出しを取り合わないため、
背景のラベル付けタスクが使う(2026-10-02)。

使い方: python scripts/news_to_label.py [--limit 200] [--count] [--oldest-first]
"""
from __future__ import annotations

import argparse
import json

import _boot  # noqa: F401

from surge_radar import db
from surge_radar.vocab import window_start


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--count", action="store_true", help="未処理の件数だけ出す")
    ap.add_argument("--oldest-first", action="store_true", help="古い背景から出す(背景のラベル付けタスク用)")
    a = ap.parse_args()

    with db.cursor() as conn:
        bd = conn.execute("SELECT MAX(date) d FROM snapshots").fetchone()["d"]
        start = window_start(bd)
        # 優先度: 0 = Material Window 内(基準日の終値の時刻以降に公開)、1 = 基準日だが公開時刻不明、2 = それ以前
        prio = """CASE WHEN MAX(n.published_at) >= %(start)s OR MAX(n.date) > %(bd)s THEN 0
                       WHEN MAX(n.date) = %(bd)s AND MAX(n.published_at) IS NULL THEN 1 ELSE 2 END"""
        if a.count:
            rows = conn.execute(
                f"""SELECT p, COUNT(*) n FROM (
                      SELECT n.title_key, {prio} p FROM news n
                      LEFT JOIN news_reviews v ON v.title_key = n.title_key
                      WHERE v.title_key IS NULL GROUP BY n.title_key) t GROUP BY p""",
                {"start": start, "bd": bd}).fetchall()
            by = {r["p"]: r["n"] for r in rows}
            print(json.dumps({"unreviewed_titles": sum(by.values()),
                              "in_material_window": by.get(0, 0), "base_date_time_unknown": by.get(1, 0),
                              "older_background": by.get(2, 0)}, ensure_ascii=False))
            return
        if a.oldest_first:
            order, order_out = "p DESC, d ASC, n.title_key", "p.p DESC, p.d ASC, p.title_key"
        else:
            order, order_out = "p, d DESC, n.title_key", "p.p, p.d DESC, p.title_key"
        rows = conn.execute(
            f"""WITH pending AS (
                 SELECT n.title_key, MAX(n.date) d, {prio} p FROM news n
                 LEFT JOIN news_reviews v ON v.title_key = n.title_key
                 WHERE v.title_key IS NULL
                 GROUP BY n.title_key ORDER BY {order} LIMIT %(limit)s)
               SELECT p.title_key, p.d, p.p, n.title, n.source, n.code, s.name
               FROM pending p
               JOIN news n ON n.title_key = p.title_key
               LEFT JOIN securities s ON s.code = n.code
               ORDER BY {order_out}""",
            {"start": start, "bd": bd, "limit": a.limit}).fetchall()

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
