from datetime import date

from tradingagents.dashboard.performance import (
    benchmark_comparison,
    portfolio_performance,
    trade_performance,
)


def test_portfolio_performance_calculates_return_drawdown_and_trade_stats():
    history = [
        {"equity": 100_000, "created_at": "2026-05-01T10:00:00+00:00"},
        {"equity": 105_000, "created_at": "2026-05-02T10:00:00+00:00"},
        {"equity": 102_000, "created_at": "2026-05-03T10:00:00+00:00"},
        {"equity": 108_000, "created_at": "2026-05-04T10:00:00+00:00"},
    ]
    trades = [
        {
            "ticker": "SPY",
            "action": "buy",
            "quantity": 10,
            "price": 100,
            "created_at": "2026-05-01T10:00:00+00:00",
        },
        {
            "ticker": "SPY",
            "action": "sell",
            "quantity": 10,
            "price": 110,
            "created_at": "2026-05-02T10:00:00+00:00",
        },
    ]

    metrics = portfolio_performance(
        history,
        trades,
        100_000,
        price_provider=lambda ticker, start, end: (100.0, 104.0),
    )

    assert metrics["total_pnl"] == 8_000
    assert metrics["total_return_pct"] == 0.08
    assert round(metrics["max_drawdown"], 4) == -0.0286
    assert metrics["trade_count"] == 2
    assert metrics["closed_trade_count"] == 1
    assert metrics["win_rate"] == 1.0
    assert metrics["gross_profit"] == 100
    assert metrics["profit_factor"] is None
    assert metrics["benchmarks"][0]["ticker"] == "SPY"
    assert metrics["benchmarks"][0]["return_pct"] == 0.04
    assert round(metrics["benchmarks"][0]["alpha_pct"], 4) == 0.04


def test_trade_performance_handles_losing_closed_trade():
    metrics = trade_performance(
        [
            {
                "ticker": "SPY",
                "action": "buy",
                "quantity": 10,
                "price": 100,
                "trade_date": "2026-05-01",
            },
            {
                "ticker": "SPY",
                "action": "sell",
                "quantity": 10,
                "price": 95,
                "trade_date": "2026-05-02",
            },
        ]
    )

    assert metrics["loss_count"] == 1
    assert metrics["gross_loss"] == 50
    assert metrics["average_closed_trade_pnl"] == -50


def test_benchmark_comparison_reports_errors_without_failing():
    rows = benchmark_comparison(
        [{"equity": 100_000, "created_at": "2026-05-01"}],
        start_equity=100_000,
        portfolio_return_pct=0.05,
        benchmarks=["SPY"],
        price_provider=lambda ticker, start, end: (_raise("missing prices")),
    )

    assert rows[0]["ticker"] == "SPY"
    assert rows[0]["error"] == "missing prices"


def test_benchmark_comparison_uses_matching_snapshot_dates():
    calls = []

    def prices(ticker, start, end):
        calls.append((ticker, start, end))
        return 100.0, 110.0

    rows = benchmark_comparison(
        [
            {"equity": 100_000, "created_at": "2026-05-01T15:00:00+00:00"},
            {"equity": 105_000, "created_at": "2026-05-08T15:00:00+00:00"},
        ],
        start_equity=100_000,
        portfolio_return_pct=0.05,
        benchmarks=["QQQ"],
        price_provider=prices,
    )

    assert calls == [("QQQ", date(2026, 5, 1), date(2026, 5, 8))]
    assert rows[0]["return_pct"] == 0.10
    assert round(rows[0]["alpha_value"], 2) == -5_000


def _raise(message):
    raise ValueError(message)
