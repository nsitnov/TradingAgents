from tradingagents.dashboard.scanner_calibration import ScannerCalibrationReporter
from tradingagents.dashboard.storage import DashboardStorage


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


def test_scanner_calibration_report_splits_scanner_and_baseline_orders(tmp_path):
    storage = _storage(tmp_path)
    storage.upsert_scanner_event(
        {
            "event_hash": "event-1",
            "source": "manual",
            "region": "ASIA",
            "title": "TSMC raises guidance",
            "summary": "Strong AI chip demand.",
            "url": "",
            "published_at": "2026-01-05T08:00:00+00:00",
            "language": "en",
            "created_at": "2026-01-05T08:00:00+00:00",
        }
    )
    storage.upsert_scanner_signal(
        {
            "signal_id": "signal-1",
            "event_id": 1,
            "event_hash": "event-1",
            "entity": "TSMC",
            "region": "ASIA",
            "category": "semiconductors",
            "us_targets": ["NVDA"],
            "direction": "bullish",
            "score": 0.85,
            "confidence": 0.75,
            "reason": "Matched TSMC",
            "created_at": "2026-01-05T08:00:01+00:00",
        }
    )
    storage.upsert_scanner_dislocation(
        {
            "dislocation_id": "dislocation-1",
            "signal_id": "signal-1",
            "event_id": 1,
            "entity": "TSMC",
            "reference_symbol": "2330.TW",
            "target_symbol": "NVDA",
            "reference_move_pct": 0.08,
            "target_move_pct": 0.01,
            "gap_pct": 0.07,
            "z_score": 2.5,
            "spread_mean": 0.0,
            "spread_std": 0.02,
            "lookback_days": 20,
            "is_dislocated": True,
            "direction": "target_lagging_upside",
            "created_at": "2026-01-05T08:00:02+00:00",
        }
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
            "execution": {"status": "filled", "order_id": "order-scanner"},
            "created_at": "2026-01-05T08:00:03+00:00",
        }
    )
    storage.upsert_order(
        {
            "order_id": "order-scanner",
            "run_id": "scanner-confluence-review-1",
            "ticker": "NVDA",
            "decision": "Buy",
            "action": "buy",
            "status": "filled",
            "quantity": 2,
            "estimated_price": 100,
            "estimated_notional": 200,
            "projected_position_market_value": 200,
            "mode": "PAPER",
            "idempotency_key": "scanner-key",
            "reason": "",
            "trade_date": "2026-01-05",
            "source": "scanner_confluence",
            "created_at": "2026-01-05T08:00:04+00:00",
            "updated_at": "2026-01-05T08:00:04+00:00",
        }
    )
    storage.upsert_order(
        {
            "order_id": "order-agent",
            "run_id": "agent-run-1",
            "ticker": "SPY",
            "decision": "Buy",
            "action": "buy",
            "status": "filled",
            "quantity": 1,
            "estimated_price": 100,
            "estimated_notional": 100,
            "projected_position_market_value": 100,
            "mode": "PAPER",
            "idempotency_key": "agent-key",
            "reason": "",
            "trade_date": "2026-01-05",
            "source": "manual",
            "created_at": "2026-01-05T08:00:05+00:00",
            "updated_at": "2026-01-05T08:00:05+00:00",
        }
    )
    storage.insert_fill(
        {
            "fill_id": "fill-scanner",
            "order_id": "order-scanner",
            "run_id": "scanner-confluence-review-1",
            "ticker": "NVDA",
            "action": "buy",
            "quantity": 2,
            "price": 100,
            "notional": 200,
            "created_at": "2026-01-05T08:00:06+00:00",
        }
    )
    storage.insert_trade(
        {
            "run_id": "scanner-confluence-review-1",
            "ticker": "NVDA",
            "decision": "Buy",
            "action": "buy",
            "quantity": 2,
            "price": 100,
            "trade_date": "2026-01-05",
            "created_at": "2026-01-05T08:00:06+00:00",
        }
    )

    report = ScannerCalibrationReporter(storage).report()

    assert report["funnel"]["events"] == 1
    assert report["funnel"]["execution_per_candidate"] == 1.0
    assert report["scanner"]["orders"] == 1
    assert report["scanner"]["filled_notional"] == 200
    assert report["baseline"]["orders"] == 1
    assert report["review_quality"]["average_score"] == 0.9
    assert report["recommendations"]
