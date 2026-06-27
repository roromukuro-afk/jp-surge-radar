"""Load .env into os.environ, with multi-line PEM (VAPID_PRIVATE_KEY) support.

Never prints values. Safe to import from any script.
"""
import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def load_env(path: str | None = None) -> None:
    env_file = Path(path) if path else (_ROOT / ".env")
    if not env_file.exists():
        return
    text = env_file.read_text(encoding="utf-8")
    in_block = False
    key_name = None
    block_lines: list[str] = []
    result: dict[str, str] = {}
    for line in text.splitlines():
        if in_block:
            block_lines.append(line)
            if "-----END" in line:
                result[key_name] = "\n".join(block_lines)
                in_block = False
                block_lines = []
                key_name = None
        elif "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip()
            if "-----BEGIN" in v:
                key_name = k
                block_lines = [v]
                in_block = True
            else:
                result[k] = v
    for k, v in result.items():
        if k not in os.environ:
            os.environ[k] = v
