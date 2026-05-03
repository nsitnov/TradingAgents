import base64

from fastapi.testclient import TestClient

import tradingagents.dashboard.app as dashboard_app
from tradingagents.dashboard.storage import DashboardStorage


app = dashboard_app.app


def _auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_health_does_not_require_dashboard_auth(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_PASSWORD", "secret")
    client = TestClient(app)

    assert client.get("/api/health").status_code == 200


def test_dashboard_requires_basic_auth_when_password_is_set(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_USER", "admin")
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_PASSWORD", "secret")
    client = TestClient(app)

    assert client.get("/").status_code == 401
    assert client.get("/", headers=_auth_header("admin", "secret")).status_code == 200


def test_order_risk_and_audit_endpoints_are_available(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_DASHBOARD_PASSWORD", raising=False)
    client = TestClient(app)

    assert "orders" in client.get("/api/orders").json()
    assert "fills" in client.get("/api/orders/fills").json()
    assert client.get("/api/risk/config").json()["mode"] in {
        "DEMO",
        "PAPER",
        "LIVE_DISABLED",
    }
    assert "risk_decisions" in client.get("/api/risk/decisions").json()
    assert "audit_events" in client.get("/api/audit/events").json()
    assert "orders_total" in client.get("/api/readiness/metrics").json()
    assert (
        client.get("/api/readiness/stability-gate").json()["live_trading_allowed"]
        is False
    )
    assert "postmortems" in client.get("/api/readiness/postmortems").json()
    broker_config = client.get("/api/broker/config").json()
    assert broker_config["broker"]
    assert broker_config["execution_enabled"] is False
    assert "positions" in client.get("/api/broker/positions").json()
    assert "orders" in client.get("/api/broker/orders").json()


def test_autopilot_endpoints_are_available(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_DASHBOARD_PASSWORD", raising=False)

    class StubAutopilot:
        def config(self):
            return {"enabled": True, "paper_trading_enabled": True}

        def save_config(self, config):
            return {**self.config(), **config}

        def status(self):
            return {"enabled": True, "mode": "paper_autopilot"}

        def jobs(self, limit=50):
            return [{"job_id": "auto-1", "status": "completed"}]

        def run_once(self, job_type="run_once"):
            return {
                "job_id": "auto-1",
                "status": "completed",
                "job_type": job_type,
                "result": {"summary": {"paper_executions": 1}},
            }

    monkeypatch.setattr(dashboard_app, "autopilot_service", StubAutopilot())
    client = TestClient(app)

    assert client.get("/api/autopilot/config").json()["enabled"] is True
    assert client.put("/api/autopilot/config", json={"enabled": False}).json()[
        "enabled"
    ] is False
    assert client.get("/api/autopilot/status").json()["mode"] == "paper_autopilot"
    assert client.get("/api/autopilot/jobs").json()["jobs"][0]["job_id"] == "auto-1"
    assert client.post("/api/autopilot/run-now").json()["status"] == "completed"


def test_progress_endpoints_are_available(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_DASHBOARD_PASSWORD", raising=False)

    class StubProgressReporter:
        def weekly(self):
            return {"overall_status": "improving", "score": 82}

        def history(self, weeks=8):
            return {"weeks": [{"overall_status": "improving", "score": 82}]}

    monkeypatch.setattr(dashboard_app, "progress_reporter", StubProgressReporter())
    client = TestClient(app)

    assert client.get("/api/progress/weekly").json()["score"] == 82
    assert client.get("/api/progress/history").json()["weeks"][0]["overall_status"] == "improving"


def test_backtest_endpoints_are_available(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADINGAGENTS_DASHBOARD_PASSWORD", raising=False)
    storage = DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )
    monkeypatch.setattr(dashboard_app, "storage", storage)

    class StubBacktestEngine:
        def __init__(self, config):
            self.config = config

        def run(self):
            return {
                "backtest_id": "bt-api-1",
                "status": "completed",
                "started_at": "2026-01-05T08:00:00+00:00",
                "ended_at": "2026-01-05T08:00:01+00:00",
                "config": self.config.as_dict(),
                "summary": {"trade_count": 0, "total_pnl": 0.0},
                "performance": {},
                "history": [
                    {"created_at": "2026-01-02T23:59:59+00:00", "equity": 100000},
                    {"created_at": "2026-01-05T23:59:59+00:00", "equity": 100100},
                ],
                "trades": [],
            }

    monkeypatch.setattr(dashboard_app, "BacktestEngine", StubBacktestEngine)
    client = TestClient(app)

    response = client.post(
        "/api/backtests",
        json={
            "tickers": ["SPY"],
            "start": "2026-01-02",
            "end": "2026-01-05",
            "fixed_decision": "Hold",
        },
    )

    assert response.status_code == 200
    assert response.json()["backtest_id"] == "bt-api-1"
    assert client.get("/api/backtests").json()["backtests"][0]["backtest_id"] == "bt-api-1"
    assert (
        client.get("/api/backtests/bt-api-1").json()["result"]["summary"]["trade_count"]
        == 0
    )
    assert "monte_carlo" in client.get(
        "/api/backtests/bt-api-1/validation"
    ).json()


def test_agent_replay_endpoints_are_available(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_DASHBOARD_PASSWORD", raising=False)

    class StubAgentReplayService:
        def start(self, request):
            return {
                "job_id": "replay-1",
                "status": "queued",
                "started_at": "2026-01-05T08:00:00+00:00",
                "config": request.as_config(),
                "progress": {"pct_complete": 0.0},
                "result": {},
                "error": None,
            }

        def list_jobs(self, limit=50):
            return [{"job_id": "replay-1", "status": "completed"}]

        def get_job(self, job_id):
            return {"job_id": job_id, "status": "completed", "result": {"summary": {}}}

        def cancel(self, job_id):
            return True

    monkeypatch.setattr(dashboard_app, "agent_replay_service", StubAgentReplayService())
    client = TestClient(app)

    response = client.post(
        "/api/agent-replays",
        json={
            "tickers": ["SPY"],
            "start": "2026-01-05",
            "end": "2026-01-05",
            "decision_provider": "fixed",
            "fixed_decision": "Hold",
            "max_decisions": 5,
        },
    )

    assert response.status_code == 200
    assert response.json()["job_id"] == "replay-1"
    assert client.get("/api/agent-replays").json()["jobs"][0]["job_id"] == "replay-1"
    assert client.get("/api/agent-replays/replay-1").json()["status"] == "completed"
    assert client.post("/api/agent-replays/replay-1/cancel").json()["status"] == "cancel_requested"


def test_scanner_endpoints_are_available(monkeypatch, tmp_path):
    monkeypatch.delenv("TRADINGAGENTS_DASHBOARD_PASSWORD", raising=False)
    storage = DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )
    scanner = dashboard_app.CrossMarketScanner(storage=storage)
    monkeypatch.setattr(dashboard_app, "scanner", scanner)
    client = TestClient(app)

    config = client.get("/api/scanner/config").json()
    assert "rules" in config

    response = client.post(
        "/api/scanner/events",
        json={
            "title": "ASML beats estimates on strong lithography demand",
            "summary": "Semiconductor equipment demand rises.",
            "source": "manual",
            "region": "EU",
            "url": "https://example.test/asml",
            "published_at": "2026-01-05T08:00:00+00:00",
        },
    )

    assert response.status_code == 200
    assert response.json()["signals"]
    assert client.get("/api/scanner/events").json()["events"][0]["title"].startswith("ASML")
    assert client.get("/api/scanner/signals").json()["signals"][0]["us_targets"]

    class StubScanner(scanner.__class__):
        def detect_dislocations(self, request):
            return {
                "dislocations": [
                    {
                        "dislocation_id": "dis-1",
                        "signal_id": "sig-1",
                        "target_symbol": "NVDA",
                        "z_score": 2.1,
                        "is_dislocated": True,
                    }
                ],
                "errors": [],
            }

        def dislocations(self, limit=100):
            return [
                {
                    "dislocation_id": "dis-1",
                    "signal_id": "sig-1",
                    "target_symbol": "NVDA",
                    "z_score": 2.1,
                    "is_dislocated": True,
                }
            ]

    monkeypatch.setattr(dashboard_app, "scanner", StubScanner(storage=storage))
    assert client.post("/api/scanner/dislocations/detect", json={}).json()[
        "dislocations"
    ][0]["is_dislocated"]
    assert client.get("/api/scanner/dislocations").json()["dislocations"][0][
        "target_symbol"
    ] == "NVDA"

    class StubConfluence:
        def review(self, request):
            return {
                "reviews": [
                    {
                        "review_id": "review-1",
                        "status": "paper_candidate",
                        "action": "Buy",
                        "target_symbol": "NVDA",
                    }
                ],
                "errors": [],
            }

        def reviews(self, limit=100):
            return [
                {
                    "review_id": "review-1",
                    "status": "paper_candidate",
                    "action": "Buy",
                    "target_symbol": "NVDA",
                }
            ]

    monkeypatch.setattr(dashboard_app, "scanner_confluence", StubConfluence())
    assert client.post("/api/scanner/confluence/review", json={}).json()["reviews"][
        0
    ]["status"] == "paper_candidate"
    assert client.get("/api/scanner/confluence/reviews").json()["reviews"][0][
        "action"
    ] == "Buy"

    class StubScannerExecutor:
        def execute(self, request):
            return {
                "executions": [
                    {
                        "review_id": "review-1",
                        "target_symbol": "NVDA",
                        "execution": {"status": "filled", "order_id": "order-1"},
                    }
                ],
                "errors": [],
            }

    monkeypatch.setattr(dashboard_app, "scanner_executor", StubScannerExecutor())
    assert client.post("/api/scanner/confluence/execute", json={}).json()[
        "executions"
    ][0]["execution"]["status"] == "filled"

    class StubScannerCalibration:
        def report(self, limit=250):
            return {
                "funnel": {"paper_executions": 1},
                "scanner": {"orders": 1},
                "baseline": {"orders": 0},
                "recommendations": [],
            }

    monkeypatch.setattr(dashboard_app, "scanner_calibration", StubScannerCalibration())
    assert client.get("/api/scanner/calibration").json()["scanner"]["orders"] == 1
