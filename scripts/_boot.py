"""全スクリプトの先頭で import する: リポジトリ直下を sys.path に通し、.env を読み、
標準出力を UTF-8 にする。surge_radar.db は import 時に DATABASE_URL を読むので、
これより先に surge_radar を import しないこと。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from _envload import load_env  # noqa: E402

load_env()
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="backslashreplace")
    except Exception:
        pass
