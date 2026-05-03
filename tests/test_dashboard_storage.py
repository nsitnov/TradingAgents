from tradingagents.dashboard.storage import DashboardStorage


def test_storage_persists_runs_events_sections_and_portfolio(tmp_path):
    storage = DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )
    run = {
        "run_id": "run-1",
        "status": "completed",
        "started_at": "2026-05-03T08:00:00+00:00",
        "ended_at": "2026-05-03T08:01:00+00:00",
        "request": {"ticker": "SPY", "analysis_date": "2026-05-03"},
        "stats": {"llm_calls": 1},
        "decision": "Hold",
        "error": None,
    }

    storage.upsert_run(run)
    storage.insert_event(
        {
            "run_id": "run-1",
            "seq": 1,
            "type": "message",
            "created_at": "2026-05-03T08:00:01+00:00",
            "payload": {"content": "hello"},
        }
    )
    storage.upsert_section(
        run_id="run-1",
        ticker="SPY",
        analysis_date="2026-05-03",
        section="market_report",
        title="Market",
        content="Report",
    )
    storage.insert_trade(
        {
            "run_id": "run-1",
            "ticker": "SPY",
            "decision": "Hold",
            "action": "hold",
            "quantity": 0,
            "price": 100,
            "trade_date": "2026-05-03",
            "created_at": "2026-05-03T08:01:00+00:00",
        }
    )
    storage.insert_portfolio_snapshot(
        {
            "updated_at": "2026-05-03T08:01:00+00:00",
            "cash": 100000,
            "market_value": 0,
            "equity": 100000,
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
        },
        run_id="run-1",
    )

    detail = storage.run_detail("run-1")
    assert detail["report_sections"]["market_report"] == "Report"
    assert detail["events"][0]["payload"]["content"] == "hello"
    assert storage.trades()[0]["ticker"] == "SPY"
    assert storage.portfolio_history()[0]["equity"] == 100000

