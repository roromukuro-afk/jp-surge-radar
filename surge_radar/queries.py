"""サイト表示用の読み取りクエリ。"""
from __future__ import annotations

import unicodedata

from . import db

_CAND_SQL = """
SELECT c.id, c.base_date, c.code, s.name, s.market, c.base_close, c.target, c.rank,
       c.conviction, c.thesis, c.trigger, c.risk, c.chart_view, c.labels, c.procedure,
       o.bars_tracked, o.max_high, o.max_ret, o.min_ret, o.hit, o.hit_day, o.final, o.last_date
FROM candidates c
LEFT JOIN securities s ON s.code = c.code
LEFT JOIN outcomes o ON o.candidate_id = c.id
"""


def _status(r: dict) -> str:
    # JPX の社名は全角英数字(例 "Ｖｅｒｉｔａｓ　Ｉｎ")なので表示用に半角へ寄せる
    if r.get("name"):
        r["name"] = unicodedata.normalize("NFKC", r["name"])
    if r.get("hit"):
        return "hit"
    if r.get("final"):
        return "miss"
    return "open"


def selection_dates() -> list[str]:
    with db.cursor() as conn:
        return [r["base_date"] for r in conn.execute(
            "SELECT base_date FROM selection_runs ORDER BY base_date DESC").fetchall()]


def selection(base_date: str) -> dict | None:
    with db.cursor() as conn:
        run = conn.execute("SELECT * FROM selection_runs WHERE base_date=%s", (base_date,)).fetchone()
        if not run:
            return None
        rows = conn.execute(_CAND_SQL + " WHERE c.base_date=%s ORDER BY c.rank", (base_date,)).fetchall()
    for r in rows:
        r["status"] = _status(r)
    return {"run": run, "candidates": rows}


def history() -> dict:
    with db.cursor() as conn:
        rows = conn.execute(_CAND_SQL + " ORDER BY c.base_date DESC, c.rank").fetchall()
    for r in rows:
        r["status"] = _status(r)
    decided = [r for r in rows if r["status"] != "open"]
    hits = sum(1 for r in decided if r["status"] == "hit")
    return {"rows": rows, "decided": len(decided), "hits": hits,
            "open": len(rows) - len(decided)}


def health() -> dict:
    with db.cursor() as conn:
        snap = conn.execute("SELECT MAX(date) d FROM snapshots").fetchone()["d"]
        n_snap = conn.execute("SELECT COUNT(*) n FROM snapshots WHERE date=%s", (snap,)).fetchone()["n"] if snap else 0
        sel = conn.execute("SELECT MAX(base_date) d FROM selection_runs").fetchone()["d"]
        n_sel = conn.execute("SELECT n_selected FROM selection_runs WHERE base_date=%s",
                             (sel,)).fetchone()["n_selected"] if sel else 0
        last = conn.execute(
            "SELECT job, status, started_at, finished_at, counts, message FROM runs "
            "ORDER BY id DESC LIMIT 1").fetchone()
        unlabeled = conn.execute(
            """SELECT COUNT(DISTINCT n.title_key) n FROM news n
               LEFT JOIN news_labels l ON l.title_key = n.title_key
               WHERE l.title_key IS NULL""").fetchone()["n"]
        rec = conn.execute(
            """SELECT COUNT(*) FILTER (WHERE o.hit OR o.final) decided,
                      COUNT(*) FILTER (WHERE o.hit) hits, COUNT(*) total
               FROM candidates c LEFT JOIN outcomes o ON o.candidate_id = c.id""").fetchone()
    return {"ok": True, "latest_snapshot": snap, "snapshot_stocks": n_snap,
            "latest_selection": sel, "selected": n_sel, "unlabeled_titles": unlabeled,
            "record": rec, "last_run": last}
