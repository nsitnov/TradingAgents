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
    order = {
        "order_id": "order-1",
        "run_id": "run-1",
        "ticker": "SPY",
        "decision": "Buy",
        "action": "buy",
        "status": "filled",
        "quantity": 10,
        "estimated_price": 100,
        "estimated_notional": 1000,
        "projected_position_market_value": 1000,
        "mode": "PAPER",
        "idempotency_key": "run-1:SPY:Buy:2026-05-03:agent",
        "reason": "",
        "trade_date": "2026-05-03",
        "source": "agent",
        "created_at": "2026-05-03T08:01:00+00:00",
        "updated_at": "2026-05-03T08:01:00+00:00",
    }
    storage.upsert_order(order)
    storage.insert_fill(
        {
            "fill_id": "fill-1",
            "order_id": "order-1",
            "run_id": "run-1",
            "ticker": "SPY",
            "action": "buy",
            "quantity": 10,
            "price": 100,
            "notional": 1000,
            "created_at": "2026-05-03T08:01:00+00:00",
        }
    )
    storage.insert_risk_decision(
        "order-1",
        "run-1",
        {"status": "approved", "reason": "", "checks": [{"name": "mode", "passed": True}]},
    )
    storage.insert_audit_event(
        "order_filled",
        "order",
        "order-1",
        "run-1",
        {"order_id": "order-1"},
    )

    detail = storage.run_detail("run-1")
    assert detail["report_sections"]["market_report"] == "Report"
    assert detail["events"][0]["payload"]["content"] == "hello"
    assert storage.trades()[0]["ticker"] == "SPY"
    assert storage.portfolio_history()[0]["equity"] == 100000
    assert storage.orders()[0]["status"] == "filled"
    assert storage.fills()[0]["notional"] == 1000
    assert storage.risk_decisions()[0]["status"] == "approved"
    assert storage.audit_events()[0]["event_type"] == "order_filled"
