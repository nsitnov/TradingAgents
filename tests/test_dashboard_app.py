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
    broker_config = client.get("/api/broker/config").json()
    assert broker_config["broker"]
    assert broker_config["execution_enabled"] is False
    assert "positions" in client.get("/api/broker/positions").json()
    assert "orders" in client.get("/api/broker/orders").json()


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
