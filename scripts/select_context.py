"""
候補選定のための材料を出す(procedures/select.md)。基準日より後の株価は一切出さない。

  --pool             入口条件に当てはまる銘柄の一覧
  --labels A,B       指定ラベルをすべて持つ銘柄の一覧
  --code 1234        1 銘柄の日足・ラベル・材料

基準日は --date で指定、省略時は最新のスナップショットの日付。
"""
from __future__ import annotations

import argparse
import json

import _boot  # noqa: F401

from surge_radar import db

ENTRY_LABELS = ["出来高急増", "ブレイク2週", "ブレイク1か月", "ストップ高引け", "ストップ高タッチ", "急騰"]
COLS = ["close", "ret_1d", "ret_5d", "vol_ratio", "dist_high_2w", "range_avg_2w", "turnover_avg_2w"]


def _base(conn, d: str | None) -> str:
    return d or conn.execute("SELECT MAX(date) d FROM snapshots").fetchone()["d"]


def _recent_dates(conn, bd: str, n: int) -> list[str]:
    return [r["date"] for r in conn.execute(
        "SELECT DISTINCT date FROM snapshots WHERE date <= %s ORDER BY date DESC LIMIT %s",
        (bd, n)).fetchall()]


def _fmt(f: dict) -> dict:
    out = {}
    for k in COLS:
        v = f.get(k)
        if v is None:
            out[k] = None
        elif k in ("close", "turnover_avg_2w"):
            out[k] = round(v) if k == "close" or v < 1e6 else f"{v/1e8:.2f}億"
        else:
            out[k] = round(v, 3)
    return out


def _positive_subjects(conn, since: str, until: str) -> dict[str, int]:
    rows = conn.execute(
        """SELECT n.code, COUNT(DISTINCT n.title_key) k FROM news n
           JOIN news_labels l ON l.title_key = n.title_key
           WHERE n.date BETWEEN %s AND %s AND l.direction >= 1 AND n.code = ANY(l.subjects)
           GROUP BY n.code""", (since, until)).fetchall()
    return {r["code"]: r["k"] for r in rows}


def listing(conn, bd: str, where_labels: list[str] | None, pool: bool) -> list[dict]:
    snaps = conn.execute(
        """SELECT s.code, s.features, s.labels, c.name FROM snapshots s
           LEFT JOIN securities c ON c.code = s.code WHERE s.date = %s ORDER BY s.code""",
        (bd,)).fetchall()
    dates = _recent_dates(conn, bd, 3)
    pos = _positive_subjects(conn, min(dates), bd) if pool else {}
    out = []
    for s in snaps:
        labels = s["labels"] or []
        if where_labels and not all(x in labels for x in where_labels):
            continue
        if pool and not (set(labels) & set(ENTRY_LABELS) or pos.get(s["code"])):
            continue
        out.append({"code": s["code"], "name": s["name"], **_fmt(s["features"]),
                    "labels": labels, "pos_news_3d": pos.get(s["code"], 0)})
    return out


def one(conn, bd: str, code: str) -> dict:
    name = conn.execute("SELECT name, market FROM securities WHERE code=%s", (code,)).fetchone()
    snap = conn.execute("SELECT features, labels FROM snapshots WHERE date=%s AND code=%s",
                        (bd, code)).fetchone()
    bars = conn.execute(
        "SELECT date, open, high, low, close, volume FROM prices WHERE code=%s AND date <= %s "
        "ORDER BY date", (code, bd)).fetchall()
    news = conn.execute(
        """SELECT n.date, n.source, n.title, l.subjects, l.kind, l.direction, l.scheduled
           FROM news n LEFT JOIN news_labels l ON l.title_key = n.title_key
           WHERE n.code = %s ORDER BY n.date DESC, n.id DESC""", (code,)).fetchall()
    for n in news:
        n["is_subject"] = (code in (n["subjects"] or [])) if n["kind"] else None
        n.pop("subjects", None)
    return {"base_date": bd, "code": code, "security": name,
            "features": snap["features"] if snap else None,
            "labels": snap["labels"] if snap else None,
            "bars": bars, "news": news}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pool", action="store_true")
    g.add_argument("--labels")
    g.add_argument("--code")
    a = ap.parse_args()
    with db.cursor() as conn:
        bd = _base(conn, a.date)
        if a.code:
            print(json.dumps(one(conn, bd, a.code), ensure_ascii=False, default=str))
            return
        rows = listing(conn, bd, a.labels.split(",") if a.labels else None, a.pool)
    # 一覧は表形式(タブ区切り)で出す。数百銘柄を JSON で出すと読む側の負担が大きい
    cols = ["code", "name", *COLS, "pos_news_3d", "labels"]
    print(f"# base_date={bd} count={len(rows)}")
    tab = "\t"
    print(tab.join(cols))
    for r in rows:
        print(tab.join("/".join(r[c]) if c == "labels" else ("" if r[c] is None else str(r[c]))
                       for c in cols))


if __name__ == "__main__":
    main()
