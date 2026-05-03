from __future__ import annotations

import random
from math import sqrt
from typing import Any, Dict, List, Optional, Sequence

from tradingagents.dashboard.performance import max_drawdown


def validate_backtest_result(
    result: Dict[str, Any],
    *,
    train_size: int = 63,
    test_size: int = 21,
    step_size: int = 21,
    monte_carlo_iterations: int = 500,
    seed: int = 42,
) -> Dict[str, Any]:
    history = result.get("history", [])
    return {
        "walk_forward": walk_forward_validation(
            history,
            train_size=train_size,
            test_size=test_size,
            step_size=step_size,
        ),
        "monte_carlo": monte_carlo_bootstrap(
            history,
            iterations=monte_carlo_iterations,
            seed=seed,
        ),
    }


def walk_forward_validation(
    history: List[Dict[str, Any]],
    *,
    train_size: int = 63,
    test_size: int = 21,
    step_size: int = 21,
) -> Dict[str, Any]:
    if train_size < 2 or test_size < 2 or step_size < 1:
        raise ValueError("train_size/test_size must be >= 2 and step_size must be >= 1")
    rows = _sorted_history(history)
    if len(rows) < train_size + test_size:
        return {
            "window_count": 0,
            "pass_rate": 0.0,
            "average_test_return_pct": 0.0,
            "average_test_max_drawdown": 0.0,
            "windows": [],
        }

    windows: List[Dict[str, Any]] = []
    start = 0
    while start + train_size + test_size <= len(rows):
        train = rows[start : start + train_size]
        test = rows[start + train_size - 1 : start + train_size + test_size]
        train_equities = _equities(train)
        test_equities = _equities(test)
        train_return = _total_return(train_equities)
        test_return = _total_return(test_equities)
        test_dd = max_drawdown(test_equities)
        windows.append(
            {
                "train_start": _created_at(train[0]),
                "train_end": _created_at(train[-1]),
                "test_start": _created_at(test[0]),
                "test_end": _created_at(test[-1]),
                "train_return_pct": train_return,
                "test_return_pct": test_return,
                "test_max_drawdown": test_dd,
                "passed": test_return > 0.0 and test_dd >= -0.10,
            }
        )
        start += step_size

    passed = [item for item in windows if item["passed"]]
    return {
        "window_count": len(windows),
        "pass_rate": (len(passed) / len(windows)) if windows else 0.0,
        "average_test_return_pct": _mean(
            [item["test_return_pct"] for item in windows]
        ),
        "average_test_max_drawdown": _mean(
            [item["test_max_drawdown"] for item in windows]
        ),
        "windows": windows,
    }


def monte_carlo_bootstrap(
    history: List[Dict[str, Any]],
    *,
    iterations: int = 500,
    seed: int = 42,
    horizon: Optional[int] = None,
) -> Dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be at least 1")
    rows = _sorted_history(history)
    equities = _equities(rows)
    returns = _period_returns(equities)
    if not equities or not returns:
        start_equity = equities[0] if equities else 0.0
        return {
            "iterations": 0,
            "horizon": 0,
            "start_equity": start_equity,
            "end_equity_p05": start_equity,
            "end_equity_p50": start_equity,
            "end_equity_p95": start_equity,
            "return_pct_p05": 0.0,
            "return_pct_p50": 0.0,
            "return_pct_p95": 0.0,
            "loss_probability": 0.0,
            "average_max_drawdown": 0.0,
        }

    rng = random.Random(seed)
    sample_horizon = horizon or len(returns)
    start_equity = equities[0]
    ending_equities: List[float] = []
    ending_returns: List[float] = []
    drawdowns: List[float] = []

    for _ in range(iterations):
        curve = [start_equity]
        equity = start_equity
        for _ in range(sample_horizon):
            equity *= 1.0 + rng.choice(returns)
            curve.append(equity)
        ending_equities.append(equity)
        ending_returns.append((equity - start_equity) / start_equity if start_equity else 0.0)
        drawdowns.append(max_drawdown(curve))

    losses = [value for value in ending_returns if value < 0]
    return {
        "iterations": iterations,
        "horizon": sample_horizon,
        "start_equity": start_equity,
        "end_equity_p05": _percentile(ending_equities, 0.05),
        "end_equity_p50": _percentile(ending_equities, 0.50),
        "end_equity_p95": _percentile(ending_equities, 0.95),
        "return_pct_p05": _percentile(ending_returns, 0.05),
        "return_pct_p50": _percentile(ending_returns, 0.50),
        "return_pct_p95": _percentile(ending_returns, 0.95),
        "loss_probability": len(losses) / iterations,
        "average_max_drawdown": _mean(drawdowns),
        "return_mean": _mean(ending_returns),
        "return_stdev": _stdev(ending_returns),
    }


def _sorted_history(history: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(history, key=lambda item: _created_at(item))


def _created_at(snapshot: Dict[str, Any]) -> str:
    return str(snapshot.get("created_at") or "")


def _equities(history: Sequence[Dict[str, Any]]) -> List[float]:
    return [float(item.get("equity", 0.0)) for item in history]


def _period_returns(equities: Sequence[float]) -> List[float]:
    returns = []
    for previous, current in zip(equities, equities[1:]):
        if previous:
            returns.append((current - previous) / previous)
    return returns


def _total_return(equities: Sequence[float]) -> float:
    if len(equities) < 2 or not equities[0]:
        return 0.0
    return (equities[-1] - equities[0]) / equities[0]


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = percentile * (len(ordered) - 1)
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return sqrt(variance)
