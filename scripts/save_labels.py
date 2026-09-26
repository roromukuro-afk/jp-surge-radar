"""
Claude が付けた材料ラベルを保存する。既にラベルがある見出しは上書きしない(付け直さない)。

使い方: python scripts/save_labels.py <labels.json> [--model claude-...]
"""
from __future__ import annotations

import argparse
import json
import sys

import _boot  # noqa: F401

from surge_radar import db

PROCEDURE = "label-v1"
KINDS = {"決算", "業績修正", "受注・契約", "提携・M&A", "TOB・MBO", "資本政策", "株主還元",
         "新製品・新事業", "許認可・規制", "人事・組織", "不祥事・訴訟", "値動き解説",
         "まとめ・市況", "その他"}
SCHEDULED = {"yes", "no", "unknown"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--model", default="")
    a = ap.parse_args()
    items = json.load(open(a.path, encoding="utf-8"))

    with db.cursor() as conn:
        linked: dict[str, set[str]] = {}
        keys = [it["title_key"] for it in items]
        for r in conn.execute("SELECT title_key, code FROM news WHERE title_key = ANY(%s)",
                              (keys,)).fetchall():
            linked.setdefault(r["title_key"], set()).add(r["code"])

    errors, rows = [], []
    for it in items:
        k = it.get("title_key")
        if k not in linked:
            errors.append(f"未知の title_key: {k!r}")
            continue
        subj = [str(s) for s in it.get("subjects") or []]
        bad = [s for s in subj if s not in linked[k]]
        if bad:
            errors.append(f"{k[:30]}: 紐付いていない銘柄を主語にしている {bad}")
            continue
        if it.get("kind") not in KINDS:
            errors.append(f"{k[:30]}: kind が不正 {it.get('kind')!r}")
            continue
        d = it.get("direction")
        if d not in (-2, -1, 0, 1, 2):
            errors.append(f"{k[:30]}: direction が不正 {d!r}")
            continue
        if it.get("scheduled") not in SCHEDULED:
            errors.append(f"{k[:30]}: scheduled が不正 {it.get('scheduled')!r}")
            continue
        rows.append((k, subj, it["kind"], d, it["scheduled"], PROCEDURE, a.model))

    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(f"{len(errors)} 件にエラー。何も保存していない。直して再実行すること")

    with db.cursor() as conn:
        before = conn.execute("SELECT COUNT(*) n FROM news_labels").fetchone()["n"]
        conn.executemany(
            """INSERT INTO news_labels(title_key,subjects,kind,direction,scheduled,procedure,model)
               VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(title_key) DO NOTHING""", rows)
        after = conn.execute("SELECT COUNT(*) n FROM news_labels").fetchone()["n"]
    print(json.dumps({"saved": after - before, "skipped_existing": len(rows) - (after - before)}))


if __name__ == "__main__":
    main()
