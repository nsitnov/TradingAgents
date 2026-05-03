from __future__ import annotations

import os
import threading
import uuid
from datetime import date, timedelta
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from tradingagents.dashboard.backtest import (
    BacktestConfig,
    BacktestEngine,
    TransactionCostModel,
    normalize_decision,
    now_iso,
)
from tradingagents.dashboard.storage import DashboardStorage


TERMINAL_STATUSES = {"completed", "error", "cancelled"}
DEFAULT_MAX_AGENT_DECISIONS = int(os.getenv("TRADINGAGENTS_REPLAY_MAX_DECISIONS", "25"))


class AgentReplayRequest(BaseModel):
    tickers: List[str] = Field(default_factory=lambda: ["SPY"], min_length=1)
    start: date
    end: date
    initial_cash: float = Field(default=100_000.0, gt=0)
    benchmarks: List[str] = Field(default_factory=lambda: ["SPY", "QQQ"])
    decision_provider: str = "tradingagents"
    fixed_decision: str = "Hold"
    transaction_costs: Dict[str, float] = Field(default_factory=dict)
    analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals"]
    )
    research_depth: int = Field(default=1, ge=1, le=5)
    llm_provider: str = "openai"
    shallow_thinker: str = "gpt-5.4-mini"
    deep_thinker: str = "gpt-5.4"
    backend_url: Optional[str] = None
    output_language: str = "English"
    openai_reasoning_effort: Optional[str] = None
    max_decisions: int = Field(default=DEFAULT_MAX_AGENT_DECISIONS, ge=1, le=5000)

    @field_validator("tickers", "benchmarks")
    @classmethod
    def normalize_symbols(cls, value: List[str]) -> List[str]:
        symbols = [item.strip().upper() for item in value if item.strip()]
        if not symbols:
            raise ValueError("At least one symbol is required")
        return list(dict.fromkeys(symbols))

    @field_validator("decision_provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        if provider not in {"fixed", "tradingagents"}:
            raise ValueError("decision_provider must be fixed or tradingagents")
        return provider

    @field_validator("fixed_decision")
    @classmethod
    def normalize_fixed_decision(cls, value: str) -> str:
        return normalize_decision(value)

    @model_validator(mode="after")
    def validate_dates_and_budget(self) -> "AgentReplayRequest":
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        decisions = estimate_decisions(self.tickers, self.start, self.end)
        if decisions > self.max_decisions:
            raise ValueError(
                f"Replay would require {decisions} decisions, above max_decisions={self.max_decisions}"
            )
        return self

    def to_backtest_config(self) -> BacktestConfig:
        config = BacktestConfig(
            tickers=self.tickers,
            start=self.start,
            end=self.end,
            initial_cash=self.initial_cash,
            benchmarks=self.benchmarks,
            decision_provider=self.decision_provider,
            fixed_decision=self.fixed_decision,
            transaction_costs=TransactionCostModel(**self.transaction_costs),
            analysts=self.analysts,
            research_depth=self.research_depth,
            llm_provider=self.llm_provider,
            shallow_thinker=self.shallow_thinker,
            deep_thinker=self.deep_thinker,
            backend_url=self.backend_url,
            output_language=self.output_language,
            openai_reasoning_effort=self.openai_reasoning_effort,
        )
        config.validate()
        return config

    def as_config(self) -> Dict[str, Any]:
        data = self.model_dump(mode="json")
        data["estimated_decisions"] = estimate_decisions(self.tickers, self.start, self.end)
        return data


class AgentReplayService:
    def __init__(
        self,
        storage: DashboardStorage,
        *,
        engine_factory: Callable[..., BacktestEngine] = BacktestEngine,
    ) -> None:
        self.storage = storage
        self.engine_factory = engine_factory
        self._lock = threading.Lock()
        self._cancel_flags: Dict[str, threading.Event] = {}

    def start(self, request: AgentReplayRequest) -> Dict[str, Any]:
        with self._lock:
            running = [
                job
                for job in self.storage.agent_replay_jobs(limit=20)
                if job["status"] not in TERMINAL_STATUSES
            ]
            if running:
                raise RuntimeError("Another agent replay job is already active")

            job = {
                "job_id": str(uuid.uuid4()),
                "status": "queued",
                "started_at": now_iso(),
                "ended_at": None,
                "config": request.as_config(),
                "progress": _initial_progress(request),
                "result": {},
                "error": None,
            }
            cancel_flag = threading.Event()
            self._cancel_flags[job["job_id"]] = cancel_flag
            self.storage.upsert_agent_replay_job(job)

        thread = threading.Thread(
            target=self._worker,
            args=(job["job_id"], request, cancel_flag),
            name=f"agent-replay-{job['job_id'][:8]}",
            daemon=True,
        )
        thread.start()
        return job

    def list_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.storage.agent_replay_jobs(limit=limit)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.storage.agent_replay_job_detail(job_id)

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            flag = self._cancel_flags.get(job_id)
            if flag:
                flag.set()
                job = self.storage.agent_replay_job_detail(job_id)
                if job and job["status"] not in TERMINAL_STATUSES:
                    job["status"] = "cancelling"
                    self.storage.upsert_agent_replay_job(job)
                return True
        job = self.storage.agent_replay_job_detail(job_id)
        return bool(job and job["status"] == "cancelled")

    def _worker(
        self,
        job_id: str,
        request: AgentReplayRequest,
        cancel_flag: threading.Event,
    ) -> None:
        job = self.storage.agent_replay_job_detail(job_id) or {}
        job.update({"status": "running", "error": None})
        self.storage.upsert_agent_replay_job(job)

        def on_progress(progress: Dict[str, Any]) -> None:
            current = self.storage.agent_replay_job_detail(job_id) or job
            total = int(progress.get("total_steps") or current["progress"].get("total_steps") or 0)
            completed = int(progress.get("completed_steps") or 0)
            current["progress"] = {
                **current.get("progress", {}),
                **progress,
                "pct_complete": (completed / total) if total else 0.0,
            }
            current["status"] = "cancelling" if cancel_flag.is_set() else "running"
            self.storage.upsert_agent_replay_job(current)

        try:
            engine = self.engine_factory(
                request.to_backtest_config(),
                progress_callback=on_progress,
                should_cancel=cancel_flag.is_set,
            )
            result = engine.run()
            latest = self.storage.agent_replay_job_detail(job_id) or job
            latest["result"] = result
            latest["status"] = result.get("status", "completed")
            latest["ended_at"] = result.get("ended_at") or now_iso()
            latest["error"] = result.get("error")
            progress = latest.get("progress", {})
            if result.get("status") == "completed":
                progress = {
                    **progress,
                    "completed_steps": progress.get("total_steps", 0),
                    "pct_complete": 1.0,
                }
            latest["progress"] = progress
            self.storage.upsert_agent_replay_job(latest)
            if result.get("status") == "completed":
                self.storage.upsert_backtest(result)
        except Exception as exc:
            latest = self.storage.agent_replay_job_detail(job_id) or job
            latest.update(
                {
                    "status": "error",
                    "ended_at": now_iso(),
                    "error": str(exc),
                }
            )
            self.storage.upsert_agent_replay_job(latest)
        finally:
            with self._lock:
                self._cancel_flags.pop(job_id, None)


def estimate_decisions(tickers: List[str], start: date, end: date) -> int:
    return len(tickers) * len(list(_business_days(start, end)))


def _business_days(start: date, end: date):
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def _initial_progress(request: AgentReplayRequest) -> Dict[str, Any]:
    total = estimate_decisions(request.tickers, request.start, request.end)
    return {
        "completed_steps": 0,
        "total_steps": total,
        "pct_complete": 0.0,
        "estimated_decisions": total,
        "trade_count": 0,
        "skipped_count": 0,
    }
