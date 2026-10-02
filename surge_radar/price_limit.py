"""
東証の制限値幅(1 日に動ける上限・下限)。候補の 20% 経路が翌営業日に届くかを機械的に示すために使う。

表は東証の通常の制限値幅(基準値段 → 値幅)。ストップ高が続いたときの値幅の拡大など、
特別な扱いは含めない(表示するときにその旨を書く)。
"""
from __future__ import annotations

# (基準値段がこの値未満, 制限値幅)
_TABLE = [
    (100, 30), (200, 50), (500, 80), (700, 100), (1000, 150), (1500, 300), (2000, 400),
    (3000, 500), (5000, 700), (7000, 1000), (10000, 1500), (15000, 3000), (20000, 4000),
    (30000, 5000), (50000, 7000), (70000, 10000), (100000, 15000),
]


def limit_width(base: float) -> int:
    for upper, width in _TABLE:
        if base < upper:
            return width
    raise ValueError(f"基準値段 {base} は表の範囲外")


def path_to_target(p0: float, target_ret: float = 0.2) -> dict:
    """翌営業日・翌々営業日の上限と Target を並べる(ストップ高で引け続けた場合の最大)。"""
    target = p0 * (1 + target_ret)
    up1 = p0 + limit_width(p0)
    up2 = up1 + limit_width(up1)
    return {
        "p0": p0, "target": round(target, 1),
        "next_day_upper": up1, "reachable_next_day": up1 >= target,
        "day2_upper_if_limit_up_close": up2, "reachable_by_day2": up2 >= target,
        "note": "東証の通常の制限値幅による。値幅の拡大など特別な扱いは含めない。"
                "2 日目は 1 日目がストップ高で引けた場合の最大",
    }
