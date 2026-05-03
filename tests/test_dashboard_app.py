import base64

from fastapi.testclient import TestClient

from tradingagents.dashboard.app import app


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
    assert client.get("/api/broker/config").json()["broker"]
    assert "positions" in client.get("/api/broker/positions").json()
    assert "orders" in client.get("/api/broker/orders").json()
