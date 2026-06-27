"""Vercel serverless entrypoint (@vercel/python ASGI app).

Exposes the lightweight FastAPI app (no numpy/pandas/sklearn) as `app`.
vercel.json routes all paths here and bundles surge_radar/** via includeFiles.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from surge_radar.web.app_vercel import app  # noqa: E402,F401
