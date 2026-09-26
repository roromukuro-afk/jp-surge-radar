"""
教師データ(この仕組みが実際に選んだ候補とその成否)の振り返り。procedures/select.md の手順 1。

成功群と失敗群を比べる。判定期間中・判定未確認は比較に入れない。
各特徴について、次を件数・分母つきで出す(定義は下の DEFINITIONS)。
特徴が「不明」(データ不足で判定できない)の候補は、その特徴の分母から外す。

使い方: python scripts/review.py [--pairs] [--min-support 2]
"""
from __future__ import annotations

import argparse
import itertools
import json

import _boot  # noqa: F401

from surge_radar import db

DEFINITIONS = {
    "N": "比較に使う判定済み候補の数(その特徴が不明なものを除く)",
    "S / F": "N のうち成功 / 失敗の数",
    "with_S / with_F": "その特徴を持つ成功 / 失敗の数",
    "rate_in_S / rate_in_F": "成功群・失敗群それぞれでの出現率 (with_S/S, with_F/F)",
    "succ_rate_with / succ_rate_without": "その特徴を持つ候補の成功率 / 持たない候補の成功率",
    "support": "(with_S+with_F)/N",
    "confidence": "with_S/(with_S+with_F) = succ_rate_with",
    "lift": "confidence ÷ (S/N)。1 より大きいほど、その特徴を持つ候補で成功が多い",
    "odds_ratio": "(with_S+0.5)(without_F+0.5) / ((with_F+0.5)(without_S+0.5))。0 件の枠があるので各枠に 0.5 を足している",
}

# 足りないと計算できないラベルと、その判定に使う数値特徴(None なら不明)
REQUIRES = [
    (("20日",), "ma20"), (("10日",), "high_10"), (("5日高値", "5日安値", "5日線"), "high_5"),
    (("値幅収縮", "値幅拡大"), "range_avg20"),
    (("出来高急増", "出来高増加", "出来高減少", "＋出来高"), "vol_ratio20"),
    (("出来高漸増", "出来高収縮"), "vol_trend"), (("売買代金",), "turnover_avg20"),
    (("上昇トレンド", "下降トレンド", "横ばい"), "ma20_slope5"),
    (("高値切り", "高値横ばい", "安値切り", "安値横ばい"), "hh_ratio"),
]
KNOWN_MECH_PREFIXES = [p for ps, _ in REQUIRES for p in ps]


def unknown_labels(features: dict) -> set[str]:
    """数値特徴が None で判定できなかったラベル族(接頭辞)を返す。"""
    return {p for ps, key in REQUIRES if features.get(key) is None for p in ps}


def is_unknown(label: str, unk: set[str]) -> bool:
    return any(p in label for p in unk)


def load() -> list[dict]:
    with db.cursor() as conn:
        rows = conn.execute(
            """SELECT c.id, c.base_date, c.code, s.name, c.labels, c.features, c.chart_patterns,
                      c.material_status, c.material_event_ids, c.routes, c.thesis, c.base_close,
                      o.bars_tracked, o.max_ret, o.min_ret, o.hit, o.hit_day, o.final, o.status
               FROM candidates c LEFT JOIN outcomes o ON o.candidate_id = c.id
               LEFT JOIN securities s ON s.code = c.code
               ORDER BY c.base_date, c.code""").fetchall()
        ev = {}
        ids = [i for r in rows for i in (r["material_event_ids"] or [])]
        if ids:
            for e in conn.execute("SELECT id, event_type FROM material_events WHERE id = ANY(%s)",
                                  (ids,)).fetchall():
                ev[e["id"]] = e["event_type"]
    for r in rows:
        feats = set(r["labels"] or [])
        feats |= {f"形:{p['label']}" for p in (r["chart_patterns"] or []) if p.get("label")}
        feats |= {f"材料:{ev[i]}" for i in (r["material_event_ids"] or []) if i in ev}
        feats.add(f"材料状態:{r['material_status']}")
        feats |= {f"ルート:{x}" for x in (r["routes"] or [])}
        r["feats"] = feats
        r["unknown"] = unknown_labels(r["features"] or {})
        r["result"] = ("success" if r["hit"] else "failure" if r["final"]
                       else "unverified" if r["status"] == "unverified" else "tracking")
    return rows


def stats(decided: list[dict], feat_sets: list[tuple[str, ...]]) -> list[dict]:
    out = []
    for fs in feat_sets:
        rel = [r for r in decided if not any(is_unknown(f, r["unknown"]) for f in fs)]
        N = len(rel)
        if N == 0:
            continue
        S = sum(1 for r in rel if r["result"] == "success")
        F = N - S
        has = [r for r in rel if all(f in r["feats"] for f in fs)]
        wS = sum(1 for r in has if r["result"] == "success")
        wF = len(has) - wS
        if not has:
            continue
        woS, woF = S - wS, F - wF
        conf = wS / len(has)
        out.append({
            "feature": " × ".join(fs), "N": N, "S": S, "F": F, "with_S": wS, "with_F": wF,
            "rate_in_S": round(wS / S, 3) if S else None, "rate_in_F": round(wF / F, 3) if F else None,
            "succ_rate_with": round(conf, 3),
            "succ_rate_without": round(woS / (woS + woF), 3) if (woS + woF) else None,
            "support": round(len(has) / N, 3), "confidence": round(conf, 3),
            "lift": round(conf / (S / N), 3) if S else None,
            "odds_ratio": round((wS + .5) * (woF + .5) / ((wF + .5) * (woS + .5)), 3),
        })
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", action="store_true", help="2 つの特徴の組み合わせも出す")
    ap.add_argument("--min-support", type=int, default=2, help="組み合わせは、この件数以上に出たものだけ")
    a = ap.parse_args()

    rows = load()
    decided = [r for r in rows if r["result"] in ("success", "failure")]
    S = sum(1 for r in decided if r["result"] == "success")
    summary = {
        "total_candidates": len(rows), "decided": len(decided), "success": S,
        "failure": len(decided) - S,
        "tracking": sum(1 for r in rows if r["result"] == "tracking"),
        "unverified": sum(1 for r in rows if r["result"] == "unverified"),
        "distinct_codes_decided": len({r["code"] for r in decided}),
        "distinct_base_dates_decided": len({r["base_date"] for r in decided}),
    }
    out = {"summary": summary, "definitions": DEFINITIONS}
    if decided:
        singles = sorted({f for r in decided for f in r["feats"]})
        out["single_features"] = sorted(stats(decided, [(f,) for f in singles]),
                                        key=lambda x: (-(x["with_S"] + x["with_F"]), x["feature"]))
        if a.pairs:
            counts: dict[tuple, int] = {}
            for r in decided:
                for p in itertools.combinations(sorted(r["feats"]), 2):
                    counts[p] = counts.get(p, 0) + 1
            pairs = [p for p, n in counts.items() if n >= a.min_support]
            res = stats(decided, pairs)
            out["pairs"] = sorted(res, key=lambda x: (-(x["lift"] or 0), -(x["with_S"] + x["with_F"])))[:80]
            out["pairs_note"] = (f"{len(pairs)} 通りの組み合わせを試した。多数を試すと偶然の関連が必ず出るので、"
                                 f"件数の少ない組み合わせの Lift を法則として扱わない")
        out["decided_cases"] = [{k: r[k] for k in ("base_date", "code", "name", "result", "hit_day",
                                                   "max_ret", "min_ret", "material_status", "routes", "thesis")}
                                for r in decided]
    else:
        out["note"] = "判定済みの候補はまだ無い"
    out["tracking_cases"] = [{k: r[k] for k in ("base_date", "code", "name", "bars_tracked", "max_ret", "min_ret")}
                             for r in rows if r["result"] == "tracking"]
    print(json.dumps(out, ensure_ascii=False, default=str, indent=1))


if __name__ == "__main__":
    main()
