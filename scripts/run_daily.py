"""Entry point for daily pipeline in GitHub Actions.

Usage: python scripts/run_daily.py [limit] [skip_materials]
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

limit_str = sys.argv[1].strip() if len(sys.argv) > 1 else ""
skip_mat_str = sys.argv[2].strip().lower() if len(sys.argv) > 2 else "false"
skip_uni_str = sys.argv[3].strip().lower() if len(sys.argv) > 3 else "false"

limit = int(limit_str) if limit_str.isdigit() else None
skip_materials = skip_mat_str == "true"
skip_universe = skip_uni_str == "true"

print(f"daily pipeline: limit={limit} skip_materials={skip_materials} skip_universe={skip_universe}", flush=True)

from surge_radar import pipeline
result = pipeline.run_daily(
    limit=limit,
    skip_materials=skip_materials,
    material_max_pages=5,
    material_time_limit=120.0,
    update_universe=not skip_universe,
)
import json
print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
