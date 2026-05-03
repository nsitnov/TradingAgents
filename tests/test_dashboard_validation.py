from tradingagents.dashboard.validation import (
    monte_carlo_bootstrap,
    validate_backtest_result,
    walk_forward_validation,
)


def _history(values):
    return [
        {
            "created_at": f"2026-01-{index + 1:02d}T23:59:59+00:00",
            "equity": value,
        }
        for index, value in enumerate(values)
    ]


def test_walk_forward_validation_reports_window_stability():
    result = walk_forward_validation(
        _history([100, 101, 102, 103, 104, 105, 106]),
        train_size=3,
        test_size=2,
        step_size=2,
    )

    assert result["window_count"] == 2
    assert result["pass_rate"] == 1.0
    assert result["windows"][0]["train_return_pct"] == 0.02
    assert round(result["windows"][0]["test_return_pct"], 4) == 0.0196


def test_monte_carlo_bootstrap_is_deterministic_with_seed():
    history = _history([100, 102, 101, 104, 106, 105])

    first = monte_carlo_bootstrap(history, iterations=50, seed=7)
    second = monte_carlo_bootstrap(history, iterations=50, seed=7)

    assert first == second
    assert first["iterations"] == 50
    assert first["horizon"] == 5
    assert first["end_equity_p95"] >= first["end_equity_p05"]


def test_validate_backtest_result_combines_validation_sections():
    validation = validate_backtest_result(
        {"history": _history([100, 101, 99, 103, 104])},
        train_size=2,
        test_size=2,
        step_size=1,
        monte_carlo_iterations=20,
    )

    assert validation["walk_forward"]["window_count"] == 2
    assert validation["monte_carlo"]["iterations"] == 20
