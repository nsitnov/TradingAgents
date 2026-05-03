from __future__ import annotations

import time
from datetime import date

import pytest

from tradingagents.dashboard.agent_replay import (
    AgentReplayRequest,
    AgentReplayService,
    estimate_decisions,
)
from tradingagents.dashboard.storage import DashboardStorage


class FakeReplayEngine:
    def __init__(self, config, *, progress_callback=None, should_cancel=None):
        self.config = config
        self.progress_callback = progress_callback
        self.should_cancel = should_cancel or (lambda: False)

    def run(self):
        total = estimate_decisions(self.config.tickers, self.config.start, self.config.end)
        for step in range(1, total + 1):
            if self.should_cancel():
                return {
                    "backtest_id": "bt-cancelled",
                    "status": "cancelled",
                    "started_at": "2026-01-05T08:00:00+00:00",
                    "ended_at": "2026-01-05T08:00:01+00:00",
                    "config": self.config.as_dict(),
                    "summary": {},
                    "history": [],
                    "trades": [],
                    "error": "Backtest cancelled",
                }
            if self.progress_callback:
                self.progress_callback(
                    {
                        "completed_steps": step,
                        "total_steps": total,
                        "ticker": self.config.tickers[0],
                        "trade_date": self.config.start.isoformat(),
                        "trade_count": step,
                        "skipped_count": 0,
                        "equity": 100000 + step,
                    }
                )
        return {
            "backtest_id": "bt-replay-1",
            "status": "completed",
            "started_at": "2026-01-05T08:00:00+00:00",
            "ended_at": "2026-01-05T08:00:01+00:00",
            "config": self.config.as_dict(),
            "summary": {"trade_count": total, "total_pnl": 10.0},
            "performance": {},
            "history": [{"created_at": "2026-01-05T23:59:59+00:00", "equity": 100010}],
            "trades": [],
            "validation": {"walk_forward": {}, "monte_carlo": {}},
        }


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


def _wait_for_terminal(service, job_id):
    for _ in range(50):
        job = service.get_job(job_id)
        if job and job["status"] in {"completed", "error", "cancelled"}:
            return job
        time.sleep(0.02)
    raise AssertionError("agent replay job did not finish")


def test_agent_replay_request_enforces_decision_budget():
    with pytest.raises(ValueError, match="above max_decisions"):
        AgentReplayRequest(
            tickers=["SPY", "QQQ"],
            start=date(2026, 1, 5),
            end=date(2026, 1, 9),
            max_decisions=5,
        )


def test_agent_replay_service_runs_job_and_persists_backtest(tmp_path):
    storage = _storage(tmp_path)
    service = AgentReplayService(storage, engine_factory=FakeReplayEngine)
    request = AgentReplayRequest(
        tickers=["SPY"],
        start=date(2026, 1, 5),
        end=date(2026, 1, 6),
        decision_provider="fixed",
        fixed_decision="Hold",
        max_decisions=10,
    )

    job = service.start(request)
    finished = _wait_for_terminal(service, job["job_id"])

    assert finished["status"] == "completed"
    assert finished["progress"]["pct_complete"] == 1.0
    assert finished["result"]["backtest_id"] == "bt-replay-1"
    assert storage.backtest_detail("bt-replay-1")["result"]["summary"]["trade_count"] == 2


def test_storage_persists_agent_replay_job(tmp_path):
    storage = _storage(tmp_path)
    storage.upsert_agent_replay_job(
        {
            "job_id": "job-1",
            "status": "completed",
            "started_at": "2026-01-05T08:00:00+00:00",
            "ended_at": "2026-01-05T08:00:01+00:00",
            "config": {"tickers": ["SPY"]},
            "progress": {"pct_complete": 1.0},
            "result": {"summary": {"total_pnl": 10}},
            "error": None,
        }
    )

    assert storage.agent_replay_jobs()[0]["job_id"] == "job-1"
    assert storage.agent_replay_job_detail("job-1")["result"]["summary"]["total_pnl"] == 10
