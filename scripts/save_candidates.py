"""
Claude が選んだ候補とレポートを保存する(procedures/select.md の手順 6)。

T_now・T_prev・Material Window・各段階の件数・除外一覧・ルートは data/tmp/funnel_<基準日>.json から取る。
基準終値(P0)・Target・スナップショットのラベルと特徴量は DB から取る。
同じ基準日の記録が既にあれば保存しない(選び直しで結果を差し替えられないようにする)。
1 件でも誤りがあれば何も保存しない。候補 0 件も記録として保存する。

使い方: python scripts/save_candidates.py <candidates.json> --report <report.md> --model <モデル名>
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import _boot  # noqa: F401

from surge_radar import db
from surge_radar.track import TARGET_RET
from surge_radar.vocab import JST, save_deadline

TMP = Path(__file__).resolve().parent.parent / "data" / "tmp"
STATUSES = {"あり", "新規材料なし", "新規材料確認不能"}
EVAL_WORDS = ("良いチャート", "悪いチャート", "健全", "理想", "綺麗", "きれいな", "強い形", "好材料", "悪材料")
REQUIRED_TEXT = ("thesis", "teacher_match", "post_surge_check", "dilution")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--report", required=True)
    ap.add_argument("--model", required=True)
    a = ap.parse_args()
    doc = json.load(open(a.path, encoding="utf-8"))
    bd = doc["base_date"]
    cands = doc.get("candidates")
    report = Path(a.report).read_text(encoding="utf-8")

    # 寄り付き後の保存は拒否する(判定期間の値動きを見てから記録できてしまうため)
    deadline = save_deadline(bd)
    if datetime.now(JST) >= deadline:
        sys.exit(f"保存期限 {deadline:%Y-%m-%d %H:%M}(基準日の翌営業日の寄り付き)を過ぎた。保存しない")

    fpath = TMP / f"funnel_{bd}.json"
    if not fpath.exists():
        sys.exit(f"{fpath} が無い。先に select_context.py --funnel を実行すること")
    fun = json.loads(fpath.read_text(encoding="utf-8"))
    stage1 = {s["code"]: s for s in fun["stage1"]}
    excluded = {e["code"] for e in fun["excluded"]}

    errors = []
    # 全銘柄を見たことの確認: 第 1 段階の表を全ページ読んでいること(ユーザー決定 2026-09-26)
    spath = TMP / f"scan_{bd}.json"
    scan = json.loads(spath.read_text(encoding="utf-8")) if spath.exists() else {}
    if scan.get("t_now") != fun["t_now"]:
        errors.append("第 1 段階の走査記録が今回の --funnel と対応していない。--funnel の後に全ページを読み直すこと")
    else:
        unread = [p for p in range(1, fun["counts"]["pages"] + 1) if str(p) not in scan["pages_viewed"]]
        if unread:
            errors.append(f"第 1 段階の表で読んでいないページがある: {unread}(select_context.py --page N)")
    if not isinstance(cands, list):
        errors.append("candidates はリスト(0 件なら [])")
        cands = []
    if not report.strip():
        errors.append("レポートが空")
    with db.cursor() as conn:
        if conn.execute("SELECT 1 FROM selection_runs WHERE base_date=%s", (bd,)).fetchone():
            sys.exit(f"{bd} の記録は保存済み。差し替えはしない")
        snaps = {r["code"]: r for r in conn.execute(
            "SELECT code, close, features, labels FROM snapshots WHERE date=%s", (bd,)).fetchall()}

    seen = set()
    for c in cands:
        code = str(c.get("code"))
        tag = code
        if code in seen:
            errors.append(f"{tag}: 重複")
        seen.add(code)
        if code in excluded:
            errors.append(f"{tag}: 直近 10 営業日に予測済みのため対象外")
        if code not in snaps:
            errors.append(f"{tag}: {bd} のスナップショットに無い(3000 円超・売買なし・コード誤り)")
            continue
        if code not in stage1:
            errors.append(f"{tag}: 第 1 段階の走査対象に無い")
        for k in REQUIRED_TEXT:
            if not str(c.get(k) or "").strip():
                errors.append(f"{tag}: {k} が空")
        st = c.get("material_status")
        if st not in STATUSES:
            errors.append(f"{tag}: material_status は {sorted(STATUSES)} のどれか")
        ids = c.get("material_event_ids") or []
        if st == "あり":
            allowed = set(stage1.get(code, {}).get("new_material_events", []))
            if not ids:
                errors.append(f"{tag}: 材料ありなのに material_event_ids が空")
            elif not set(ids) <= allowed:
                errors.append(f"{tag}: Material Window 外の材料イベントを新規材料にしている {sorted(set(ids) - allowed)}")
            if not str(c.get("material_analysis") or "").strip():
                errors.append(f"{tag}: 材料ありなのに material_analysis が空")
        elif ids:
            errors.append(f"{tag}: 材料なし・確認不能なのに material_event_ids がある")
        pats = c.get("chart_patterns")
        if not isinstance(pats, list):
            errors.append(f"{tag}: chart_patterns はリスト")
            pats = []
        for p in pats:
            if not str(p.get("label") or "").strip() or not str(p.get("evidence") or "").strip():
                errors.append(f"{tag}: chart_patterns の各要素に label と evidence が要る")
        p0 = snaps[code]["close"]
        path = c.get("path") or {}
        for k in ("p0", "target", "conditions", "why_10d", "most_uncertain"):
            if path.get(k) in (None, ""):
                errors.append(f"{tag}: path.{k} が空")
        try:
            if abs(float(path.get("p0")) - p0) > 0.5:
                errors.append(f"{tag}: path.p0={path.get('p0')} が基準日終値 {p0} と違う")
            if abs(float(path.get("target")) - p0 * (1 + TARGET_RET)) > 1.0:
                errors.append(f"{tag}: path.target は P0×1.2 = {p0 * (1 + TARGET_RET):.1f}")
        except (TypeError, ValueError):
            errors.append(f"{tag}: path.p0 / path.target が数値でない")
        fz = c.get("falsifiers") or {}
        if not isinstance(fz, dict) or not any(str(v).strip() for v in fz.values()):
            errors.append(f"{tag}: falsifiers(反証条件)が空")
        blob = json.dumps(c, ensure_ascii=False)
        bad = [w for w in EVAL_WORDS if w in blob]
        if bad:
            errors.append(f"{tag}: 評価語を使っている {bad}(観測した形・事実で書く)")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(f"{len(errors)} 件にエラー。何も保存していない")

    funnel_rec = {k: fun[k] for k in ("material_window", "exclusion_range", "counts")}
    funnel_rec["stage2"] = doc.get("stage2")
    funnel_rec["pages_viewed"] = scan.get("pages_viewed")
    with db.cursor() as conn:
        for c in cands:
            code = str(c["code"])
            s = snaps[code]
            conn.execute(
                """INSERT INTO candidates(base_date,code,base_close,target,thesis,labels,features,procedure,
                       model,t_now,chart_patterns,supply,material_status,material_event_ids,
                       material_analysis,teacher_match,post_surge_check,dilution,path,falsifiers,routes)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (bd, code, s["close"], s["close"] * (1 + TARGET_RET), c["thesis"], s["labels"],
                 db.j(s["features"]), doc.get("procedure", ""), a.model, fun["t_now"],
                 db.j(c.get("chart_patterns") or []), db.j(c.get("supply") or {}),
                 c["material_status"], c.get("material_event_ids") or [], c.get("material_analysis"),
                 c["teacher_match"], c["post_surge_check"], c["dilution"], db.j(c["path"]),
                 db.j(c["falsifiers"]), stage1[code]["routes"]))
        conn.execute(
            """INSERT INTO selection_runs(base_date,procedure,pool_size,n_selected,notes,t_prev,t_now,
                                          funnel,exclusions,report)
               VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (bd, doc.get("procedure", ""), fun["counts"]["stage1_scanned"], len(cands),
             doc.get("notes", ""), fun["t_prev"], fun["t_now"], db.j(funnel_rec),
             db.j(fun["excluded"]), report))
    print(json.dumps({"base_date": bd, "saved": len(cands), "t_now": fun["t_now"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
