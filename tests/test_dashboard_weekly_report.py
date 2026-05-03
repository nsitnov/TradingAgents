from datetime import datetime

import pytest

from tradingagents.dashboard.storage import DashboardStorage
from tradingagents.dashboard.weekly_report import (
    build_weekly_report,
    render_report_text,
    send_report_email,
    weekly_period,
)


def test_weekly_period_starts_on_monday_in_configured_timezone():
    period = weekly_period(
        now=datetime.fromisoformat("2026-05-03T09:00:00+03:00"),
        timezone_name="Europe/Sofia",
    )

    assert period.start.isoformat() == "2026-04-27T00:00:00+03:00"
    assert period.end.isoformat() == "2026-05-03T09:00:00+03:00"


def test_weekly_report_calculates_net_gross_and_trade_summary(tmp_path):
    storage = DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )
    storage.insert_portfolio_snapshot(
        {
            "updated_at": "2026-04-27T00:00:00+03:00",
            "cash": 90_000,
            "market_value": 10_000,
            "equity": 100_000,
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
            "positions": {"SPY": {"quantity": 100, "market_value": 10_000, "unrealized_pnl": 0}},
        },
        run_id="baseline",
    )
    storage.insert_portfolio_snapshot(
        {
            "updated_at": "2026-04-29T12:00:00+03:00",
            "cash": 90_000,
            "market_value": 11_000,
            "equity": 101_000,
            "realized_pnl": 0,
            "unrealized_pnl": 1_000,
            "total_pnl": 1_000,
            "positions": {
                "SPY": {"quantity": 100, "market_value": 11_000, "unrealized_pnl": 1_000}
            },
        },
        run_id="midweek",
    )
    storage.insert_portfolio_snapshot(
        {
            "updated_at": "2026-05-03T08:00:00+03:00",
            "cash": 90_000,
            "market_value": 10_500,
            "equity": 100_500,
            "realized_pnl": 0,
            "unrealized_pnl": 500,
            "total_pnl": 500,
            "positions": {
                "SPY": {"quantity": 100, "market_value": 10_500, "unrealized_pnl": 500}
            },
        },
        run_id="end",
    )
    storage.insert_trade(
        {
            "run_id": "trade-1",
            "ticker": "SPY",
            "decision": "Buy",
            "action": "buy",
            "quantity": 10,
            "price": 100,
            "trade_date": "2026-04-30",
            "created_at": "2026-04-30T09:00:00+03:00",
        }
    )

    report = build_weekly_report(
        storage,
        now=datetime.fromisoformat("2026-05-03T09:00:00+03:00"),
        timezone_name="Europe/Sofia",
    )

    assert report["portfolio"]["net_pnl"] == 500
    assert report["portfolio"]["gross_gain"] == 1_000
    assert report["portfolio"]["gross_loss"] == 500
    assert report["trades"]["buys"] == 1
    assert report["trades"]["buy_notional"] == 1_000
    assert report["positions"][0]["ticker"] == "SPY"
    assert "progress" in report
    assert "overall_status" in report["progress"]
    assert "+$500.00" in render_report_text(report)
    assert "Progress Scorecard" in render_report_text(report)


def test_send_report_email_posts_resend_payload(monkeypatch):
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

    monkeypatch.setattr("tradingagents.dashboard.weekly_report.requests.post", fake_post)
    report = {
        "period": {
            "start": "2026-04-27T00:00:00+03:00",
            "end": "2026-05-03T09:00:00+03:00",
            "timezone": "Europe/Sofia",
            "label": "2026-04-27 to 2026-05-03 09:00 Europe/Sofia",
        },
        "portfolio": {
            "start_equity": 100_000,
            "end_equity": 101_000,
            "net_pnl": 1_000,
            "net_pnl_pct": 1.0,
            "gross_gain": 1_000,
            "gross_loss": 0,
            "realized_pnl_change": 0,
            "unrealized_pnl_change": 1_000,
            "cash": 90_000,
            "market_value": 11_000,
            "total_pnl": 1_000,
            "snapshot_count": 2,
        },
        "trades": {
            "count": 0,
            "buys": 0,
            "sells": 0,
            "holds": 0,
            "tickers": [],
            "buy_notional": 0,
            "sell_notional": 0,
        },
        "positions": [],
        "has_data": True,
    }

    result = send_report_email(
        report,
        api_key="re_test",
        sender="TradingAgents <reports@example.com>",
        recipients=["me@example.com"],
        subject_prefix="Weekly",
    )

    assert result == {"id": "email-1"}
    assert posted["url"] == "https://api.resend.com/emails"
    assert posted["headers"]["Authorization"] == "Bearer re_test"
    assert posted["headers"]["Idempotency-Key"] == (
        "tradingagents-weekly-report-2026-04-27-2026-05-03"
    )
    assert posted["json"]["to"] == ["me@example.com"]
    assert posted["json"]["subject"].startswith("Weekly:")
    assert "Progress Scorecard" in posted["json"]["text"]
    assert "Progress Scorecard" in posted["json"]["html"]


def test_send_report_email_raises_on_resend_error(monkeypatch):
    class Response:
        status_code = 403
        text = "forbidden"

    monkeypatch.setattr(
        "tradingagents.dashboard.weekly_report.requests.post",
        lambda *args, **kwargs: Response(),
    )

    with pytest.raises(RuntimeError, match="Resend email failed"):
        send_report_email(
            {
                "period": {
                    "start": "2026-04-27",
                    "end": "2026-05-03",
                    "label": "period",
                },
                "portfolio": {
                    "start_equity": 0,
                    "end_equity": 0,
                    "net_pnl": 0,
                    "net_pnl_pct": 0,
                    "gross_gain": 0,
                    "gross_loss": 0,
                    "realized_pnl_change": 0,
                    "unrealized_pnl_change": 0,
                    "cash": 0,
                    "market_value": 0,
                },
                "trades": {
                    "count": 0,
                    "buys": 0,
                    "sells": 0,
                    "holds": 0,
                    "buy_notional": 0,
                    "sell_notional": 0,
                },
                "positions": [],
                "has_data": False,
            },
            api_key="re_test",
            sender="TradingAgents <reports@example.com>",
            recipients=["me@example.com"],
        )
