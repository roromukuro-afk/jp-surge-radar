"""設定。判定条件・ラベル定義はそれぞれ track.py / snapshot.py に置く。"""
from __future__ import annotations

import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    for d in (ROOT / "data", Path(tempfile.gettempdir()) / "surge_radar_data"):
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    return ROOT


DATA_DIR = _data_dir()
CACHE_DIR = DATA_DIR / "cache"
LOG_DIR = DATA_DIR / "logs"
for _d in (CACHE_DIR, LOG_DIR):
    try:
        _d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

TARGET_MARKETS = ["プライム", "スタンダード", "グロース"]
