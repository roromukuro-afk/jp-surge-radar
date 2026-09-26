"""
候補選定のための情報を出す(procedures/select.md)。基準日より後の株価は一切出さない。

  --funnel           T_now・T_prev・Material Window・直近 10 営業日の除外・第 1 段階の走査対象(全銘柄)
                     (data/tmp/funnel_<基準日>.json に保存。save_candidates.py が読む)
  --page N           第 1 段階の表の N ページ目(約 300 銘柄ずつ)。読んだページを data/tmp/scan_<基準日>.json に記録
  --code 1234        1 銘柄の日足・特徴・ラベル・見出し(Window 内か)・材料イベント・時価総額
  --labels A,B       指定ラベルをすべて持つ銘柄の一覧

基準日は --date、省略時は最新のスナップショットの日付。
Material Window は「基準日の終値の時刻(15:30) < 公開時刻 <= 分析開始(T_now)」(ユーザー決定 2026-09-26)。
T_prev(前回の正式な分析の時刻)は記録のために出すだけで、Window には使わない。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import _boot  # noqa: F401

from surge_radar import db
from surge_radar.vocab import JST, save_deadline, timing, window_start, window_status

TMP = Path(__file__).resolve().parent.parent / "data" / "tmp"
EXCLUDE_DAYS = 10
PAGE_SIZE = 300
COLS = ["close", "ret_1d", "ret_5d", "ret_20d", "vol_ratio20", "vol_trend", "dist_high_20",
        "atr14_pct", "turnover_avg20"]


# ---------------- 共通 ----------------

def base_date(conn, d: str | None) -> str:
    return d or conn.execute("SELECT MAX(date) d FROM snapshots").fetchone()["d"]


def trading_days(conn) -> list[str]:
    """価格が 1000 銘柄以上揃っている日 = 営業日(昇順)。"""
    return [r["date"] for r in conn.execute(
        "SELECT date FROM prices GROUP BY date HAVING COUNT(*) > 1000 ORDER BY date").fetchall()]


def t_prev(conn) -> datetime | None:
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


def new_material_codes(conn, start, tn) -> dict[str, list[int]]:
    """Window 内の見出しから、主語になっている銘柄 -> 材料イベント id。"""
    rows = conn.execute(
        """SELECT n.code, n.date, n.published_at, e.id, e.subjects
           FROM news n JOIN material_events e ON e.title_key = n.title_key
           WHERE n.code = ANY(e.subjects)""").fetchall()
    out: dict[str, set[int]] = {}
    for r in rows:
        if window_status(r["published_at"], r["date"], start, tn) == "new":
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


def _fmt(v, k):
    if v is None:
        return ""
    if k == "close":
        return f"{v:.0f}"
    if k == "turnover_avg20":
        return f"{v/1e8:.2f}億"
    return f"{v:.3f}"


def _funnel_path(bd: str) -> Path:
    return TMP / f"funnel_{bd}.json"


def _scan_path(bd: str) -> Path:
    return TMP / f"scan_{bd}.json"


def funnel(conn, bd: str) -> dict:
    """全銘柄(3000 円以下・直近 10 営業日の予測済みを除く)を第 1 段階の走査対象にする。
    ルートは選別に使わず、各銘柄の目印として付ける(ユーザー決定 2026-09-26: 全銘柄を見る)。"""
    tn = datetime.now(JST)
    tp = t_prev(conn)
    start = window_start(bd)
    days = trading_days(conn)
    recent = recent_predictions(conn, days, tn)
    newmat = new_material_codes(conn, start, tn)
    snaps = conn.execute(
        """SELECT s.code, s.features, s.labels FROM snapshots s WHERE s.date = %s ORDER BY s.code""",
        (bd,)).fetchall()
    n_universe = conn.execute("SELECT COUNT(*) n FROM securities").fetchone()["n"]
    n_priced = conn.execute("SELECT COUNT(DISTINCT code) n FROM prices WHERE date=%s", (bd,)).fetchone()["n"]

    scan, excluded = [], []
    route_counts: dict[str, int] = {}
    for s in snaps:
        if s["code"] in recent:
            excluded.append({"code": s["code"], "reason": f"直近{EXCLUDE_DAYS}営業日に予測済み",
                             "last_base_date": recent[s["code"]]})
            continue
        rs = routes_for(set(s["labels"] or []), s["features"], s["code"] in newmat)
        for x in rs:
            route_counts[x] = route_counts.get(x, 0) + 1
        scan.append({"code": s["code"], "routes": rs, "new_material_events": newmat.get(s["code"], [])})

    n_pages = (len(scan) + PAGE_SIZE - 1) // PAGE_SIZE
    doc = {
        "base_date": bd,
        "t_now": tn.isoformat(timespec="seconds"),
        "t_prev": tp.isoformat(timespec="seconds") if tp else None,
        "save_deadline": save_deadline(bd).isoformat(timespec="minutes"),
        "material_window": {
            "rule": "基準日の終値の時刻 < 公開時刻 <= T_now(分析開始)。日付だけの見出しは基準日より後の日付なら新規、"
                    "基準日と同じ日なら終値の前か後か分からないので新規に数えない",
            "from": start.isoformat(timespec="seconds"),
            "to": tn.isoformat(timespec="seconds"),
            "codes_with_new_material": len(newmat)},
        "exclusion_range": {"trading_days": EXCLUDE_DAYS,
                            "since": ([d for d in days if d <= tn.strftime('%Y-%m-%d')][-EXCLUDE_DAYS:] or [None])[0]},
        "counts": {"universe_securities": n_universe, "priced_on_base_date": n_priced,
                   "le_3000_snapshot": len(snaps), "excluded_recent": len(excluded),
                   "stage1_scanned": len(scan), "pages": n_pages, "page_size": PAGE_SIZE,
                   "by_route": dict(sorted(route_counts.items())),
                   "no_route": sum(1 for x in scan if not x["routes"])},
        "excluded": excluded,
        "stage1": scan,
    }
    TMP.mkdir(parents=True, exist_ok=True)
    _funnel_path(bd).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    # 走査記録は funnel を作り直すたびに空から始める
    _scan_path(bd).write_text(json.dumps({"t_now": doc["t_now"], "pages_viewed": {}}), encoding="utf-8")

    if tn >= save_deadline(bd):
        print(f"# 注意: 保存期限 {doc['save_deadline']}(翌営業日の寄り付き)を過ぎている。この基準日の候補は保存できない")
    print(json.dumps({k: doc[k] for k in ("base_date", "t_now", "t_prev", "save_deadline", "material_window",
                                          "exclusion_range", "counts")}, ensure_ascii=False, indent=1))
    print(f"# 除外: {', '.join(e['code'] for e in excluded) or 'なし'}")
    new_codes = [x["code"] for x in scan if x["new_material_events"]]
    print(f"# 新規材料のある銘柄({len(new_codes)}): {', '.join(new_codes) or 'なし'}")
    print(f"# 全 {n_pages} ページ。`--page 1` から `--page {n_pages}` まで全部読むこと"
          f"(読んでいないページがあると save_candidates.py が保存を拒否する)")
    return doc


def page(conn, bd: str, n: int) -> None:
    """第 1 段階の表の n ページ目(1 始まり)を出し、読んだことを記録する。"""
    import unicodedata
    fpath = _funnel_path(bd)
    if not fpath.exists():
        raise SystemExit("先に --funnel を実行すること")
    fun = json.loads(fpath.read_text(encoding="utf-8"))
    n_pages = fun["counts"]["pages"]
    if not 1 <= n <= n_pages:
        raise SystemExit(f"ページは 1〜{n_pages}")
    rows = fun["stage1"][(n - 1) * PAGE_SIZE: n * PAGE_SIZE]
    codes = [r["code"] for r in rows]
    snaps = {s["code"]: s for s in conn.execute(
        """SELECT s.code, s.features, c.name FROM snapshots s LEFT JOIN securities c ON c.code = s.code
           WHERE s.date = %s AND s.code = ANY(%s)""", (bd, codes)).fetchall()}
    cols = ["code", "name", "routes", "new_mat", *COLS]
    print(f"# base_date={bd} page {n}/{n_pages}  ({len(rows)} 銘柄)")
    print("\t".join(cols))
    for r in rows:
        s = snaps[r["code"]]
        name = unicodedata.normalize("NFKC", s["name"] or "")[:12]
        row = [r["code"], name, "".join(r["routes"]) or "-", str(len(r["new_material_events"]))]
        row += [_fmt(s["features"].get(k), k) for k in COLS]
        print("\t".join(row))
    scan = json.loads(_scan_path(bd).read_text(encoding="utf-8"))
    scan["pages_viewed"][str(n)] = datetime.now(JST).isoformat(timespec="seconds")
    _scan_path(bd).write_text(json.dumps(scan, ensure_ascii=False), encoding="utf-8")
    left = [p for p in range(1, n_pages + 1) if str(p) not in scan["pages_viewed"]]
    print(f"# 未読のページ: {left or 'なし'}")


# ---------------- 1 銘柄 ----------------

def one(conn, bd: str, code: str) -> dict:
    from surge_radar.sources import yahoo
    tn = datetime.now(JST)
    start = window_start(bd)
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
            "timing": timing(pub, tdset), "window": window_status(pub, n["date"], start, tn),
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
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--funnel", action="store_true")
    g.add_argument("--page", type=int, help="第 1 段階の表の N ページ目(1 始まり)")
    g.add_argument("--labels")
    g.add_argument("--code")
    a = ap.parse_args()
    with db.cursor() as conn:
        bd = base_date(conn, a.date)
        if a.funnel:
            funnel(conn, bd)
            return
        if a.page:
            page(conn, bd, a.page)
            return
        if a.code:
            print(json.dumps(one(conn, bd, a.code), ensure_ascii=False, default=str))
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
