from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.oms import OrderIntent, PaperOrderService, RiskConfig
from tradingagents.dashboard.storage import DashboardStorage


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


def test_paper_order_service_fills_approved_order_and_writes_audit(tmp_path):
    storage = _storage(tmp_path)
    ledger = PaperLedger(
        tmp_path / "ledger.json",
        price_provider=lambda _: 100.0,
        storage=storage,
    )
    service = PaperOrderService(
        ledger=ledger,
        storage=storage,
        risk_config=RiskConfig(
            mode="PAPER",
            max_position_pct=0.50,
            max_trade_notional=25_000,
        ),
    )

    result = service.submit_decision(
        OrderIntent(
            run_id="run-1",
            ticker="SPY",
            decision="Buy",
            trade_date="2026-05-03",
        )
    )

    assert result["order"]["status"] == "filled"
    assert storage.orders()[0]["status"] == "filled"
    assert storage.fills()[0]["notional"] == 20_000
    assert storage.audit_events()[0]["event_type"] == "order_filled"
    assert ledger.snapshot()["positions"]["SPY"]["market_value"] == 20_000


def test_risk_gate_rejects_order_over_notional_limit(tmp_path):
    storage = _storage(tmp_path)
    ledger = PaperLedger(
        tmp_path / "ledger.json",
        price_provider=lambda _: 100.0,
        storage=storage,
    )
    service = PaperOrderService(
        ledger=ledger,
        storage=storage,
        risk_config=RiskConfig(mode="PAPER", max_trade_notional=1_000),
    )

    result = service.submit_decision(
        OrderIntent(
            run_id="run-1",
            ticker="SPY",
            decision="Buy",
            trade_date="2026-05-03",
        )
    )

    assert result["order"]["status"] == "rejected"
    assert "max_trade_notional" in result["risk"]["reason"]
    assert storage.fills() == []
    assert "SPY" not in ledger.snapshot()["positions"]


def test_manual_approval_defers_and_then_executes_order(tmp_path):
    storage = _storage(tmp_path)
    ledger = PaperLedger(
        tmp_path / "ledger.json",
        price_provider=lambda _: 100.0,
        storage=storage,
    )
    service = PaperOrderService(
        ledger=ledger,
        storage=storage,
        risk_config=RiskConfig(
            mode="PAPER",
            require_manual_approval=True,
            max_position_pct=0.50,
            max_trade_notional=25_000,
        ),
    )

    result = service.submit_decision(
        OrderIntent(
            run_id="run-1",
            ticker="SPY",
            decision="Buy",
            trade_date="2026-05-03",
        )
    )

    assert result["order"]["status"] == "pending_approval"
    assert storage.fills() == []

    approved = service.approve_order(result["order"]["order_id"])

    assert approved["order"]["status"] == "filled"
    assert storage.fills()[0]["notional"] == 20_000


def test_idempotency_key_prevents_duplicate_order_execution(tmp_path):
    storage = _storage(tmp_path)
    ledger = PaperLedger(
        tmp_path / "ledger.json",
        price_provider=lambda _: 100.0,
        storage=storage,
    )
    service = PaperOrderService(
        ledger=ledger,
        storage=storage,
        risk_config=RiskConfig(
            mode="PAPER",
            max_position_pct=0.50,
            max_trade_notional=25_000,
        ),
    )
    intent = OrderIntent(
        run_id="run-1",
        ticker="SPY",
        decision="Buy",
        trade_date="2026-05-03",
    )

    first = service.submit_decision(intent)
    second = service.submit_decision(intent)

    assert first["order"]["order_id"] == second["order"]["order_id"]
    assert len(storage.orders()) == 1
    assert len(storage.fills()) == 1


def test_demo_mode_logs_order_without_mutating_ledger(tmp_path):
    storage = _storage(tmp_path)
    ledger = PaperLedger(
        tmp_path / "ledger.json",
        price_provider=lambda _: 100.0,
        storage=storage,
    )
    service = PaperOrderService(
        ledger=ledger,
        storage=storage,
        risk_config=RiskConfig(
            mode="DEMO",
            max_position_pct=0.50,
            max_trade_notional=25_000,
        ),
    )

    result = service.submit_decision(
        OrderIntent(
            run_id="run-1",
            ticker="SPY",
            decision="Buy",
            trade_date="2026-05-03",
        )
    )

    assert result["order"]["status"] == "simulated"
    assert storage.fills() == []
    assert "SPY" not in ledger.snapshot()["positions"]
