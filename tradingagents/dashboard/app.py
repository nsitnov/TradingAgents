from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from tradingagents.dashboard.agent_replay import AgentReplayRequest, AgentReplayService
from tradingagents.dashboard.autopilot import AutopilotService
from tradingagents.dashboard.automation import (
    load_automation_config,
    run_daily_once,
    save_automation_config,
)
from tradingagents.dashboard.backtest import BacktestEngine, BacktestRequest
from tradingagents.dashboard.brokers import BrokerError, broker_config_from_env, broker_from_env
from tradingagents.dashboard.costs import (
    apply_openai_cost_baseline,
    cached_openai_costs,
    cost_window_start,
    refresh_openai_costs,
)
from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.llm_eval import LLMEvalRequest, LLMEvalService
from tradingagents.dashboard.monitor import RunRequest, RunStore, event_stream
from tradingagents.dashboard.oms import RiskConfig
from tradingagents.dashboard.progress import ProgressReporter
from tradingagents.dashboard.readiness import ReadinessReporter
from tradingagents.dashboard.scanner import (
    CrossMarketScanner,
    DislocationRequest,
    RSSIngestRequest,
    ScannerEventRequest,
)
from tradingagents.dashboard.scanner_calibration import ScannerCalibrationReporter
from tradingagents.dashboard.scanner_confluence import (
    ConfluenceRequest,
    ScannerConfluenceReviewer,
)
from tradingagents.dashboard.scanner_execution import (
    ScannerExecutionRequest,
    ScannerPaperExecutor,
)
from tradingagents.dashboard.storage import DashboardStorage
from tradingagents.dashboard.validation import validate_backtest_result


STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="TradingAgents Dashboard", version="0.1.0")
storage = DashboardStorage()
ledger = PaperLedger(storage=storage)
ledger.sync_to_storage()
store = RunStore(ledger=ledger, storage=storage)
agent_replay_service = AgentReplayService(storage=storage)
scanner = CrossMarketScanner(storage=storage)
scanner_confluence = ScannerConfluenceReviewer(storage=storage)
scanner_executor = ScannerPaperExecutor(storage=storage, ledger=ledger)
scanner_calibration = ScannerCalibrationReporter(storage=storage)
readiness_reporter = ReadinessReporter(storage=storage)
progress_reporter = ProgressReporter(
    storage=storage,
    scanner_calibration=scanner_calibration,
    readiness=readiness_reporter,
)
llm_eval_service = LLMEvalService(storage=storage)
autopilot_service = AutopilotService(
    storage=storage,
    ledger=ledger,
    scanner=scanner,
    confluence=scanner_confluence,
    executor=scanner_executor,
    calibration=scanner_calibration,
    readiness=readiness_reporter,
)

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


@app.post("/api/backtests")
def create_backtest(request: BacktestRequest):
    try:
        result = BacktestEngine(request.to_config()).run()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    storage.upsert_backtest(result)
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error", "Backtest failed"))
    return result


@app.get("/api/backtests")
def list_backtests(limit: int = Query(default=50, ge=1, le=200)):
    return {"backtests": storage.backtests(limit=limit)}


@app.get("/api/backtests/{backtest_id}")
def get_backtest(backtest_id: str):
    result = storage.backtest_detail(backtest_id)
    if not result:
        raise HTTPException(status_code=404, detail="Backtest not found")
    return result


@app.get("/api/backtests/{backtest_id}/validation")
def get_backtest_validation(backtest_id: str):
    record = storage.backtest_detail(backtest_id)
    if not record:
        raise HTTPException(status_code=404, detail="Backtest not found")
    result = record.get("result", {})
    return result.get("validation") or validate_backtest_result(result)


@app.post("/api/agent-replays")
def create_agent_replay(request: AgentReplayRequest):
    try:
        return agent_replay_service.start(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/agent-replays")
def list_agent_replays(limit: int = Query(default=50, ge=1, le=200)):
    return {"jobs": agent_replay_service.list_jobs(limit=limit)}


@app.get("/api/agent-replays/{job_id}")
def get_agent_replay(job_id: str):
    job = agent_replay_service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Agent replay job not found")
    return job


@app.post("/api/agent-replays/{job_id}/cancel")
def cancel_agent_replay(job_id: str):
    if not agent_replay_service.cancel(job_id):
        raise HTTPException(status_code=404, detail="Active agent replay job not found")
    return {"status": "cancel_requested"}


@app.get("/api/scanner/config")
def scanner_config():
    return scanner.config()


@app.post("/api/scanner/events")
def ingest_scanner_event(request: ScannerEventRequest):
    return scanner.scan_event(request)


@app.post("/api/scanner/rss")
def ingest_scanner_rss(request: RSSIngestRequest):
    try:
        return scanner.ingest_rss(request)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/scanner/events")
def scanner_events(limit: int = Query(default=100, ge=1, le=500)):
    return {"events": scanner.events(limit=limit)}


@app.get("/api/scanner/signals")
def scanner_signals(limit: int = Query(default=100, ge=1, le=500)):
    return {"signals": scanner.signals(limit=limit)}


@app.post("/api/scanner/dislocations/detect")
def detect_scanner_dislocations(request: DislocationRequest):
    try:
        return scanner.detect_dislocations(request)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/scanner/dislocations")
def scanner_dislocations(limit: int = Query(default=100, ge=1, le=500)):
    return {"dislocations": scanner.dislocations(limit=limit)}


@app.post("/api/scanner/confluence/review")
def review_scanner_confluence(request: ConfluenceRequest):
    return scanner_confluence.review(request)


@app.get("/api/scanner/confluence/reviews")
def scanner_confluence_reviews(limit: int = Query(default=100, ge=1, le=500)):
    return {"reviews": scanner_confluence.reviews(limit=limit)}


@app.post("/api/scanner/confluence/execute")
def execute_scanner_confluence(request: ScannerExecutionRequest):
    return scanner_executor.execute(request)


@app.get("/api/scanner/calibration")
def scanner_calibration_report(limit: int = Query(default=250, ge=1, le=1000)):
    return scanner_calibration.report(limit=limit)


@app.get("/api/orders")
def orders(limit: int = Query(default=100, ge=1, le=500)):
    return {"orders": storage.orders(limit=limit)}


@app.get("/api/orders/fills")
def order_fills(limit: int = Query(default=100, ge=1, le=500)):
    return {"fills": storage.fills(limit=limit)}


@app.post("/api/orders/{order_id}/approve")
def approve_order(order_id: str):
    try:
        return store.approve_order(order_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/orders/{order_id}/reject")
def reject_order(order_id: str, payload: dict | None = None):
    try:
        reason = (payload or {}).get("reason") or "Rejected manually"
        return store.reject_order(order_id, reason)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/api/risk/config")
def risk_config():
    return RiskConfig.from_env().as_dict()


@app.get("/api/risk/decisions")
def risk_decisions(limit: int = Query(default=100, ge=1, le=500)):
    return {"risk_decisions": storage.risk_decisions(limit=limit)}


@app.get("/api/audit/events")
def audit_events(limit: int = Query(default=100, ge=1, le=500)):
    return {"audit_events": storage.audit_events(limit=limit)}


@app.get("/api/readiness/metrics")
def readiness_metrics():
    return readiness_reporter.metrics()


@app.get("/api/readiness/stability-gate")
def readiness_stability_gate():
    return readiness_reporter.stability_gate()


@app.get("/api/readiness/postmortems")
def readiness_postmortems(limit: int = Query(default=100, ge=1, le=500)):
    return readiness_reporter.trade_postmortems(limit=limit)


@app.get("/api/progress/weekly")
def weekly_progress():
    return progress_reporter.weekly()


@app.get("/api/progress/history")
def progress_history(weeks: int = Query(default=8, ge=1, le=52)):
    return progress_reporter.history(weeks=weeks)


@app.get("/api/llm-eval/runs")
def llm_eval_runs(limit: int = Query(default=50, ge=1, le=200)):
    return {"runs": llm_eval_service.runs(limit=limit)}


@app.get("/api/llm-eval/runs/{eval_id}")
def llm_eval_run(eval_id: str):
    result = llm_eval_service.detail(eval_id)
    if not result:
        raise HTTPException(status_code=404, detail="LLM evaluation run not found")
    return result


@app.post("/api/llm-eval/run")
def start_llm_eval(request: LLMEvalRequest):
    try:
        result = llm_eval_service.run(request)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    if result.get("status") == "error":
        raise HTTPException(status_code=500, detail=result.get("error", "LLM evaluation failed"))
    return result


@app.get("/api/llm-eval/scorecard")
def llm_eval_scorecard():
    return llm_eval_service.scorecard()


@app.get("/api/autopilot/config")
def autopilot_config():
    return autopilot_service.config()


@app.put("/api/autopilot/config")
def update_autopilot_config(config: dict):
    return autopilot_service.save_config(config)


@app.get("/api/autopilot/status")
def autopilot_status():
    return autopilot_service.status()


@app.get("/api/autopilot/jobs")
def autopilot_jobs(limit: int = Query(default=50, ge=1, le=200)):
    return {"jobs": autopilot_service.jobs(limit=limit)}


@app.post("/api/autopilot/run-now")
def autopilot_run_now():
    try:
        return autopilot_service.run_once(job_type="manual_run_now")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/broker/config")
def broker_config():
    config = broker_config_from_env()
    config["execution_enabled"] = False
    config["execution_note"] = "Broker execution is disabled; platform is paper-ledger only."
    return config


@app.get("/api/broker/positions")
def broker_positions():
    try:
        config = broker_config_from_env()
        if config["broker"] != "alpaca_paper":
            return {"broker": "paper_ledger", "positions": []}
        broker = broker_from_env()
        return {"broker": broker.name, "positions": broker.get_positions()}
    except BrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/broker/orders")
def broker_orders(
    status: str = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        config = broker_config_from_env()
        if config["broker"] != "alpaca_paper":
            return {"broker": "paper_ledger", "orders": []}
        broker = broker_from_env()
        return {"broker": broker.name, "orders": broker.get_orders(status=status, limit=limit)}
    except BrokerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


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
def openai_costs(days: int = Query(default=1, ge=1, le=90)):
    try:
        return refresh_openai_costs(storage, days=days)
    except Exception as exc:
        cached = cached_openai_costs(storage, days=days)
        raw_total = sum(item["amount"] for item in cached)
        totals = apply_openai_cost_baseline(raw_total, days=days)
        return {
            "configured": bool(os.getenv("OPENAI_ADMIN_KEY")),
            "error": str(exc),
            "costs": cached,
            "total": totals["total"],
            "raw_total": totals["raw_total"],
            "baseline": totals["baseline"],
            "baseline_applied_usd": totals["baseline_applied_usd"],
            "period_days": days,
            "period_start": cost_window_start(days),
        }
