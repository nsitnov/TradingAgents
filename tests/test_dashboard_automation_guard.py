from unittest.mock import patch

from tradingagents.dashboard.automation import run_daily_once, save_automation_config


def test_daily_run_skips_when_cost_guardrail_is_unavailable(tmp_path, monkeypatch):
    config_path = tmp_path / "automation.json"
    save_automation_config(
        {
            "watchlist": ["SPY"],
            "include_positions": False,
            "weekdays_only": False,
            "require_openai_admin_key": False,
        },
        path=config_path,
    )
    monkeypatch.setenv("TRADINGAGENTS_DASHBOARD_ENV", str(tmp_path / "missing.env"))

    with patch(
        "tradingagents.dashboard.automation.today_openai_spend",
        side_effect=RuntimeError("forbidden"),
    ):
        result = run_daily_once(config_path=config_path)

    assert result["status"] == "skipped_cost_guard_unavailable"
