"""
自分が出した候補の成績を振り返る(procedures/select.md の手順 1)。

判定が確定した候補(+20% に届いた、または 10 営業日を過ぎた)と、追跡中の候補を出す。
ラベルごとの成否の件数も付ける。件数が少ないうちは偏りが大きいことに注意して読むこと。

使い方: python scripts/review.py
"""
from __future__ import annotations

import json
from collections import defaultdict

import _boot  # noqa: F401

from surge_radar import db


def main() -> None:
    with db.cursor() as conn:
        rows = conn.execute(
            """SELECT c.base_date, c.code, s.name, c.rank, c.conviction, c.labels, c.thesis,
                      c.trigger, c.risk, c.chart_view, c.base_close, c.procedure,
                      o.bars_tracked, o.max_ret, o.min_ret, o.hit, o.hit_day, o.final
               FROM candidates c
               LEFT JOIN outcomes o ON o.candidate_id = c.id
               LEFT JOIN securities s ON s.code = c.code
               ORDER BY c.base_date, c.rank""").fetchall()

    done = [r for r in rows if r["hit"] or r["final"]]
    open_ = [r for r in rows if not (r["hit"] or r["final"])]

    by_label: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    by_conv: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for r in done:
        for lb in r["labels"] or []:
            by_label[lb][0] += 1 if r["hit"] else 0
            by_label[lb][1] += 1
        by_conv[r["conviction"]][0] += 1 if r["hit"] else 0
        by_conv[r["conviction"]][1] += 1

    hits = sum(1 for r in done if r["hit"])
    out = {
        "summary": {"decided": len(done), "hit": hits, "tracking": len(open_)},
        "by_conviction": {str(k): f"{v[0]}/{v[1]}" for k, v in sorted(by_conv.items())},
        "by_label": {k: f"{v[0]}/{v[1]}" for k, v in sorted(by_label.items(), key=lambda x: -x[1][1])},
        "decided": done,
        "tracking": [{k: r[k] for k in ("base_date", "code", "name", "rank", "bars_tracked",
                                         "max_ret", "min_ret")} for r in open_],
    }
    print(json.dumps(out, ensure_ascii=False, default=str, indent=1))


if __name__ == "__main__":
    main()
