from tradingagents.dashboard.readiness import ReadinessReporter
from tradingagents.dashboard.storage import DashboardStorage


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


def test_readiness_metrics_and_gate_keep_live_disabled(tmp_path, monkeypatch):
    storage = _storage(tmp_path)
    storage.upsert_order(
        {
            "order_id": "order-1",
            "run_id": "scanner-confluence-review-1",
            "ticker": "NVDA",
            "decision": "Buy",
            "action": "buy",
            "status": "filled",
            "quantity": 1,
            "estimated_price": 100,
            "estimated_notional": 100,
            "projected_position_market_value": 100,
            "mode": "PAPER",
            "idempotency_key": "scanner-key",
            "reason": "",
            "trade_date": "2026-01-05",
            "source": "scanner_confluence",
            "created_at": "2026-01-05T08:00:00+00:00",
            "updated_at": "2026-01-05T08:00:00+00:00",
        }
    )
    storage.insert_fill(
        {
            "fill_id": "fill-1",
            "order_id": "order-1",
            "run_id": "scanner-confluence-review-1",
            "ticker": "NVDA",
            "action": "buy",
            "quantity": 1,
            "price": 100,
            "notional": 100,
            "created_at": "2026-01-05T08:00:00+00:00",
        }
    )
    storage.insert_risk_decision(
        "order-1",
        "scanner-confluence-review-1",
        {"status": "approved", "reason": "", "checks": []},
    )
    storage.insert_audit_event("order_filled", "order", "order-1", "run-1", {})
    storage.insert_portfolio_snapshot(
        {
            "cash": 99_900,
            "market_value": 100,
            "equity": 100_000,
            "realized_pnl": 0,
            "unrealized_pnl": 0,
            "total_pnl": 0,
            "updated_at": "2026-01-05T08:00:00+00:00",
        },
        run_id="run-1",
    )
    storage.upsert_scanner_confluence_review(
        {
            "review_id": "review-1",
            "dislocation_id": "dislocation-1",
            "signal_id": "signal-1",
            "event_id": 1,
            "target_symbol": "NVDA",
            "entity": "TSMC",
            "status": "paper_candidate",
            "action": "Buy",
            "total_score": 0.9,
            "execution_status": "filled",
            "execution": {"status": "filled", "order_id": "order-1"},
            "created_at": "2026-01-05T08:00:00+00:00",
        }
    )

    reporter = ReadinessReporter(storage)
    metrics = reporter.metrics()
    gate = reporter.stability_gate()

    assert metrics["orders_total"] == 1
    assert metrics["scanner_paper_executions_total"] == 1
    assert metrics["broker_execution_enabled"] is False
    assert gate["live_trading_allowed"] is False
    assert gate["status"] == "blocked"
    assert any(item["name"] == "paper_history_30_days" for item in gate["conditions"])


def test_readiness_postmortems_close_fifo_trades(tmp_path):
    storage = _storage(tmp_path)
    storage.insert_trade(
        {
            "run_id": "scanner-confluence-review-1",
            "ticker": "NVDA",
            "decision": "Buy",
            "action": "buy",
            "quantity": 2,
            "price": 100,
            "trade_date": "2026-01-05",
            "created_at": "2026-01-05T08:00:00+00:00",
        }
    )
    storage.insert_trade(
        {
            "run_id": "scanner-confluence-review-1",
            "ticker": "NVDA",
            "decision": "Sell",
            "action": "sell",
            "quantity": 1,
            "price": 110,
            "trade_date": "2026-01-06",
            "created_at": "2026-01-06T08:00:00+00:00",
        }
    )

    result = ReadinessReporter(storage).trade_postmortems()

    assert result["summary"]["closed_trades"] == 1
    assert result["summary"]["total_pnl"] == 10
    assert result["postmortems"][0]["source"] == "scanner_confluence"
    assert result["postmortems"][0]["verdict"] == "win"
