"""Diagnostic: report DB row counts to size the pipeline workload. No secrets printed."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _envload import load_env
load_env()

from surge_radar import db

t0 = time.monotonic()
with db.cursor() as conn:
    def one(q):
        return conn.execute(q).fetchone()
    print("DB mode:", "postgres" if db.DATABASE_URL else "sqlite")
    print("securities:", one("SELECT COUNT(*) n FROM securities")["n"])
    print("prices rows:", one("SELECT COUNT(*) n FROM prices")["n"])
    print("priced codes:", one("SELECT COUNT(DISTINCT code) n FROM prices")["n"])
    print("predictions total:", one("SELECT COUNT(*) n FROM predictions")["n"])
    print("predictions OPEN:", one("SELECT COUNT(*) n FROM predictions WHERE status='open'")["n"])
    print("predictions judged:", one("SELECT COUNT(*) n FROM predictions WHERE status='judged'")["n"])
    print("prediction_outcomes:", one("SELECT COUNT(*) n FROM prediction_outcomes")["n"])
    print("materials:", one("SELECT COUNT(*) n FROM materials")["n"])
    print("teacher_samples:", one("SELECT COUNT(*) n FROM teacher_samples")["n"])
    print("model_meta rows:", one("SELECT COUNT(*) n FROM model_meta")["n"])
    # open predictions by run_date range
    r = one("SELECT MIN(run_date) lo, MAX(run_date) hi FROM predictions WHERE status='open'")
    print("open run_date range:", r["lo"], "->", r["hi"])
print(f"(1 connection, {time.monotonic()-t0:.2f}s)")

# Measure pooled cursor() overhead with 20 sequential calls
t0 = time.monotonic()
for i in range(20):
    with db.cursor() as conn:
        conn.execute("SELECT 1").fetchone()
dt = time.monotonic() - t0
print(f"20 pooled db.cursor() calls: {dt:.2f}s  ({dt/20*1000:.0f}ms each)")
