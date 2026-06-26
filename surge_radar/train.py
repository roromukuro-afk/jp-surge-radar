"""
再学習オーケストレーション。

教師データ(過去急騰の正例/負例 + ライブの成功/失敗)からモデルを再学習。
ライブ失敗教師が増えるほど精度が改善する設計。
"""
from __future__ import annotations

from . import model, teacher


def ensure_historical(min_total: int = 200, **kwargs) -> dict:
    """
    教師データが少なければ教師データ数を返すだけ(build は手動 seed-teacher コマンドで)。
    日次パイプラインで何時間もブロックしないよう、build_historical は呼ばない。
    """
    c = teacher.counts()
    total = c.get("_total", 0)
    hist_total = sum(
        v.get("n", 0) for k, v in c.get("by_source", {}).items()
        if k in ("historical_pos", "historical_neg")
    )
    return {
        "built": False,
        "total": total,
        "historical": hist_total,
        "live": total - hist_total,
        "sufficient": total >= min_total,
        "note": "" if total >= min_total else f"Run: python -m surge_radar.cli seed-teacher --step 5 --max-per-code 40",
    }


def retrain(notes: str = "", min_new_samples: int = 20) -> dict:
    """
    再学習。前回モデルのサンプル数から min_new_samples 以上増えていない場合はスキップ。
    live_fail が増えた日や seed-teacher 完了後は必ずフル再学習する。
    """
    c = teacher.counts()
    total = c.get("_total", 0)
    meta = model.latest_meta()
    prev_n = meta.get("n_samples", 0) if meta else 0
    if prev_n > 0 and (total - prev_n) < min_new_samples:
        return {
            "trained": False,
            "skipped": True,
            "reason": f"新規サンプル{total - prev_n}件 < 閾値{min_new_samples}件 (前回{prev_n}件→現在{total}件)",
            "n_samples": total,
        }
    return model.train(notes=notes)
