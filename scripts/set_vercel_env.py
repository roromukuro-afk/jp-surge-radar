"""Set Vercel environment variables from .env without printing secret values.

Usage: python scripts/set_vercel_env.py --scope <scope>

Reads DATABASE_URL / VAPID_* from .env (multi-line PEM supported) and pushes
them to the linked Vercel project for production + preview environments.
Values are passed via stdin to `vercel env add` and never echoed.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _envload import load_env

load_env()

scope = None
if "--scope" in sys.argv:
    scope = sys.argv[sys.argv.index("--scope") + 1]

# (name, value, is_secret)
VARS = [
    ("DATABASE_URL", os.environ.get("DATABASE_URL", ""), True),
    ("VAPID_PUBLIC_KEY", os.environ.get("VAPID_PUBLIC_KEY", ""), False),
    ("VAPID_PRIVATE_KEY", os.environ.get("VAPID_PRIVATE_KEY", ""), True),
    ("VAPID_ADMIN_EMAIL", os.environ.get("VAPID_ADMIN_EMAIL", ""), False),
    ("SURGE_ENABLE_LLM", "0", False),
]

ENVIRONMENTS = ["production", "preview"]


_IS_WIN = os.name == "nt"


def run_env_add(name: str, value: str, environment: str) -> bool:
    cmd = ["vercel", "env", "add", name, environment, "--force"]
    if scope:
        cmd += ["--scope", scope]
    # value via stdin (newline-terminated). On Windows vercel is a .cmd,
    # so run through the shell; only controlled tokens are in the command line.
    proc = subprocess.run(
        " ".join(cmd) if _IS_WIN else cmd,
        input=value + "\n", text=True,
        capture_output=True, shell=_IS_WIN,
    )
    ok = proc.returncode == 0
    if not ok:
        # Don't print the value; only stderr (which Vercel keeps value-free)
        print(f"  ! {name}/{environment} failed: {proc.stderr.strip()[:200]}")
    return ok


missing = [n for n, v, _ in VARS if not v]
if missing:
    print(f"MISSING from .env: {missing}")
    sys.exit(2)

for name, value, secret in VARS:
    for environment in ENVIRONMENTS:
        ok = run_env_add(name, value, environment)
        tag = "secret" if secret else "plain"
        print(f"  {'OK' if ok else '..'} {name} [{tag}] -> {environment}")

print("done")
