"""
Claude が付けた材料ラベル(原子的イベント × 6 軸)を保存する。procedures/label.md。

処理済みの見出しは上書きしない(付け直さない)。1 件でも誤りがあれば何も保存しない。

使い方: python scripts/save_labels.py <labels.json> --model <モデル名>
"""
from __future__ import annotations

import argparse
import json
import sys

import _boot  # noqa: F401

from surge_radar import db
from surge_radar.vocab import (ACTORS, EVENT_TYPES, MATERIAL_VERSION, PATHWAYS, SCOPES, STAGES,
                               VALUE_WORDS)


def validate(items: list[dict], linked: dict[str, set[str]]) -> list[str]:
    errors = []
    for it in items:
        k = it.get("title_key")
        tag = (k or "")[:30]
        if k not in linked:
            errors.append(f"未知の title_key: {k!r}")
            continue
        subj = [str(s) for s in it.get("subjects") or []]
        bad = [s for s in subj if s not in linked[k]]
        if bad:
            errors.append(f"{tag}: 紐付いていない銘柄を主語にしている {bad}")
        events = it.get("events")
        if events is None:
            errors.append(f"{tag}: events が無い(出来事が無ければ空リストにする)")
            continue
        if events and not subj:
            errors.append(f"{tag}: 主語が無いのに events がある")
        for i, e in enumerate(events):
            es = [str(s) for s in e.get("subjects") or []]
            if not es or any(s not in subj for s in es):
                errors.append(f"{tag}[{i}]: イベントの主語は見出しの subjects から選ぶ {es}")
            if e.get("event_type") not in EVENT_TYPES:
                errors.append(f"{tag}[{i}]: event_type が選択肢に無い {e.get('event_type')!r}")
            if e.get("actor") not in ACTORS:
                errors.append(f"{tag}[{i}]: actor が選択肢に無い {e.get('actor')!r}")
            pw = e.get("pathways") or []
            if not pw or any(p not in PATHWAYS for p in pw):
                errors.append(f"{tag}[{i}]: pathways が空か選択肢に無い {pw}")
            if e.get("scope") not in SCOPES:
                errors.append(f"{tag}[{i}]: scope が選択肢に無い {e.get('scope')!r}")
            if e.get("stage") not in STAGES:
                errors.append(f"{tag}[{i}]: stage が選択肢に無い {e.get('stage')!r}")
            facts = (e.get("facts") or "").strip()
            if not facts:
                errors.append(f"{tag}[{i}]: facts が空")
            if any(w in facts for w in VALUE_WORDS):
                errors.append(f"{tag}[{i}]: facts に価値判断の語がある")
    return errors


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--model", required=True)
    a = ap.parse_args()
    items = json.load(open(a.path, encoding="utf-8"))

    keys = [it.get("title_key") for it in items]
    with db.cursor() as conn:
        linked: dict[str, set[str]] = {}
        for r in conn.execute("SELECT title_key, code FROM news WHERE title_key = ANY(%s)",
                              (keys,)).fetchall():
            linked.setdefault(r["title_key"], set()).add(r["code"])
        done = {r["title_key"] for r in conn.execute(
            "SELECT title_key FROM news_reviews WHERE title_key = ANY(%s)", (keys,)).fetchall()}

    errors = validate(items, linked)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(f"{len(errors)} 件にエラー。何も保存していない。直して再実行すること")

    new = [it for it in items if it["title_key"] not in done]
    n_events = 0
    with db.cursor() as conn:
        for it in new:
            events = it["events"]
            conn.execute(
                """INSERT INTO news_reviews(title_key,subjects,n_events,note,procedure,model)
                   VALUES(%s,%s,%s,%s,%s,%s)""",
                (it["title_key"], [str(s) for s in it.get("subjects") or []], len(events),
                 it.get("note") or "", MATERIAL_VERSION, a.model))
            for i, e in enumerate(events):
                conn.execute(
                    """INSERT INTO material_events(title_key,idx,subjects,event_type,actor,pathways,
                                                   scope,stage,facts,amount,procedure,model)
                       VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (it["title_key"], i, [str(s) for s in e["subjects"]], e["event_type"],
                     e["actor"], e["pathways"], e["scope"], e["stage"], e["facts"].strip(),
                     e.get("amount"), MATERIAL_VERSION, a.model))
                n_events += 1
    print(json.dumps({"reviewed": len(new), "events": n_events,
                      "skipped_already_reviewed": len(items) - len(new)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
