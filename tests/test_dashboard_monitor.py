from tradingagents.dashboard.monitor import RunAccumulator, RunRequest
import pytest


def test_run_request_orders_and_filters_analysts():
    request = RunRequest(
        ticker=" spy ",
        analysts=["fundamentals", "market"],
    )

    assert request.ticker == "SPY"
    assert request.analysts == ["market", "fundamentals"]


def test_accumulator_initializes_selected_agents_and_reports():
    accumulator = RunAccumulator(["market", "news"])

    assert "Market Analyst" in accumulator.agent_status
    assert "News Analyst" in accumulator.agent_status
    assert "Social Analyst" not in accumulator.agent_status
    assert "market_report" in accumulator.report_sections
    assert "news_report" in accumulator.report_sections
    assert "sentiment_report" not in accumulator.report_sections


def test_run_request_rejects_empty_analysts():
    with pytest.raises(ValueError):
        RunRequest(analysts=[])
