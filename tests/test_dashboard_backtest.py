from datetime import date

from tradingagents.dashboard.backtest import (
    BacktestConfig,
    BacktestEngine,
    FixedDecisionProvider,
    TransactionCostModel,
)
from tradingagents.dashboard.storage import DashboardStorage


class FakePriceProvider:
    def __init__(self, prices):
        self.prices = {
            ticker: {
                (day if isinstance(day, date) else date.fromisoformat(day)): price
                for day, price in rows.items()
            }
            for ticker, rows in prices.items()
        }

    def prepare(self, tickers, start, end):
        return None

    def close(self, ticker, day):
        rows = self.prices[ticker]
        candidates = [price_date for price_date in rows if price_date <= day]
        if not candidates:
            raise ValueError(f"missing price for {ticker}")
        return rows[max(candidates)]

    def price_range(self, ticker, start, end):
        return self.close(ticker, start), self.close(ticker, end)


class SequenceDecisionProvider:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.index = 0

    def decide(self, ticker, day, portfolio):
        decision = self.decisions[min(self.index, len(self.decisions) - 1)]
        self.index += 1
        return decision


def _config(**overrides):
    data = {
        "tickers": ["SPY"],
        "start": date(2026, 1, 2),
        "end": date(2026, 1, 5),
        "benchmarks": ["SPY"],
        "transaction_costs": TransactionCostModel(
            spread_bps=0,
            slippage_bps=0,
            fee_bps=0,
            fixed_fee=0,
        ),
    }
    data.update(overrides)
    return BacktestConfig(**data)


def test_backtest_replays_fixed_buy_decisions_and_benchmarks():
    provider = FakePriceProvider(
        {"SPY": {date(2026, 1, 2): 100.0, date(2026, 1, 5): 110.0}}
    )

    result = BacktestEngine(
        _config(fixed_decision="Buy"),
        price_provider=provider,
    ).run()

    assert result["status"] == "completed"
    assert result["summary"]["trade_count"] == 2
    assert result["summary"]["end_equity"] == 102000.0
    assert result["performance"]["total_return_pct"] == 0.02
    assert result["performance"]["benchmarks"][0]["ticker"] == "SPY"
    assert result["performance"]["benchmarks"][0]["return_pct"] == 0.1


def test_backtest_can_close_positions_and_realize_pnl():
    provider = FakePriceProvider(
        {"SPY": {date(2026, 1, 2): 100.0, date(2026, 1, 5): 110.0}}
    )

    result = BacktestEngine(
        _config(),
        price_provider=provider,
        decision_provider=SequenceDecisionProvider(["Buy", "Sell"]),
    ).run()

    assert [trade["action"] for trade in result["trades"]] == ["buy", "sell"]
    assert result["summary"]["end_equity"] == 102000.0
    assert result["portfolio"]["positions"]["SPY"]["quantity"] == 0.0
    assert result["portfolio"]["realized_pnl"] == 2000.0


def test_transaction_costs_reduce_equity_on_entry():
    provider = FakePriceProvider(
        {"SPY": {date(2026, 1, 2): 100.0, date(2026, 1, 5): 100.0}}
    )
    zero_cost = BacktestEngine(
        _config(end=date(2026, 1, 2), fixed_decision="Buy"),
        price_provider=provider,
    ).run()
    with_costs = BacktestEngine(
        _config(
            end=date(2026, 1, 2),
            fixed_decision="Buy",
            transaction_costs=TransactionCostModel(
                spread_bps=5,
                slippage_bps=5,
                fee_bps=2,
                fixed_fee=1,
            ),
        ),
        price_provider=provider,
    ).run()

    assert with_costs["summary"]["end_equity"] < zero_cost["summary"]["end_equity"]
    assert with_costs["trades"][0]["fees"] > 0


def test_fixed_decision_provider_normalizes_unknown_values_to_hold():
    provider = FixedDecisionProvider("wait")

    assert provider.decide("SPY", date(2026, 1, 2), {}) == "Hold"


def test_storage_persists_backtest_summary_and_detail(tmp_path):
    storage = DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )
    result = {
        "backtest_id": "bt-1",
        "status": "completed",
        "started_at": "2026-01-05T08:00:00+00:00",
        "ended_at": "2026-01-05T08:00:01+00:00",
        "config": {"tickers": ["SPY"]},
        "summary": {"total_pnl": 123.0},
        "history": [{"equity": 100123.0}],
        "trades": [],
    }

    storage.upsert_backtest(result)

    assert storage.backtests()[0]["summary"]["total_pnl"] == 123.0
    detail = storage.backtest_detail("bt-1")
    assert detail["result"]["history"][0]["equity"] == 100123.0
