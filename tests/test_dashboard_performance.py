from tradingagents.dashboard.performance import portfolio_performance, trade_performance


def test_portfolio_performance_calculates_return_drawdown_and_trade_stats():
    history = [
        {"equity": 100_000},
        {"equity": 105_000},
        {"equity": 102_000},
        {"equity": 108_000},
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

    metrics = portfolio_performance(history, trades, 100_000)

    assert metrics["total_pnl"] == 8_000
    assert metrics["total_return_pct"] == 0.08
    assert round(metrics["max_drawdown"], 4) == -0.0286
    assert metrics["trade_count"] == 2
    assert metrics["closed_trade_count"] == 1
    assert metrics["win_rate"] == 1.0
    assert metrics["gross_profit"] == 100
    assert metrics["profit_factor"] is None


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
