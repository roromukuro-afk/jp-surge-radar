"""Print recent job_logs from PostgreSQL. Used by GitHub Actions."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from surge_radar import db
    with db.cursor() as c:
        rows = c.execute(
            "SELECT job,started_at,status,message FROM job_logs ORDER BY id DESC LIMIT 10"
        ).fetchall()
    for r in rows:
        print(r["job"], r["started_at"], r["status"], (r["message"] or "")[:200])
except Exception as e:
    print("log fetch failed:", e)
