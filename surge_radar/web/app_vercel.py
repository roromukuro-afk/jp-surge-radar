"""
FastAPI app (Vercel/web-only mode).

Same routes as app.py but imports ONLY lightweight modules (no numpy/pandas/sklearn).
ML operations (retrain) are disabled — they run via GitHub Actions.
"""
from __future__ import annotations

import socket
from pathlib import Path

import io

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import os

from .. import db, leak_check, push_notify, queries


def _qr_svg(url: str) -> str:
    try:
        import segno
        buf = io.BytesIO()
        qr = segno.make_qr(url)
        qr.save(buf, kind="svg", scale=4, border=2)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return ""


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


_LAN_IP = _lan_ip()

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
app = FastAPI(title="日本株 急騰レーダー (Surge Radar)")
# StaticFiles はディレクトリが無いと import 時に RuntimeError を投げるため存在確認する
_STATIC_DIR = BASE / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

_SW_PATH = _STATIC_DIR / "sw.js"

CATEGORY_LABELS = {
    "A": "今すぐ買い検討型", "B": "ブレイク確認買い型", "C": "押し目・再点火待ち型",
    "D": "監視候補", "E": "見送り・除外",
}
RESULT_LABELS = {
    "S": "S成功(5日内)", "A": "A成功(10日内)", "B": "B成功(20日内)",
    "near": "惜しい", "fail": "失敗", "danger_fail": "危険失敗", "open": "追跡中",
}

_LAN_URL = f"http://{_LAN_IP}:8012"
_QR_SVG = _qr_svg(_LAN_URL)


def _ctx(request: Request, **kw):
    base = {"request": request, "cat_labels": CATEGORY_LABELS, "result_labels": RESULT_LABELS,
            "latest": queries.latest_run_date(), "overview": queries.overview(),
            "lan_ip": _LAN_IP, "lan_url": _LAN_URL, "qr_svg": _QR_SVG}
    base.update(kw)
    return base


@app.get("/sw.js")
def service_worker():
    content = _SW_PATH.read_text(encoding="utf-8") if _SW_PATH.exists() else ""
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


# NOTE: DB schema is created/migrated by the pipeline (GitHub Actions), not here.
# Running init_db() on every serverless cold start is slow and unnecessary.


@app.get("/", response_class=HTMLResponse)
def index(request: Request, run_date: str | None = None, category: str = "ALL", limit: int = 100):
    actual_run_date = run_date or queries.latest_run_date()
    rows = queries.ranking(actual_run_date, category, limit)
    return templates.TemplateResponse("ranking.html", _ctx(
        request, rows=rows, run_date=actual_run_date,
        category=category, run_dates=queries.run_dates(), limit=limit,
        cat_summary=queries.category_summary(actual_run_date),
        mat_coverage=queries.material_coverage(actual_run_date),
        market_score=(rows[0]["flags"].get("market_score") if rows else None)))


@app.get("/pred/{pid}", response_class=HTMLResponse)
def detail(request: Request, pid: int):
    d = queries.prediction_detail(pid)
    if not d:
        return HTMLResponse("<h1>Not found</h1>", status_code=404)
    return templates.TemplateResponse("detail.html", _ctx(request, p=d))


@app.get("/detail/{code}", response_class=HTMLResponse)
def detail_by_code(request: Request, code: str):
    with db.cursor() as conn:
        r = conn.execute(
            "SELECT id FROM predictions WHERE code=%s ORDER BY run_date DESC LIMIT 1", (code,)
        ).fetchone()
    if not r:
        return HTMLResponse(f"<h1>銘柄 {code} の予測データが見つかりません</h1>", status_code=404)
    return RedirectResponse(f"/pred/{r['id']}")


@app.get("/history", response_class=HTMLResponse)
def history(request: Request, limit: int = 200):
    return templates.TemplateResponse("history.html", _ctx(
        request, rows=queries.history(limit)))


@app.get("/accuracy", response_class=HTMLResponse)
def accuracy(request: Request):
    return templates.TemplateResponse("accuracy.html", _ctx(
        request, stats=queries.accuracy_stats()))


@app.get("/failures", response_class=HTMLResponse)
def failures(request: Request, limit: int = 200):
    return templates.TemplateResponse("failures.html", _ctx(
        request, rows=queries.failure_samples(limit)))


@app.get("/model", response_class=HTMLResponse)
def model_status(request: Request):
    return templates.TemplateResponse("model.html", _ctx(
        request, meta=queries.latest_model_meta(), teacher=queries.teacher_counts()))


@app.get("/logs", response_class=HTMLResponse)
def logs(request: Request, limit: int = 80):
    return templates.TemplateResponse("logs.html", _ctx(
        request, rows=queries.job_logs(limit)))


@app.get("/api/ranking")
def api_ranking(run_date: str | None = None, category: str = "ALL", limit: int = 100):
    # jsonable_encoder で PostgreSQL の datetime/Decimal を JSON 化
    return JSONResponse(jsonable_encoder(queries.ranking(run_date, category, limit)))


@app.get("/api/accuracy")
def api_accuracy():
    return JSONResponse(jsonable_encoder(queries.accuracy_stats()))


@app.get("/data-leak-check", response_class=HTMLResponse)
def data_leak_check_page(request: Request, code: str | None = None):
    report = leak_check.audit(sample_code=code)
    return templates.TemplateResponse("leak_check.html", _ctx(request, report=report, sample_code=code or ""))


@app.get("/api/data-leak-check")
def api_leak_check(code: str | None = None):
    return JSONResponse(leak_check.audit(sample_code=code))


@app.post("/api/retrain")
def api_retrain():
    return JSONResponse({
        "ok": False,
        "error": "再学習はGitHub Actions経由で実行されます。手動実行はRepository Actionsからdaily.ymlをdispatchしてください。",
    }, status_code=503)


@app.get("/api/teacher-status")
def api_teacher_status():
    c = queries.teacher_counts()
    hist = sum(v.get("n", 0) for k, v in c.get("by_source", {}).items()
               if k in ("historical_pos", "historical_neg"))
    return JSONResponse({"total": c.get("_total", 0), "historical": hist,
                         "live_fail": c.get("by_source", {}).get("live_fail", {}).get("n", 0),
                         "live_success": c.get("by_source", {}).get("live_success", {}).get("n", 0)})


@app.get("/healthz")
def healthz():
    return {"ok": True, **queries.overview()}


@app.get("/push/public-key")
def push_public_key():
    return JSONResponse({"key": push_notify.VAPID_PUBLIC_KEY or "",
                         "configured": push_notify.is_configured()})


@app.post("/push/subscribe")
async def push_subscribe(request: Request):
    try:
        data = await request.json()
        endpoint = data.get("endpoint", "")
        keys = data.get("keys", {})
        if not endpoint:
            return JSONResponse({"ok": False, "error": "endpoint required"}, status_code=400)
        ua = request.headers.get("User-Agent", "")[:200]
        with db.cursor() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO push_subscriptions(endpoint,p256dh,auth,user_agent)"
                " VALUES(%s,%s,%s,%s)",
                (endpoint, keys.get("p256dh"), keys.get("auth"), ua),
            )
            conn.execute(
                "UPDATE push_subscriptions SET p256dh=%s,auth=%s,last_used=CURRENT_TIMESTAMP"
                " WHERE endpoint=%s",
                (keys.get("p256dh"), keys.get("auth"), endpoint),
            )
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.delete("/push/subscribe")
async def push_unsubscribe(request: Request):
    try:
        data = await request.json()
        endpoint = data.get("endpoint", "")
        if endpoint:
            with db.cursor() as conn:
                conn.execute("DELETE FROM push_subscriptions WHERE endpoint=%s", (endpoint,))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
