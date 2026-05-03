from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.oms import RiskConfig
from tradingagents.dashboard.scanner_execution import (
    ScannerExecutionRequest,
    ScannerPaperExecutor,
)
from tradingagents.dashboard.storage import DashboardStorage


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


def _seed_candidate(storage):
    review = {
        "review_id": "review-1",
        "dislocation_id": "dislocation-1",
        "signal_id": "signal-1",
        "event_id": 1,
        "target_symbol": "NVDA",
        "entity": "TSMC",
        "status": "paper_candidate",
        "action": "Buy",
        "total_score": 0.9,
        "agent_reviews": [],
        "candidate": {
            "ticker": "NVDA",
            "action": "Buy",
            "source": "scanner_confluence",
            "paper_only": True,
            "reason": "TSMC target_lagging_upside z=2.50",
        },
        "created_at": "2026-01-05T08:00:00+00:00",
    }
    storage.upsert_scanner_confluence_review(review)
    return review


def test_scanner_paper_executor_fills_candidate_order(tmp_path):
    storage = _storage(tmp_path)
    review = _seed_candidate(storage)
    ledger = PaperLedger(
        tmp_path / "ledger.json",
        price_provider=lambda _: 100.0,
        storage=storage,
    )
    executor = ScannerPaperExecutor(
        storage=storage,
        ledger=ledger,
        risk_config=RiskConfig(
            mode="PAPER",
            max_position_pct=0.50,
            max_trade_notional=25_000,
        ),
    )

    result = executor.execute(
        ScannerExecutionRequest(review_ids=[review["review_id"]])
    )

    assert result["errors"] == []
    execution = result["executions"][0]
    assert execution["order"]["status"] == "filled"
    assert execution["order"]["source"] == "scanner_confluence"
    assert execution["order"]["run_id"] == "scanner-confluence-review-1"
    assert storage.fills()[0]["ticker"] == "NVDA"
    assert storage.scanner_confluence_reviews()[0]["execution_status"] == "filled"


def test_scanner_paper_executor_skips_already_executed_candidate(tmp_path):
    storage = _storage(tmp_path)
    review = _seed_candidate(storage)
    review["execution"] = {"status": "filled", "order_id": "order-1"}
    review["execution_status"] = "filled"
    storage.upsert_scanner_confluence_review(review)
    ledger = PaperLedger(
        tmp_path / "ledger.json",
        price_provider=lambda _: 100.0,
        storage=storage,
    )

    result = ScannerPaperExecutor(storage=storage, ledger=ledger).execute(
        ScannerExecutionRequest(review_ids=[review["review_id"]])
    )

    assert result["executions"][0]["execution"]["status"] == "skipped"
    assert storage.orders() == []


def test_scanner_paper_executor_skips_non_candidate(tmp_path):
    storage = _storage(tmp_path)
    review = _seed_candidate(storage)
    review["status"] = "watch"
    storage.upsert_scanner_confluence_review(review)
    ledger = PaperLedger(
        tmp_path / "ledger.json",
        price_provider=lambda _: 100.0,
        storage=storage,
    )

    result = ScannerPaperExecutor(storage=storage, ledger=ledger).execute(
        ScannerExecutionRequest(review_ids=[review["review_id"]])
    )

    assert result["executions"][0]["execution"]["status"] == "skipped"
    assert storage.orders() == []
