from datetime import date

from tradingagents.dashboard.costs import (
    day_start_epoch,
    parse_costs_payload,
    refresh_openai_costs,
    save_openai_cost_baseline,
    today_openai_spend,
)
from tradingagents.dashboard.storage import DashboardStorage


def test_parse_openai_costs_payload():
    costs = parse_costs_payload(
        {
            "data": [
                {
                    "start_time": 1777776000,
                    "end_time": 1777862400,
                    "results": [
                        {
                            "amount": {"value": 1.25, "currency": "usd"},
                            "line_item": "Responses API",
                            "project_id": "proj_123",
                        }
                    ],
                }
            ]
        }
    )

    assert costs == [
        {
            "start_time": 1777776000,
            "end_time": 1777862400,
            "amount": 1.25,
            "currency": "usd",
            "line_item": "Responses API",
            "project_id": "proj_123",
            "payload": {
                "amount": {"value": 1.25, "currency": "usd"},
                "line_item": "Responses API",
                "project_id": "proj_123",
            },
        }
    ]


def test_today_cost_uses_local_baseline(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "TRADINGAGENTS_OPENAI_COST_BASELINE_PATH",
        str(tmp_path / "baseline.json"),
    )
    storage = DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )
    storage.replace_openai_costs(
        [
            {
                "start_time": day_start_epoch(date.today()),
                "end_time": day_start_epoch(date.today()) + 86400,
                "amount": 10.0,
                "currency": "usd",
            }
        ]
    )
    save_openai_cost_baseline(baseline_spend_usd=8.0)

    assert today_openai_spend(storage) == 2.0
    result = refresh_openai_costs(storage, days=1)

    assert result["raw_total"] == 10.0
    assert result["baseline_applied_usd"] == 8.0
    assert result["total"] == 2.0
