from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

from tradingagents.dashboard.costs import today_openai_spend
from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.monitor import RunRequest, RunStore, TERMINAL_STATUSES
from tradingagents.dashboard.storage import DashboardStorage


DEFAULT_AUTOMATION_CONFIG_PATH = (
    Path.home() / ".tradingagents" / "dashboard" / "automation.json"
)
DEFAULT_AUTOMATION_CONFIG = {
    "enabled": True,
    "watchlist": ["SPY", "QQQ", "NVDA", "AAPL", "MSFT"],
    "include_positions": True,
    "weekdays_only": True,
    "daily_openai_budget_usd": 5.0,
    "require_openai_admin_key": True,
    "run_request": {
        "analysts": ["market", "social", "news", "fundamentals"],
        "research_depth": 1,
        "llm_provider": "openai",
        "shallow_thinker": "gpt-5.4-mini",
        "deep_thinker": "gpt-5.4",
        "output_language": "English",
    },
}


def load_automation_config(path: Path = DEFAULT_AUTOMATION_CONFIG_PATH) -> Dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_AUTOMATION_CONFIG))
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    merged = json.loads(json.dumps(DEFAULT_AUTOMATION_CONFIG))
    merged.update(data)
    merged["run_request"].update(data.get("run_request", {}))
    return merged


def save_automation_config(
    config: Dict[str, Any], path: Path = DEFAULT_AUTOMATION_CONFIG_PATH
) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_automation_config(config)
    with path.open("w", encoding="utf-8") as f:
        json.dump(normalized, f, indent=2)
    return normalized


def normalize_automation_config(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = json.loads(json.dumps(DEFAULT_AUTOMATION_CONFIG))
    merged.update(config or {})
    merged["run_request"].update((config or {}).get("run_request", {}))
    merged["watchlist"] = sorted(
        {
            str(ticker).strip().upper()
            for ticker in merged.get("watchlist", [])
            if str(ticker).strip()
        }
    )
    merged["daily_openai_budget_usd"] = float(merged.get("daily_openai_budget_usd", 5.0))
    return merged


def select_daily_tickers(config: Dict[str, Any], ledger: PaperLedger) -> List[str]:
    tickers = {ticker.upper() for ticker in config.get("watchlist", [])}
    if config.get("include_positions", True):
        tickers.update(
            ticker.upper()
            for ticker, position in ledger.snapshot().get("positions", {}).items()
            if float(position.get("quantity", 0.0)) > 0
        )
    return sorted(tickers)


def should_skip_today(config: Dict[str, Any], today: date) -> bool:
    return bool(config.get("weekdays_only", True) and today.weekday() >= 5)


def run_daily_once(
    *,
    config_path: Path = DEFAULT_AUTOMATION_CONFIG_PATH,
    source: str = "daily",
) -> Dict[str, Any]:
    load_dotenv()
    load_dotenv(".env.enterprise", override=False)
    env_path = os.getenv(
        "TRADINGAGENTS_DASHBOARD_ENV",
        "/home/nsitnov/.config/tradingagents-dashboard.env",
    )
    if Path(env_path).exists():
        load_dotenv(env_path, override=False)

    storage = DashboardStorage()
    ledger = PaperLedger(storage=storage)
    ledger.sync_to_storage()
    store = RunStore(ledger=ledger, storage=storage)
    config = load_automation_config(config_path)
    today = date.today()

    if not config.get("enabled", True):
        return {"status": "disabled", "tickers": []}
    if should_skip_today(config, today):
        return {"status": "skipped_weekend", "tickers": []}

    tickers = select_daily_tickers(config, ledger)
    job_id = storage.create_daily_job(today.isoformat(), tickers)
    completed: List[str] = []
    skipped: List[str] = []

    try:
        provider = str(config.get("run_request", {}).get("llm_provider", "")).lower()
        if (
            provider == "openai"
            and config.get("require_openai_admin_key", True)
            and not os.getenv("OPENAI_ADMIN_KEY")
        ):
            message = "OPENAI_ADMIN_KEY is required for automated OpenAI budget guardrails"
            storage.finish_daily_job(job_id, "skipped_missing_openai_admin_key", message)
            return {"status": "skipped_missing_openai_admin_key", "tickers": tickers}

        for ticker in tickers:
            try:
                spend = today_openai_spend(storage)
            except Exception as exc:
                message = f"OpenAI cost guardrail unavailable: {exc}"
                storage.finish_daily_job(job_id, "skipped_cost_guard_unavailable", message)
                return {
                    "status": "skipped_cost_guard_unavailable",
                    "tickers": tickers,
                    "error": message,
                }
            if spend >= float(config["daily_openai_budget_usd"]):
                skipped.append(ticker)
                continue

            request_data = dict(config.get("run_request", {}))
            request_data["ticker"] = ticker
            request_data["analysis_date"] = today.isoformat()
            request = RunRequest(**request_data)
            run = store.start_run(request, source=source)

            while True:
                snapshot = store.get_run(run.run_id)
                if snapshot and snapshot["status"] in TERMINAL_STATUSES:
                    if snapshot["status"] == "completed":
                        completed.append(ticker)
                    else:
                        skipped.append(ticker)
                    break
                time.sleep(1.0)

        status = "completed" if not skipped else "completed_with_skips"
        storage.finish_daily_job(job_id, status)
        return {"status": status, "tickers": tickers, "completed": completed, "skipped": skipped}
    except Exception as exc:
        storage.finish_daily_job(job_id, "error", str(exc))
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Run TradingAgents daily paper automation")
    parser.add_argument("--config", default=str(DEFAULT_AUTOMATION_CONFIG_PATH))
    args = parser.parse_args()
    result = run_daily_once(config_path=Path(args.config))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
