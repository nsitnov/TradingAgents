from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from tradingagents.dashboard.automation import (
    load_automation_config,
    run_daily_once,
    save_automation_config,
)
from tradingagents.dashboard.costs import refresh_openai_costs
from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.monitor import RunRequest, RunStore, event_stream
from tradingagents.dashboard.storage import DashboardStorage


STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="TradingAgents Dashboard", version="0.1.0")
storage = DashboardStorage()
ledger = PaperLedger(storage=storage)
ledger.sync_to_storage()
store = RunStore(ledger=ledger, storage=storage)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _auth_configured() -> bool:
    return bool(os.getenv("TRADINGAGENTS_DASHBOARD_PASSWORD"))


def _unauthorized() -> Response:
    return Response(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="TradingAgents Dashboard"'},
    )


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if request.url.path == "/api/health" or not _auth_configured():
        return await call_next(request)

    username = os.getenv("TRADINGAGENTS_DASHBOARD_USER", "admin")
    password = os.getenv("TRADINGAGENTS_DASHBOARD_PASSWORD", "")
    credentials = request.headers.get("authorization", "")
    if not credentials.lower().startswith("basic "):
        return _unauthorized()

    import base64

    try:
        decoded = base64.b64decode(credentials.split(" ", 1)[1]).decode("utf-8")
        supplied_user, supplied_password = decoded.split(":", 1)
    except Exception:
        return _unauthorized()

    if not (
        secrets.compare_digest(supplied_user, username)
        and secrets.compare_digest(supplied_password, password)
    ):
        return _unauthorized()

    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/runs")
def start_run(request: RunRequest):
    try:
        run = store.start_run(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return run.snapshot()


@app.get("/api/runs")
def list_runs():
    return {"runs": store.list_runs()}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str, after: int = Query(default=0, ge=0)):
    if not store.get_run(run_id):
        raise HTTPException(status_code=404, detail="Run not found")
    return StreamingResponse(
        event_stream(store, run_id, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str):
    if not store.cancel_run(run_id):
        raise HTTPException(status_code=404, detail="Active run not found")
    return {"status": "cancel_requested"}


@app.get("/api/portfolio")
def portfolio():
    return store.portfolio()


@app.get("/api/portfolio/history")
def portfolio_history():
    return store.portfolio_history()


@app.get("/api/portfolio/trades")
def portfolio_trades():
    return {"trades": storage.trades()}


@app.get("/api/portfolio/performance")
def portfolio_performance():
    return store.portfolio_history()["performance"]


@app.get("/api/automation/config")
def automation_config():
    return load_automation_config()


@app.put("/api/automation/config")
def update_automation_config(config: dict):
    return save_automation_config(config)


@app.post("/api/automation/run-now")
def automation_run_now():
    try:
        return run_daily_once(source="manual_automation")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/automation/history")
def automation_history():
    return {"jobs": storage.daily_jobs()}


@app.get("/api/costs/openai")
def openai_costs(days: int = Query(default=30, ge=1, le=90)):
    try:
        return refresh_openai_costs(storage, days=days)
    except Exception as exc:
        cached = storage.openai_costs()
        return {
            "configured": bool(os.getenv("OPENAI_ADMIN_KEY")),
            "error": str(exc),
            "costs": cached,
            "total": sum(item["amount"] for item in cached),
        }
