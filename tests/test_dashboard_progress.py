from datetime import datetime

from tradingagents.dashboard.progress import (
    STATUS_DEGRADING,
    STATUS_IMPROVING,
    STATUS_INSUFFICIENT,
    ProgressReporter,
)
from tradingagents.dashboard.storage import DashboardStorage


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


def _snapshot(storage, run_id, updated_at, equity):
    storage.insert_portfolio_snapshot(
        {
            "updated_at": updated_at,
            "cash": equity,
            "market_value": 0,
            "equity": equity,
            "realized_pnl": equity - 100_000,
            "unrealized_pnl": 0,
            "total_pnl": equity - 100_000,
            "positions": {},
        },
        run_id=run_id,
    )


def _backtest(storage, backtest_id, alpha_spy, alpha_qqq):
    storage.upsert_backtest(
        {
            "backtest_id": backtest_id,
            "status": "completed",
            "started_at": "2026-04-29T08:00:00+03:00",
            "ended_at": "2026-04-29T08:01:00+03:00",
            "config": {"tickers": ["SPY"]},
            "summary": {"total_return_pct": 0.02},
            "performance": {
                "benchmarks": [
                    {"ticker": "SPY", "alpha_pct": alpha_spy},
                    {"ticker": "QQQ", "alpha_pct": alpha_qqq},
                ]
            },
            "history": [],
            "trades": [],
        }
    )


def test_progress_reports_insufficient_data_without_weekly_evidence(tmp_path):
    storage = _storage(tmp_path)

    report = ProgressReporter(storage).weekly(
        as_of=datetime.fromisoformat("2026-05-03T09:00:00+03:00")
    )

    assert report["overall_status"] == STATUS_INSUFFICIENT
    assert report["statuses"]["profitability"] == STATUS_INSUFFICIENT
    assert report["score"] < 50
    assert report["recommendations"]


def test_progress_classifies_improving_when_paper_and_alpha_are_positive(tmp_path):
    storage = _storage(tmp_path)
    _snapshot(storage, "start", "2026-04-28T08:00:00+03:00", 100_000)
    _snapshot(storage, "end", "2026-05-02T08:00:00+03:00", 102_000)
    _backtest(storage, "bt-positive", 0.015, 0.01)
    storage.upsert_autopilot_job(
        {
            "job_id": "auto-1",
            "job_type": "scheduled",
            "status": "completed",
            "started_at": "2026-05-02T09:00:00+03:00",
            "ended_at": "2026-05-02T09:01:00+03:00",
            "config": {},
            "result": {"summary": {"paper_executions": 1, "scanner_signals": 3}},
        }
    )
    storage.replace_openai_costs(
        [
            {
                "start_time": int(
                    datetime.fromisoformat("2026-05-02T00:00:00+00:00").timestamp()
                ),
                "end_time": int(
                    datetime.fromisoformat("2026-05-02T23:59:00+00:00").timestamp()
                ),
                "amount": 2.5,
                "currency": "usd",
            }
        ]
    )

    report = ProgressReporter(storage).weekly(
        as_of=datetime.fromisoformat("2026-05-03T09:00:00+03:00")
    )

    assert report["overall_status"] == STATUS_IMPROVING
    assert report["statuses"]["profitability"] == STATUS_IMPROVING
    assert report["statuses"]["autopilot"] == STATUS_IMPROVING
    assert report["statuses"]["cost"] == STATUS_IMPROVING
    assert report["backtests"]["benchmark_alpha"]["SPY"] == 0.015


def test_progress_classifies_degrading_on_large_drawdown(tmp_path):
    storage = _storage(tmp_path)
    _snapshot(storage, "start", "2026-04-28T08:00:00+03:00", 100_000)
    _snapshot(storage, "end", "2026-05-02T08:00:00+03:00", 88_000)
    _backtest(storage, "bt-negative", -0.02, -0.03)

    report = ProgressReporter(storage).weekly(
        as_of=datetime.fromisoformat("2026-05-03T09:00:00+03:00")
    )

    assert report["overall_status"] == STATUS_DEGRADING
    assert report["statuses"]["profitability"] == STATUS_DEGRADING
    assert report["portfolio"]["performance"]["max_drawdown"] == -0.12


def test_progress_history_returns_week_rows(tmp_path):
    storage = _storage(tmp_path)
    _snapshot(storage, "start", "2026-04-28T08:00:00+03:00", 100_000)
    _snapshot(storage, "end", "2026-05-02T08:00:00+03:00", 101_000)
    _backtest(storage, "bt-history", 0.01, 0.01)

    history = ProgressReporter(storage).history(
        weeks=2,
        as_of=datetime.fromisoformat("2026-05-03T09:00:00+03:00"),
    )

    assert len(history["weeks"]) == 2
    assert history["weeks"][-1]["overall_status"] == STATUS_IMPROVING
