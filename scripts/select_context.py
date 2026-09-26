"""
候補選定のための情報を出す(procedures/select.md)。基準日より後の株価は一切出さない。

  --funnel           T_now・T_prev・Material Window・直近 10 営業日の除外・第 1 段階の通過銘柄
                     (data/tmp/funnel_<基準日>.json にも保存。save_candidates.py が読む)
  --code 1234        1 銘柄の日足・特徴・ラベル・見出し(Window 内か)・材料イベント・時価総額
  --labels A,B       指定ラベルをすべて持つ銘柄の一覧

基準日は --date、省略時は最新のスナップショットの日付。
--t-prev はユーザーが前回分析日時を指定したときだけ使う(推測で入れない)。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _boot  # noqa: F401

from surge_radar import db
from surge_radar.vocab import JST, timing, window_status

TMP = Path(__file__).resolve().parent.parent / "data" / "tmp"
EXCLUDE_DAYS = 10
ATR_MIN = 0.02
COLS = ["close", "ret_1d", "ret_5d", "ret_20d", "vol_ratio20", "vol_trend", "dist_high_20",
        "atr14_pct", "turnover_avg20"]


# ---------------- 共通 ----------------

def base_date(conn, d: str | None) -> str:
    return d or conn.execute("SELECT MAX(date) d FROM snapshots").fetchone()["d"]


def trading_days(conn) -> list[str]:
    """価格が 1000 銘柄以上揃っている日 = 営業日(昇順)。"""
    return [r["date"] for r in conn.execute(
        "SELECT date FROM prices GROUP BY date HAVING COUNT(*) > 1000 ORDER BY date").fetchall()]


def t_prev(conn, override: str | None) -> datetime | None:
    if override:
        return datetime.fromisoformat(override).astimezone(JST)
    r = conn.execute("SELECT MAX(t_now) t FROM selection_runs").fetchone()
    return r["t"].astimezone(JST) if r and r["t"] else None


def recent_predictions(conn, days: list[str], tn: datetime) -> dict[str, str]:
    """T_now から見た直近 10 営業日に予測した銘柄 -> 最新の基準日。"""
    past = [d for d in days if d <= tn.strftime("%Y-%m-%d")]
    if not past:
        return {}
    since = past[-EXCLUDE_DAYS] if len(past) >= EXCLUDE_DAYS else past[0]
    rows = conn.execute("SELECT code, MAX(base_date) d FROM candidates WHERE base_date >= %s "
                        "GROUP BY code", (since,)).fetchall()
    return {r["code"]: r["d"] for r in rows}


def new_material_codes(conn, tp, tn) -> dict[str, list[int]]:
    """Window 内の見出しから、主語になっている銘柄 -> 材料イベント id。"""
    rows = conn.execute(
        """SELECT n.code, n.date, n.published_at, e.id, e.subjects
           FROM news n JOIN material_events e ON e.title_key = n.title_key
           WHERE n.code = ANY(e.subjects)""").fetchall()
    out: dict[str, set[int]] = {}
    for r in rows:
        if window_status(r["published_at"], r["date"], tp, tn) == "new":
            out.setdefault(r["code"], set()).add(r["id"])
    return {k: sorted(v) for k, v in out.items()}


# ---------------- 第 1 段階のルート ----------------

def routes_for(L: set[str], f: dict, has_new_material: bool) -> list[str]:
    r = []
    if L & {"10日高値接近", "20日高値接近"}:
        r.append("A")
    if "20日線回復" in L or {"高値切り上げ", "安値切り上げ"} <= L:
        r.append("B")
    if L & {"出来高急増", "出来高漸増", "売買代金レジーム上昇", "価格横ばい＋出来高増加"}:
        r.append("C")
    if {"値幅収縮", "出来高収縮"} <= L:
        r.append("D")
    bull = (f.get("body_pct") or 0) > 0
    if L & {"長い下ヒゲ", "陽の包み足"} or (bull and L & {"10日安値接近", "20日安値接近"}):
        r.append("E")
    new_high_with_volume = L & {"5日高値更新", "10日高値更新"} and L & {"出来高増加", "出来高急増"}
    if new_high_with_volume or L & {"陽線3本連続", "陽線4本以上連続"}:
        r.append("G")
    if {"10日高値から5〜15%下", "20日線上"} <= L:
        r.append("H")
    # I: 別々の特徴群が 3 つ以上重なる(10 日と 20 日の高値接近は同じ群として 1 つに数える)
    families = [bool(L & {"10日高値接近", "20日高値接近"}), "出来高漸増" in L, "売買代金レジーム上昇" in L,
                "価格横ばい＋出来高増加" in L, "値幅収縮" in L, "出来高収縮" in L]
    if sum(families) >= 3:
        r.append("I")
    if has_new_material:
        r.append("M")
    return r


def fits_range(f: dict) -> bool:
    """値幅適性の入口条件: ATR14/株価 >= 2%。+20% が「10 営業日の典型的な変動幅(ATR%×√10)」の
    約 3.2 倍以内に収まる水準。新規材料のある銘柄(ルート M)はこの条件を問わない。
    2026-09-26 に、全銘柄を読める件数に収めるために入れた入口条件(成否データへの当てはめではない)。"""
    a = f.get("atr14_pct")
    return a is not None and a >= ATR_MIN


def _fmt(v, k):
    if v is None:
        return ""
    if k == "close":
        return f"{v:.0f}"
    if k == "turnover_avg20":
        return f"{v/1e8:.2f}億"
    return f"{v:.3f}"


def funnel(conn, bd: str, tp_override: str | None) -> dict:
    tn = datetime.now(JST)
    tp = t_prev(conn, tp_override)
    days = trading_days(conn)
    recent = recent_predictions(conn, days, tn)
    newmat = new_material_codes(conn, tp, tn)
    snaps = conn.execute(
        """SELECT s.code, s.features, s.labels, c.name FROM snapshots s
           LEFT JOIN securities c ON c.code = s.code WHERE s.date = %s ORDER BY s.code""",
        (bd,)).fetchall()
    n_universe = conn.execute("SELECT COUNT(*) n FROM securities").fetchone()["n"]
    n_priced = conn.execute("SELECT COUNT(DISTINCT code) n FROM prices WHERE date=%s", (bd,)).fetchone()["n"]

    passed, excluded = [], []
    route_counts: dict[str, int] = {}
    low_range = 0
    for s in snaps:
        if s["code"] in recent:
            excluded.append({"code": s["code"], "reason": f"直近{EXCLUDE_DAYS}営業日に予測済み",
                             "last_base_date": recent[s["code"]]})
            continue
        L = set(s["labels"] or [])
        rs = routes_for(L, s["features"], s["code"] in newmat)
        if rs and "M" not in rs and not fits_range(s["features"]):
            low_range += 1
            continue
        for x in rs:
            route_counts[x] = route_counts.get(x, 0) + 1
        if rs:
            passed.append({"code": s["code"], "name": s["name"], "routes": rs,
                           "new_material_events": newmat.get(s["code"], []),
                           "f": s["features"], "labels": s["labels"]})

    doc = {
        "base_date": bd,
        "t_now": tn.isoformat(timespec="seconds"),
        "t_prev": tp.isoformat(timespec="seconds") if tp else None,
        "material_window": {
            "rule": "T_prev < 公開時刻 <= T_now" if tp else
                    "前回分析日時不明のため、T_now と同じ日(JST)に公開されたものだけを暫定の新規材料とする",
            "from": tp.isoformat(timespec="seconds") if tp else
                    tn.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds"),
            "to": tn.isoformat(timespec="seconds"),
            "codes_with_new_material": len(newmat)},
        "exclusion_range": {"trading_days": EXCLUDE_DAYS,
                            "since": ([d for d in days if d <= tn.strftime('%Y-%m-%d')][-EXCLUDE_DAYS:] or [None])[0]},
        "counts": {"universe_securities": n_universe, "priced_on_base_date": n_priced,
                   "le_3000_snapshot": len(snaps), "excluded_recent": len(excluded),
                   "route_hit_but_atr_below_min": low_range, "atr_min": ATR_MIN,
                   "stage1_passed": len(passed), "by_route": dict(sorted(route_counts.items()))},
        "excluded": excluded,
        "stage1": [{"code": p["code"], "routes": p["routes"],
                    "new_material_events": p["new_material_events"]} for p in passed],
    }
    TMP.mkdir(parents=True, exist_ok=True)
    (TMP / f"funnel_{bd}.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    # 表示: 要約 + 通過銘柄の表
    print(json.dumps({k: doc[k] for k in ("base_date", "t_now", "t_prev", "material_window",
                                          "exclusion_range", "counts")}, ensure_ascii=False, indent=1))
    print(f"# 除外: {', '.join(e['code'] for e in excluded) or 'なし'}")
    # 表は簡潔に(ラベル全体は --code で見る)。社名は半角に寄せて 12 文字まで
    import unicodedata
    cols = ["code", "name", "routes", "new_mat", *COLS]
    print("\t".join(cols))
    for p in passed:
        name = unicodedata.normalize("NFKC", p["name"] or "")[:12]
        row = [p["code"], name, "".join(p["routes"]), str(len(p["new_material_events"]))]
        row += [_fmt(p["f"].get(k), k) for k in COLS]
        print("\t".join(row))
    return doc


# ---------------- 1 銘柄 ----------------

def one(conn, bd: str, code: str, tp_override: str | None) -> dict:
    from surge_radar.sources import yahoo
    tn = datetime.now(JST)
    tp = t_prev(conn, tp_override)
    days = trading_days(conn)
    sec = conn.execute("SELECT name, market, sector33 FROM securities WHERE code=%s", (code,)).fetchone()
    snap = conn.execute("SELECT features, labels, label_version FROM snapshots WHERE date=%s AND code=%s",
                        (bd, code)).fetchone()
    bars = conn.execute(
        "SELECT date, open, high, low, close, volume FROM prices WHERE code=%s AND date <= %s "
        "ORDER BY date", (code, bd)).fetchall()
    news = conn.execute(
        """SELECT n.date, n.published_at, n.source, n.title, n.title_key, v.subjects, v.n_events, v.note
           FROM news n LEFT JOIN news_reviews v ON v.title_key = n.title_key
           WHERE n.code = %s ORDER BY n.date DESC, n.id DESC""", (code,)).fetchall()
    events = {}
    keys = list({n["title_key"] for n in news})
    if keys:
        for e in conn.execute(
                """SELECT id, title_key, subjects, event_type, actor, pathways, scope, stage, facts
                   FROM material_events WHERE title_key = ANY(%s) ORDER BY title_key, idx""",
                (keys,)).fetchall():
            events.setdefault(e["title_key"], []).append(e)
    tdset = set(days)
    out_news = []
    for n in news:
        pub = n["published_at"].astimezone(JST) if n["published_at"] else None
        evs = [{k: e[k] for k in ("id", "event_type", "actor", "pathways", "scope", "stage", "facts")}
               for e in events.get(n["title_key"], []) if code in (e["subjects"] or [])]
        out_news.append({
            "date": n["date"], "published_at": pub.isoformat(timespec="minutes") if pub else None,
            "timing": timing(pub, tdset), "window": window_status(pub, n["date"], tp, tn),
            "source": n["source"], "title": n["title"],
            "reviewed": n["n_events"] is not None,
            "is_subject": (code in (n["subjects"] or [])) if n["n_events"] is not None else None,
            "events": evs, "note": n["note"]})
    try:
        meta = yahoo.fetch_jp_stats(code)
        meta["fetched_at"] = datetime.now(JST).isoformat(timespec="minutes")
        meta["note"] = ("Yahoo!ファイナンス日本版の表示値(asof は表示された基準日)。"
                        "Float は取得していないので不明。読めなかった項目は含まない")
    except Exception as e:  # 取れなければ不明として扱う
        meta = {"error": f"{type(e).__name__}", "note": "時価総額・発行済株式数は不明"}
    recent = recent_predictions(conn, days, tn)
    return {"base_date": bd, "code": code, "security": sec,
            "recently_predicted": recent.get(code),
            "label_version": snap["label_version"] if snap else None,
            "features": snap["features"] if snap else None,
            "labels": snap["labels"] if snap else None,
            "bars": bars, "news": out_news, "meta": meta}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--t-prev", help="ユーザーが指定した前回分析日時(ISO 形式)。推測で入れない")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--funnel", action="store_true")
    g.add_argument("--labels")
    g.add_argument("--code")
    a = ap.parse_args()
    with db.cursor() as conn:
        bd = base_date(conn, a.date)
        if a.funnel:
            funnel(conn, bd, a.t_prev)
            return
        if a.code:
            print(json.dumps(one(conn, bd, a.code, a.t_prev), ensure_ascii=False, default=str))
            return
        want = a.labels.split(",")
        rows = conn.execute(
            """SELECT s.code, c.name, s.labels FROM snapshots s LEFT JOIN securities c ON c.code = s.code
               WHERE s.date = %s AND s.labels @> %s ORDER BY s.code""", (bd, want)).fetchall()
    print(f"# base_date={bd} labels={want} count={len(rows)}")
    for r in rows:
        print(f"{r['code']}\t{r['name'] or ''}\t{'/'.join(r['labels'])}")


if __name__ == "__main__":
    main()
