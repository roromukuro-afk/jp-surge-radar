"""サイト(Vercel)。候補の表示と成績のみ。データ処理はローカルの日次タスクが行う。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import queries

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="日本株 急騰レーダー")
if (BASE / "static").exists():
    app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def index(request: Request, date: str | None = None):
    dates = queries.selection_dates()
    d = date or (dates[0] if dates else None)
    sel = queries.selection(d) if d else None
    return templates.TemplateResponse(request, "index.html",
                                      {"dates": dates, "date": d, "sel": sel})


@app.get("/history", response_class=HTMLResponse)
def history(request: Request):
    return templates.TemplateResponse(request, "history.html", {"h": queries.history()})


@app.get("/report", response_class=HTMLResponse)
def report(request: Request, date: str):
    return templates.TemplateResponse(request, "report.html", {"date": date, "rep": queries.report(date)})


@app.get("/healthz")
def healthz():
    return JSONResponse(jsonable_encoder(queries.health()))
