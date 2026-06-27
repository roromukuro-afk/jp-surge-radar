"""Print pipeline summary from PostgreSQL. Used by GitHub Actions."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from surge_radar import queries
    ov = queries.overview()
    print("=== Pipeline Summary ===")
    print(f"Materials: {ov['materials']} total, {ov['mat_codes_today']} codes today")
    print(f"Mat by source: {ov.get('mat_by_source', {})}")
    print(f"Teacher: {ov['teacher_total']} total, live_fail={ov['live_fail']}")
    print(f"Today predictions: {ov['cat_today']}")
    print(f"Latest run date: {ov.get('latest_run_date', 'N/A')}")
except Exception as e:
    print("summary failed:", e)
