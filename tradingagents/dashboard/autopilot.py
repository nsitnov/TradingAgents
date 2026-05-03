from __future__ import annotations

import argparse
import json
import os
import urllib.request
import uuid
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - fallback for minimal CLI environments
    def load_dotenv(*args: Any, **kwargs: Any) -> bool:
        return False

from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.readiness import ReadinessReporter
from tradingagents.dashboard.scanner import (
    CrossMarketScanner,
    DislocationRequest,
    RSSIngestRequest,
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
from tradingagents.dashboard.storage import DashboardStorage, now_iso


DEFAULT_AUTOPILOT_CONFIG_PATH = (
    Path.home() / ".tradingagents" / "dashboard" / "autopilot.json"
)

DEFAULT_AUTOPILOT_CONFIG = {
    "enabled": True,
    "paper_trading_enabled": True,
    "scanner_enabled": True,
    "scanner_rss_sources": ["https://feeds.marketwatch.com/marketwatch/topstories/"],
    "scanner_limit_per_feed": 20,
    "lookback_days": 60,
    "z_threshold": 1.5,
    "min_abs_gap_pct": 0.005,
    "min_confluence_score": 0.65,
    "max_paper_orders_per_cycle": 10,
    "daily_analysis_enabled": False,
    "daily_analysis_source": "autopilot_daily",
    "skip_paper_execution_when_readiness_blocked": False,
    "llm_routing": {
        "mode": "balanced",
        "quick_llm_provider": "ollama",
        "quick_think_llm": "gpt-oss:20b",
        "quick_backend_url": "http://localhost:11434/v1",
        "deep_llm_provider": "openai",
        "deep_think_llm": "gpt-5.4",
        "critical_llm_provider": "openai",
        "critical_think_llm": "gpt-5.4",
    },
}


def load_autopilot_config(path: Path = DEFAULT_AUTOPILOT_CONFIG_PATH) -> Dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_AUTOPILOT_CONFIG))
    with path.open("r", encoding="utf-8") as f:
        return normalize_autopilot_config(json.load(f))


def save_autopilot_config(
    config: Dict[str, Any], path: Path = DEFAULT_AUTOPILOT_CONFIG_PATH
) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_autopilot_config(config)
    with path.open("w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)
    return normalized


def normalize_autopilot_config(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_AUTOPILOT_CONFIG))
    merged.update(config or {})
    merged["scanner_rss_sources"] = [
        str(url).strip()
        for url in merged.get("scanner_rss_sources", [])
        if str(url).strip()
    ]
    merged["scanner_limit_per_feed"] = int(merged.get("scanner_limit_per_feed", 20))
    merged["lookback_days"] = int(merged.get("lookback_days", 60))
    merged["z_threshold"] = float(merged.get("z_threshold", 1.5))
    merged["min_abs_gap_pct"] = float(merged.get("min_abs_gap_pct", 0.005))
    merged["min_confluence_score"] = float(merged.get("min_confluence_score", 0.65))
    merged["max_paper_orders_per_cycle"] = int(
        merged.get("max_paper_orders_per_cycle", 10)
    )
    routing = dict(DEFAULT_AUTOPILOT_CONFIG["llm_routing"])
    routing.update(merged.get("llm_routing") or {})
    merged["llm_routing"] = routing
    return merged


def local_llm_status(routing: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(routing.get("quick_llm_provider", "")).lower()
    model = str(routing.get("quick_think_llm", ""))
    if provider != "ollama":
        return {"provider": provider, "required": False, "available": None, "model": model}
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
        models = [item.get("name") for item in payload.get("models", [])]
        return {
            "provider": provider,
            "required": True,
            "available": model in models,
            "model": model,
            "installed_models": models,
        }
    except Exception as exc:
        return {
            "provider": provider,
            "required": True,
            "available": False,
            "model": model,
            "error": str(exc),
        }


class AutopilotService:
    def __init__(
        self,
        *,
        storage: DashboardStorage,
        ledger: PaperLedger,
        config_path: Path = DEFAULT_AUTOPILOT_CONFIG_PATH,
        scanner: Optional[CrossMarketScanner] = None,
        confluence: Optional[ScannerConfluenceReviewer] = None,
        executor: Optional[ScannerPaperExecutor] = None,
        calibration: Optional[ScannerCalibrationReporter] = None,
        readiness: Optional[ReadinessReporter] = None,
    ) -> None:
        self.storage = storage
        self.ledger = ledger
        self.config_path = Path(config_path)
        self.scanner = scanner or CrossMarketScanner(storage=storage)
        self.confluence = confluence or ScannerConfluenceReviewer(storage=storage)
        self.executor = executor or ScannerPaperExecutor(storage=storage, ledger=ledger)
        self.calibration = calibration or ScannerCalibrationReporter(storage=storage)
        self.readiness = readiness or ReadinessReporter(storage=storage)

    def config(self) -> Dict[str, Any]:
        return load_autopilot_config(self.config_path)

    def save_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        return save_autopilot_config(config, self.config_path)

    def status(self) -> Dict[str, Any]:
        config = self.config()
        jobs = self.storage.autopilot_jobs(limit=10)
        latest = jobs[0] if jobs else None
        readiness = self.readiness.stability_gate()
        return {
            "enabled": bool(config.get("enabled")),
            "mode": "paper_autopilot",
            "paper_trading_enabled": bool(config.get("paper_trading_enabled")),
            "scanner_sources": len(config.get("scanner_rss_sources", [])),
            "llm_routing": config.get("llm_routing", {}),
            "local_llm": local_llm_status(config.get("llm_routing", {})),
            "latest_job": latest,
            "readiness": {
                "status": readiness["status"],
                "live_trading_allowed": readiness["live_trading_allowed"],
            },
        }

    def jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.storage.autopilot_jobs(limit=limit)

    def run_once(
        self,
        *,
        job_type: str = "run_once",
        include_daily: Optional[bool] = None,
    ) -> Dict[str, Any]:
        config = self.config()
        job = {
            "job_id": str(uuid.uuid4()),
            "job_type": job_type,
            "status": "running",
            "started_at": now_iso(),
            "ended_at": None,
            "config": config,
            "result": {},
            "error": None,
        }
        self.storage.upsert_autopilot_job(job)

        try:
            if not config.get("enabled", True):
                result = {"status": "disabled", "steps": []}
            else:
                result = self._run_enabled_cycle(config, include_daily=include_daily)
            job["status"] = result.get("status", "completed")
            job["result"] = result
            job["ended_at"] = now_iso()
            self.storage.upsert_autopilot_job(job)
            return job
        except Exception as exc:
            job["status"] = "error"
            job["error"] = str(exc)
            job["ended_at"] = now_iso()
            self.storage.upsert_autopilot_job(job)
            raise

    def _run_enabled_cycle(
        self, config: Dict[str, Any], *, include_daily: Optional[bool]
    ) -> Dict[str, Any]:
        steps: List[Dict[str, Any]] = []

        if config.get("scanner_enabled", True):
            steps.append(self._scanner_step(config))

        steps.append(self._confluence_step(config))

        readiness = self.readiness.stability_gate()
        allow_execution = bool(config.get("paper_trading_enabled", True))
        if (
            config.get("skip_paper_execution_when_readiness_blocked", False)
            and readiness.get("status") == "blocked"
        ):
            allow_execution = False

        if allow_execution:
            steps.append(self._execution_step(config))
        else:
            steps.append({"step": "paper_execution", "status": "skipped"})

        steps.append({"step": "calibration", "status": "completed", "result": self.calibration.report()})
        steps.append({"step": "readiness", "status": readiness["status"], "result": readiness})

        run_daily = (
            bool(config.get("daily_analysis_enabled", False))
            if include_daily is None
            else include_daily
        )
        if run_daily:
            steps.append(self._daily_step(config))

        errors = [step for step in steps if step.get("status") == "error"]
        return {
            "status": "completed_with_errors" if errors else "completed",
            "steps": steps,
            "summary": _summary_from_steps(steps),
        }

    def _scanner_step(self, config: Dict[str, Any]) -> Dict[str, Any]:
        sources = config.get("scanner_rss_sources", [])
        if not sources:
            return {"step": "scanner_ingest", "status": "skipped", "reason": "No RSS sources configured"}
        try:
            result = self.scanner.ingest_rss(
                RSSIngestRequest(
                    urls=sources,
                    source="autopilot_rss",
                    region="GLOBAL",
                    limit_per_feed=config.get("scanner_limit_per_feed", 20),
                )
            )
            dislocations = self.scanner.detect_dislocations(
                DislocationRequest(
                    lookback_days=config.get("lookback_days", 60),
                    z_threshold=config.get("z_threshold", 1.5),
                    min_abs_gap_pct=config.get("min_abs_gap_pct", 0.005),
                )
            )
            return {
                "step": "scanner_ingest",
                "status": "completed",
                "result": {
                    "events": result.get("event_count", 0),
                    "signals": result.get("signal_count", 0),
                    "dislocations": len(dislocations.get("dislocations", [])),
                    "errors": dislocations.get("errors", []),
                },
            }
        except Exception as exc:
            return {"step": "scanner_ingest", "status": "error", "error": str(exc)}

    def _confluence_step(self, config: Dict[str, Any]) -> Dict[str, Any]:
        result = self.confluence.review(
            ConfluenceRequest(
                limit=50,
                min_z_score=config.get("z_threshold", 1.5),
                min_abs_gap_pct=config.get("min_abs_gap_pct", 0.005),
                min_total_score=config.get("min_confluence_score", 0.65),
            )
        )
        return {
            "step": "confluence",
            "status": "completed",
            "result": {
                "reviews": len(result.get("reviews", [])),
                "errors": result.get("errors", []),
            },
        }

    def _execution_step(self, config: Dict[str, Any]) -> Dict[str, Any]:
        result = self.executor.execute(
            ScannerExecutionRequest(
                limit=config.get("max_paper_orders_per_cycle", 10),
                trade_date=date.today(),
            )
        )
        return {
            "step": "paper_execution",
            "status": "completed",
            "result": {
                "executions": len(result.get("executions", [])),
                "errors": result.get("errors", []),
            },
        }

    def _daily_step(self, config: Dict[str, Any]) -> Dict[str, Any]:
        try:
            from tradingagents.dashboard.automation import run_daily_once

            result = run_daily_once(source=config.get("daily_analysis_source", "autopilot_daily"))
            return {"step": "daily_analysis", "status": result.get("status", "completed"), "result": result}
        except Exception as exc:
            return {"step": "daily_analysis", "status": "error", "error": str(exc)}


def _summary_from_steps(steps: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = {
        "scanner_events": 0,
        "scanner_signals": 0,
        "dislocations": 0,
        "confluence_reviews": 0,
        "paper_executions": 0,
        "errors": 0,
    }
    for step in steps:
        result = step.get("result") or {}
        summary["errors"] += 1 if step.get("status") == "error" else 0
        if step.get("step") == "scanner_ingest":
            summary["scanner_events"] += int(result.get("events", 0))
            summary["scanner_signals"] += int(result.get("signals", 0))
            summary["dislocations"] += int(result.get("dislocations", 0))
        elif step.get("step") == "confluence":
            summary["confluence_reviews"] += int(result.get("reviews", 0))
        elif step.get("step") == "paper_execution":
            summary["paper_executions"] += int(result.get("executions", 0))
    return summary


def _load_env() -> None:
    load_dotenv()
    load_dotenv(".env.enterprise", override=False)
    env_path = os.getenv(
        "TRADINGAGENTS_DASHBOARD_ENV",
        "/home/nsitnov/.config/tradingagents-dashboard.env",
    )
    if Path(env_path).exists():
        load_dotenv(env_path, override=False)


def build_default_service(
    config_path: Path = DEFAULT_AUTOPILOT_CONFIG_PATH,
) -> AutopilotService:
    _load_env()
    storage = DashboardStorage()
    ledger = PaperLedger(storage=storage)
    ledger.sync_to_storage()
    return AutopilotService(storage=storage, ledger=ledger, config_path=config_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run TradingAgents paper autopilot")
    parser.add_argument(
        "command",
        choices=["run-once", "scanner-once", "daily-once", "status"],
        nargs="?",
        default="run-once",
    )
    parser.add_argument("--config", default=str(DEFAULT_AUTOPILOT_CONFIG_PATH))
    args = parser.parse_args()

    service = build_default_service(config_path=Path(args.config))
    if args.command == "status":
        result = service.status()
    elif args.command == "scanner-once":
        result = service.run_once(job_type="scanner_once", include_daily=False)
    elif args.command == "daily-once":
        result = service.run_once(job_type="daily_once", include_daily=True)
    else:
        result = service.run_once()
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
