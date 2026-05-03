from tradingagents.dashboard.autopilot import (
    AutopilotService,
    local_llm_status,
    load_autopilot_config,
    normalize_autopilot_config,
    save_autopilot_config,
)
from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.storage import DashboardStorage


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


class StubScanner:
    def ingest_rss(self, request):
        return {"event_count": 1, "signal_count": 2, "results": []}

    def detect_dislocations(self, request):
        return {"dislocations": [{"dislocation_id": "dis-1"}], "errors": []}


class StubConfluence:
    def review(self, request):
        return {"reviews": [{"review_id": "review-1"}], "errors": []}


class StubExecutor:
    def execute(self, request):
        return {
            "executions": [
                {"review_id": "review-1", "execution": {"status": "filled"}}
            ],
            "errors": [],
        }


class StubCalibration:
    def report(self, limit=250):
        return {
            "funnel": {"paper_candidates": 1, "paper_executions": 1},
            "scanner": {"orders": 1},
            "baseline": {"orders": 0},
            "recommendations": [],
        }


class StubReadiness:
    def stability_gate(self):
        return {
            "status": "blocked",
            "live_trading_allowed": False,
            "conditions": [],
        }


def test_autopilot_config_roundtrip(tmp_path):
    path = tmp_path / "autopilot.json"

    saved = save_autopilot_config(
        {"scanner_rss_sources": [" https://example.test/rss.xml ", ""], "z_threshold": "2.0"},
        path,
    )

    assert saved["scanner_rss_sources"] == ["https://example.test/rss.xml"]
    assert saved["z_threshold"] == 2.0
    assert load_autopilot_config(path)["enabled"] is True
    assert normalize_autopilot_config({})["paper_trading_enabled"] is True
    assert normalize_autopilot_config({})["llm_routing"]["quick_llm_provider"] == "ollama"


def test_local_llm_status_reports_non_local_provider():
    status = local_llm_status({"quick_llm_provider": "openai", "quick_think_llm": "gpt-5.4"})

    assert status["required"] is False
    assert status["provider"] == "openai"


def test_autopilot_run_once_orchestrates_scanner_confluence_and_execution(tmp_path):
    storage = _storage(tmp_path)
    config_path = tmp_path / "autopilot.json"
    save_autopilot_config(
        {"scanner_rss_sources": ["https://example.test/rss.xml"]},
        config_path,
    )
    service = AutopilotService(
        storage=storage,
        ledger=PaperLedger(tmp_path / "ledger.json", price_provider=lambda _: 100.0),
        config_path=config_path,
        scanner=StubScanner(),
        confluence=StubConfluence(),
        executor=StubExecutor(),
        calibration=StubCalibration(),
        readiness=StubReadiness(),
    )

    job = service.run_once()

    assert job["status"] == "completed"
    assert job["result"]["summary"]["scanner_signals"] == 2
    assert job["result"]["summary"]["paper_executions"] == 1
    assert storage.autopilot_jobs()[0]["job_id"] == job["job_id"]
    assert service.status()["latest_job"]["job_id"] == job["job_id"]


def test_autopilot_disabled_records_disabled_job(tmp_path):
    storage = _storage(tmp_path)
    config_path = tmp_path / "autopilot.json"
    save_autopilot_config({"enabled": False}, config_path)
    service = AutopilotService(
        storage=storage,
        ledger=PaperLedger(tmp_path / "ledger.json", price_provider=lambda _: 100.0),
        config_path=config_path,
        scanner=StubScanner(),
        confluence=StubConfluence(),
        executor=StubExecutor(),
        calibration=StubCalibration(),
        readiness=StubReadiness(),
    )

    job = service.run_once()

    assert job["status"] == "disabled"
    assert job["result"]["steps"] == []
