from tradingagents.dashboard.cost_alerts import check_daily_openai_cost_alert
from tradingagents.dashboard.storage import DashboardStorage


def test_daily_cost_alert_sends_once_when_threshold_is_exceeded(tmp_path, monkeypatch):
    storage = DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )
    posted = {}

    class Response:
        status_code = 200
        text = '{"id":"email-1"}'

        def json(self):
            return {"id": "email-1"}

    def fake_post(url, *, headers, json, timeout):
        posted["url"] = url
        posted["headers"] = headers
        posted["json"] = json
        posted["timeout"] = timeout
        return Response()

    monkeypatch.setattr("tradingagents.dashboard.cost_alerts.load_dashboard_env", lambda: None)
    monkeypatch.setattr("tradingagents.dashboard.cost_alerts.today_openai_spend", lambda store: 8.25)
    monkeypatch.setattr("tradingagents.dashboard.cost_alerts.requests.post", fake_post)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("TRADINGAGENTS_REPORT_FROM", "TradingAgents <reports@example.com>")
    monkeypatch.setenv("TRADINGAGENTS_REPORT_TO", "me@example.com")

    result = check_daily_openai_cost_alert(
        storage=storage,
        threshold_usd=5.0,
    )
    duplicate = check_daily_openai_cost_alert(
        storage=storage,
        threshold_usd=5.0,
    )

    assert result["status"] == "sent"
    assert duplicate["status"] == "already_sent"
    assert posted["url"] == "https://api.resend.com/emails"
    assert posted["headers"]["Authorization"] == "Bearer re_test"
    assert posted["headers"]["Idempotency-Key"].startswith("openai-daily-cost-")
    assert posted["json"]["to"] == ["me@example.com"]
    assert "$8.25 / $5.00" in posted["json"]["subject"]


def test_daily_cost_alert_skips_below_threshold(tmp_path, monkeypatch):
    storage = DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )

    monkeypatch.setattr("tradingagents.dashboard.cost_alerts.load_dashboard_env", lambda: None)
    monkeypatch.setattr("tradingagents.dashboard.cost_alerts.today_openai_spend", lambda store: 2.25)

    result = check_daily_openai_cost_alert(
        storage=storage,
        threshold_usd=5.0,
    )

    assert result["status"] == "below_threshold"
    assert result["spend_usd"] == 2.25
