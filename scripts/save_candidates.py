"""
Claude が選んだ候補を保存する(procedures/select.md の手順 5)。

基準終値・目標価格(+20%)・スナップショットのラベルと特徴量は、ここで DB から取って記録する。
同じ基準日の候補が既にあれば保存しない(選び直しで結果を差し替えられないようにする)。

使い方: python scripts/save_candidates.py <candidates.json> [--model claude-...]
"""
from __future__ import annotations

import argparse
import json
import sys

import _boot  # noqa: F401

from surge_radar import db
from surge_radar.track import TARGET_RET

MAX_PICKS = 12


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--model", default="")
    a = ap.parse_args()
    doc = json.load(open(a.path, encoding="utf-8"))
    bd = doc["base_date"]
    cands = doc.get("candidates") or []

    errors = []
    if not 1 <= len(cands) <= MAX_PICKS:
        errors.append(f"候補数 {len(cands)} は 1〜{MAX_PICKS} の範囲外")
    with db.cursor() as conn:
        if conn.execute("SELECT 1 FROM selection_runs WHERE base_date=%s", (bd,)).fetchone():
            sys.exit(f"{bd} の候補は保存済み。差し替えはしない")
        snaps = {r["code"]: r for r in conn.execute(
            "SELECT code, close, features, labels FROM snapshots WHERE date=%s", (bd,)).fetchall()}
    if not snaps:
        errors.append(f"{bd} のスナップショットが無い")
    seen = set()
    for c in cands:
        code = str(c.get("code"))
        if code in seen:
            errors.append(f"{code} が重複")
        seen.add(code)
        if code not in snaps:
            errors.append(f"{code} は {bd} のスナップショットに無い(3000円超・売買なし・コード誤り)")
        for k in ("rank", "conviction", "thesis", "trigger", "risk", "chart_view"):
            if c.get(k) in (None, ""):
                errors.append(f"{code}: {k} が空")
        if c.get("conviction") not in (1, 2, 3, 4, 5):
            errors.append(f"{code}: conviction は 1〜5")
    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(f"{len(errors)} 件にエラー。何も保存していない")

    with db.cursor() as conn:
        for c in cands:
            s = snaps[str(c["code"])]
            conn.execute(
                """INSERT INTO candidates(base_date,code,base_close,target,rank,conviction,thesis,
                                          trigger,risk,chart_view,labels,features,procedure,model)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (bd, str(c["code"]), s["close"], s["close"] * (1 + TARGET_RET), c["rank"],
                 c["conviction"], c["thesis"], c["trigger"], c["risk"], c["chart_view"],
                 s["labels"], db.j(s["features"]), doc.get("procedure", ""), a.model))
        conn.execute(
            """INSERT INTO selection_runs(base_date,procedure,pool_size,n_selected,notes)
               VALUES(%s,%s,%s,%s,%s)""",
            (bd, doc.get("procedure", ""), doc.get("pool_size"), len(cands), doc.get("notes", "")))
    print(json.dumps({"base_date": bd, "saved": len(cands)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
