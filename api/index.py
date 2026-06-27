"""Vercel serverless entrypoint.

Exposes the lightweight FastAPI app (no numpy/pandas/sklearn) as `app`.
All routes are rewritten to this function via vercel.json.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from surge_radar.web.app_vercel import app  # noqa: E402

# Vercel's Python runtime detects the ASGI `app` object.
