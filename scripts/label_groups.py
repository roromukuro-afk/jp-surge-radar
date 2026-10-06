"""
候補をラベルで分類して、分類ごとの成否を見る(教師データの振り返りの補助。2026-09-29 追加)。

  1. ラベル別: 各ラベルを持つ候補の数と、成功・失敗・判定中・判定未確認の内訳
  2. グループ別: ラベルの重なりだけで候補をまとめ(教師なしのクラスタリング。成否は使わない)、
     グループごとの共通ラベルと成否の内訳

判定期間中の候補も数に入れて別枠で示す。成功は到達した時点で決まり、失敗は 10 営業日後にしか
決まらないので、判定中を除いた成功率は高めに出る。そのため成功率は
「下限 = 成功 ÷ 判定未確認を除いた全候補」「上限 = (成功 + 判定中) ÷ 同じ分母」の範囲で出す。

グループ分けの決まり(成否を見て調整しない。変えるときは日付と理由を残す):
  - 使うラベル: チャート・出来高の機械ラベル、材料の状態(あり/なし/確認不能)、材料イベントの種別
    (Claude が文章で付けたチャートの形とルートは使わない。語彙が固定されていないため/ラベルから決まるため)
  - 距離: 2 候補のラベル集合の Jaccard 距離(1 − 共通 ÷ 和集合)
  - まとめ方: 平均連結の階層クラスタリング、距離 0.6 未満でまとめる

使い方: python scripts/label_groups.py [--min-count 2]
"""
from __future__ import annotations

import argparse

import _boot  # noqa: F401

from review import load

RESULT_JA = {"success": "成功", "failure": "失敗", "tracking": "判定中", "unverified": "判定未確認"}
DISTANCE_THRESHOLD = 0.6


def cluster_features(r: dict) -> frozenset[str]:
    """グループ分けに使うラベル。"""
    keep = {f for f in r["feats"]
            if not f.startswith(("形:", "ルート:", "材料段階:", "材料主体:", "材料経路:", "選定モデル:"))}
    return frozenset(keep)


def jaccard_distance(a: frozenset, b: frozenset) -> float:
    u = a | b
    return 1.0 - (len(a & b) / len(u)) if u else 0.0


def clusters(sets: list[frozenset], threshold: float = DISTANCE_THRESHOLD) -> list[int]:
    """平均連結の階層クラスタリング。各要素のグループ番号(0 始まり、大きいグループから)を返す。"""
    n = len(sets)
    if n <= 1:
        return [0] * n
    from sklearn.cluster import AgglomerativeClustering
    import numpy as np
    d = np.array([[jaccard_distance(a, b) for b in sets] for a in sets])
    labels = AgglomerativeClustering(n_clusters=None, metric="precomputed", linkage="average",
                                     distance_threshold=threshold).fit_predict(d)
    order = sorted(set(labels), key=lambda g: (-list(labels).count(g), list(labels).index(g)))
    remap = {g: i for i, g in enumerate(order)}
    return [remap[g] for g in labels]


def tally(rows: list[dict]) -> dict[str, int]:
    t = {k: 0 for k in RESULT_JA}
    for r in rows:
        t[r["result"]] += 1
    return t


def rate_range(t: dict[str, int]) -> str:
    denom = t["success"] + t["failure"] + t["tracking"]
    if not denom:
        return "—"
    lo, hi = t["success"] / denom, (t["success"] + t["tracking"]) / denom
    return f"{lo:.0%}" if lo == hi else f"{lo:.0%}〜{hi:.0%}"


def fmt_tally(t: dict[str, int]) -> str:
    parts = [f"{RESULT_JA[k]}{v}" for k, v in t.items() if v]
    return "・".join(parts) or "なし"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-count", type=int, default=2, help="ラベル別の表は、この件数以上の候補に出たラベルだけ")
    a = ap.parse_args()

    rows = load()
    if not rows:
        print("候補がまだ無い")
        return
    t = tally(rows)
    print(f"# 候補 {len(rows)} 件: {fmt_tally(t)}")
    print(f"# 成功率の範囲(判定中がすべて失敗なら下限、すべて成功なら上限): {rate_range(t)}")
    print("# 件数が少ないうちは偏りが大きい。1〜2 件の差を法則として扱わない\n")

    print("## 1. ラベル別")
    print("ラベル\t候補数\t内訳\t成功率の範囲")
    feats = sorted({f for r in rows for f in r["feats"] if not f.startswith("ルート:")})
    table = []
    for f in feats:
        has = [r for r in rows if f in r["feats"]]
        if len(has) >= a.min_count:
            table.append((f, has))
    for f, has in sorted(table, key=lambda x: (-len(x[1]), x[0])):
        th = tally(has)
        print(f"{f}\t{len(has)}\t{fmt_tally(th)}\t{rate_range(th)}")

    print("\n## 2. ラベルの重なりによるグループ(成否は使わずにまとめた)")
    sets = [cluster_features(r) for r in rows]
    g = clusters(sets)
    for gi in range(max(g) + 1):
        mem = [r for r, x in zip(rows, g) if x == gi]
        ms = [s for s, x in zip(sets, g) if x == gi]
        tg = tally(mem)
        common = sorted(frozenset.intersection(*ms)) if ms else []
        half = sorted(f for f in frozenset.union(*ms) if f not in common
                      and sum(f in s for s in ms) * 2 >= len(ms)) if len(ms) > 1 else []
        print(f"\n### グループ {gi + 1}({len(mem)} 件: {fmt_tally(tg)}、成功率の範囲 {rate_range(tg)})")
        for r in mem:
            extra = f" {r['hit_day']}日目" if r["result"] == "success" and r.get("hit_day") else ""
            mx = f" 最高{r['max_ret']:+.1%}" if r.get("max_ret") is not None else ""
            print(f"- {r['base_date']} {r['code']} {r['name'] or ''}: {RESULT_JA[r['result']]}{extra}{mx}")
        print(f"- 全員に共通: {'、'.join(common) or 'なし'}")
        if half:
            print(f"- 半数以上に出る: {'、'.join(half)}")


if __name__ == "__main__":
    main()
