from tradingagents.dashboard.ledger import PaperLedger


def test_buy_decision_invests_twenty_percent_of_cash(tmp_path):
    ledger = PaperLedger(tmp_path / "ledger.json", price_provider=lambda _: 100.0)

    snapshot = ledger.apply_decision(
        ticker="SPY",
        decision="Buy",
        trade_date="2026-05-03",
        run_id="run-1",
    )

    assert snapshot["cash"] == 80_000
    assert snapshot["positions"]["SPY"]["quantity"] == 200
    assert snapshot["positions"]["SPY"]["market_value"] == 20_000
    assert snapshot["trades"][0]["action"] == "buy"


def test_sell_decision_closes_position_and_realizes_pnl(tmp_path):
    prices = iter([100.0, 110.0])
    ledger = PaperLedger(tmp_path / "ledger.json", price_provider=lambda _: next(prices))
    ledger.apply_decision(
        ticker="SPY",
        decision="Buy",
        trade_date="2026-05-03",
        run_id="run-1",
    )

    snapshot = ledger.apply_decision(
        ticker="SPY",
        decision="Sell",
        trade_date="2026-05-04",
        run_id="run-2",
    )

    assert snapshot["positions"]["SPY"]["quantity"] == 0
    assert snapshot["positions"]["SPY"]["realized_pnl"] == 2_000
    assert snapshot["cash"] == 102_000


def test_ledger_writes_trade_and_snapshot_to_storage(tmp_path):
    from tradingagents.dashboard.storage import DashboardStorage

    storage = DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )
    ledger = PaperLedger(
        tmp_path / "ledger.json",
        price_provider=lambda _: 100.0,
        storage=storage,
    )

    ledger.apply_decision(
        ticker="SPY",
        decision="Overweight",
        trade_date="2026-05-03",
        run_id="run-1",
    )

    assert storage.trades()[0]["decision"] == "Overweight"
    assert storage.portfolio_history()[0]["equity"] == 100_000
