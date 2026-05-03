from datetime import date

from tradingagents.dashboard.automation import (
    normalize_automation_config,
    select_daily_tickers,
    should_skip_today,
)
from tradingagents.dashboard.ledger import PaperLedger


def test_automation_selects_watchlist_and_current_positions(tmp_path):
    ledger = PaperLedger(tmp_path / "ledger.json", price_provider=lambda _: 100.0)
    ledger.apply_decision(
        ticker="MSFT",
        decision="Buy",
        trade_date="2026-05-01",
        run_id="run-1",
    )
    config = normalize_automation_config(
        {"watchlist": ["spy", "qqq"], "include_positions": True}
    )

    assert select_daily_tickers(config, ledger) == ["MSFT", "QQQ", "SPY"]


def test_automation_skips_weekends_when_enabled():
    config = normalize_automation_config({"weekdays_only": True})

    assert should_skip_today(config, date(2026, 5, 2)) is True
    assert should_skip_today(config, date(2026, 5, 4)) is False


def test_automation_config_requires_admin_key_by_default():
    config = normalize_automation_config({})

    assert config["require_openai_admin_key"] is True


def test_automation_defaults_to_balanced_llm_routing():
    config = normalize_automation_config({})
    run_request = config["run_request"]

    assert run_request["quick_llm_provider"] == "ollama"
    assert run_request["quick_fallback_llm_provider"] == "openai"
    assert run_request["critical_llm_provider"] == "openai"
