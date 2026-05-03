from __future__ import annotations

import json
import asyncio
import fcntl
import threading
import uuid
from collections import deque
from copy import deepcopy
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, TextIO

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator

from cli.main import classify_message_type
from cli.stats_handler import StatsCallbackHandler
from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.oms import OrderIntent, PaperOrderService
from tradingagents.dashboard.performance import portfolio_performance
from tradingagents.dashboard.storage import DashboardStorage
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.checkpointer import clear_checkpoint, get_checkpointer, thread_id
from tradingagents.graph.trading_graph import TradingAgentsGraph


ANALYST_ORDER = ["market", "social", "news", "fundamentals"]
ANALYST_AGENT_NAMES = {
    "market": "Market Analyst",
    "social": "Social Analyst",
    "news": "News Analyst",
    "fundamentals": "Fundamentals Analyst",
}
ANALYST_REPORT_MAP = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}
FIXED_AGENTS = [
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Aggressive Analyst",
    "Neutral Analyst",
    "Conservative Analyst",
    "Portfolio Manager",
]
REPORT_SECTIONS = {
    "market_report": "Market Analysis",
    "sentiment_report": "Social Sentiment",
    "news_report": "News Analysis",
    "fundamentals_report": "Fundamentals Analysis",
    "investment_plan": "Research Team",
    "trader_investment_plan": "Trader",
    "final_trade_decision": "Portfolio Decision",
}
TERMINAL_STATUSES = {"completed", "error", "cancelled"}
RUN_LOCK_PATH = Path.home() / ".tradingagents" / "dashboard" / "run.lock"


class RunRequest(BaseModel):
    ticker: str = Field(default="SPY", min_length=1, max_length=24)
    analysis_date: date = Field(default_factory=date.today)
    analysts: List[str] = Field(
        default_factory=lambda: ["market", "social", "news", "fundamentals"]
    )
    research_depth: int = Field(default=1, ge=1, le=5)
    llm_provider: str = "openai"
    shallow_thinker: str = "gpt-oss:20b"
    deep_thinker: str = "gpt-5.4"
    backend_url: Optional[str] = None
    quick_llm_provider: str = "ollama"
    quick_backend_url: Optional[str] = "http://localhost:11434/v1"
    quick_fallback_llm_provider: Optional[str] = "openai"
    quick_fallback_thinker: Optional[str] = "gpt-5.4-mini"
    quick_fallback_backend_url: Optional[str] = None
    deep_llm_provider: str = "openai"
    deep_backend_url: Optional[str] = None
    critical_llm_provider: str = "openai"
    critical_thinker: str = "gpt-5.4"
    critical_backend_url: Optional[str] = None
    output_language: str = "English"
    openai_reasoning_effort: Optional[str] = None
    checkpoint: bool = False

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("analysts")
    @classmethod
    def normalize_analysts(cls, value: List[str]) -> List[str]:
        selected = [item.lower().strip() for item in value]
        invalid = [item for item in selected if item not in ANALYST_ORDER]
        if invalid:
            raise ValueError(f"Unknown analysts: {', '.join(invalid)}")
        ordered = [item for item in ANALYST_ORDER if item in selected]
        if not ordered:
            raise ValueError("At least one analyst must be selected")
        return ordered

    @field_validator("analysis_date")
    @classmethod
    def reject_future_dates(cls, value: date) -> date:
        if value > date.today():
            raise ValueError("Analysis date cannot be in the future")
        return value


class RunEvent(BaseModel):
    seq: int
    run_id: str
    type: str
    created_at: str
    payload: Dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RunAccumulator:
    def __init__(self, analysts: List[str]) -> None:
        self.messages: Deque[Dict[str, Any]] = deque(maxlen=250)
        self.tool_calls: Deque[Dict[str, Any]] = deque(maxlen=250)
        self.agent_status: Dict[str, str] = {}
        self.report_sections: Dict[str, Optional[str]] = {}
        self.current_agent: Optional[str] = None
        self.processed_message_ids: set[str] = set()
        self.selected_analysts = analysts

        for analyst in analysts:
            self.agent_status[ANALYST_AGENT_NAMES[analyst]] = "pending"
        for agent in FIXED_AGENTS:
            self.agent_status[agent] = "pending"
        for section in REPORT_SECTIONS:
            if section in ANALYST_REPORT_MAP.values():
                analyst = next(
                    key for key, report in ANALYST_REPORT_MAP.items() if report == section
                )
                if analyst not in analysts:
                    continue
            self.report_sections[section] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "messages": list(self.messages),
            "tool_calls": list(self.tool_calls),
            "agent_status": deepcopy(self.agent_status),
            "report_sections": deepcopy(self.report_sections),
            "current_agent": self.current_agent,
        }

    def add_message(self, message_type: str, content: str) -> Dict[str, Any]:
        item = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "type": message_type,
            "content": content,
        }
        self.messages.append(item)
        return item

    def add_tool_call(self, name: str, args: Any) -> Dict[str, Any]:
        item = {
            "time": datetime.now().strftime("%H:%M:%S"),
            "name": name,
            "args": args,
        }
        self.tool_calls.append(item)
        return item

    def update_agent_status(self, agent: str, status: str) -> Optional[Dict[str, str]]:
        if agent not in self.agent_status or self.agent_status[agent] == status:
            return None
        self.agent_status[agent] = status
        self.current_agent = agent
        return {"agent": agent, "status": status}

    def update_report_section(
        self, section_name: str, content: str
    ) -> Optional[Dict[str, str]]:
        if section_name not in self.report_sections:
            return None
        if self.report_sections[section_name] == content:
            return None
        self.report_sections[section_name] = content
        return {
            "section": section_name,
            "title": REPORT_SECTIONS.get(section_name, section_name),
            "content": content,
        }


class RunRecord:
    def __init__(self, run_id: str, request: RunRequest, source: str = "manual") -> None:
        self.run_id = run_id
        self.request = request
        self.source = source
        self.status = "queued"
        self.started_at = _now_iso()
        self.ended_at: Optional[str] = None
        self.events: List[RunEvent] = []
        self.accumulator = RunAccumulator(request.analysts)
        self.stats: Dict[str, Any] = {
            "llm_calls": 0,
            "tool_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
        }
        self.decision: Optional[str] = None
        self.error: Optional[str] = None
        self.cancel_requested = False
        self.lock_file: Optional[TextIO] = None

    def snapshot(self) -> Dict[str, Any]:
        snap = self.accumulator.snapshot()
        snap.update(
            {
                "run_id": self.run_id,
                "status": self.status,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "request": self.request.model_dump(mode="json"),
                "stats": deepcopy(self.stats),
                "decision": self.decision,
                "error": self.error,
                "source": self.source,
            }
        )
        return snap


class RunStore:
    def __init__(
        self,
        ledger: Optional[PaperLedger] = None,
        storage: Optional[DashboardStorage] = None,
    ) -> None:
        self.storage = storage or DashboardStorage()
        self.ledger = ledger or PaperLedger()
        self._runs: Dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    def start_run(self, request: RunRequest, source: str = "manual") -> RunRecord:
        with self._lock:
            active = [
                run
                for run in self._runs.values()
                if run.status not in TERMINAL_STATUSES
            ]
            if active:
                raise RuntimeError("Another TradingAgents run is already active")
            lock_file = self._acquire_run_lock()
            run = RunRecord(str(uuid.uuid4()), request, source=source)
            run.lock_file = lock_file
            self._runs[run.run_id] = run
            self._emit_locked(run, "run_started", run.snapshot())
            self.storage.upsert_run(run.snapshot(), source=source)

        thread = threading.Thread(
            target=self._run_worker,
            args=(run.run_id,),
            name=f"tradingagents-run-{run.run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return run

    def list_runs(self) -> List[Dict[str, Any]]:
        with self._lock:
            live = [
                run.snapshot()
                for run in sorted(
                    self._runs.values(), key=lambda item: item.started_at, reverse=True
                )
            ]
        stored = self.storage.recent_runs()
        seen = {run["run_id"] for run in live}
        return live + [run for run in stored if run["run_id"] not in seen]

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            run = self._runs.get(run_id)
            if run:
                return run.snapshot()
        return self.storage.run_detail(run_id)

    def get_events_after(self, run_id: str, seq: int) -> List[Dict[str, Any]]:
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                detail = self.storage.run_detail(run_id)
                return [
                    event
                    for event in (detail or {}).get("events", [])
                    if event["seq"] > seq
                ]
            return [
                event.model_dump(mode="json")
                for event in run.events
                if event.seq > seq
            ]

    def is_terminal(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if run:
                return run.status in TERMINAL_STATUSES
        detail = self.storage.run_detail(run_id)
        return bool(detail and detail.get("status") in TERMINAL_STATUSES)

    def cancel_run(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            if not run or run.status in TERMINAL_STATUSES:
                return False
            run.cancel_requested = True
            self._emit_locked(run, "message", {"type": "System", "content": "Cancel requested"})
            return True

    def portfolio(self) -> Dict[str, Any]:
        return self.ledger.snapshot()

    def portfolio_history(self) -> Dict[str, Any]:
        history = self.storage.portfolio_history()
        trades = self.storage.trades()
        return {
            "history": history,
            "trades": trades,
            "performance": self._portfolio_performance(history),
        }

    def approve_order(self, order_id: str) -> Dict[str, Any]:
        return self._order_service().approve_order(order_id)

    def reject_order(self, order_id: str, reason: str = "Rejected manually") -> Dict[str, Any]:
        return self._order_service().reject_order(order_id, reason)

    def _run_worker(self, run_id: str) -> None:
        load_dotenv()
        load_dotenv(".env.enterprise", override=False)

        with self._lock:
            run = self._runs[run_id]
            run.status = "running"
            self._emit_locked(run, "agent_status", run.accumulator.snapshot())
            self.storage.upsert_run(run.snapshot(), source=run.source)

        request = run.request
        stats_handler = StatsCallbackHandler()
        trace: List[Dict[str, Any]] = []
        checkpointer_ctx = None

        try:
            config = DEFAULT_CONFIG.copy()
            config["max_debate_rounds"] = request.research_depth
            config["max_risk_discuss_rounds"] = request.research_depth
            config["quick_think_llm"] = request.shallow_thinker
            config["deep_think_llm"] = request.deep_thinker
            config["backend_url"] = request.backend_url
            config["llm_provider"] = request.llm_provider.lower()
            config["quick_llm_provider"] = request.quick_llm_provider.lower()
            config["quick_backend_url"] = request.quick_backend_url
            config["quick_fallback_llm_provider"] = (
                request.quick_fallback_llm_provider.lower()
                if request.quick_fallback_llm_provider
                else None
            )
            config["quick_fallback_think_llm"] = request.quick_fallback_thinker
            config["quick_fallback_backend_url"] = request.quick_fallback_backend_url
            config["deep_llm_provider"] = request.deep_llm_provider.lower()
            config["deep_backend_url"] = request.deep_backend_url
            config["critical_llm_provider"] = request.critical_llm_provider.lower()
            config["critical_think_llm"] = request.critical_thinker
            config["critical_backend_url"] = request.critical_backend_url
            config["openai_reasoning_effort"] = request.openai_reasoning_effort
            config["output_language"] = request.output_language
            config["checkpoint_enabled"] = request.checkpoint

            graph = TradingAgentsGraph(
                request.analysts,
                config=config,
                debug=True,
                callbacks=[stats_handler],
            )
            graph.ticker = request.ticker
            graph._resolve_pending_entries(request.ticker)

            if config.get("checkpoint_enabled"):
                checkpointer_ctx = get_checkpointer(
                    config["data_cache_dir"], request.ticker
                )
                saver = checkpointer_ctx.__enter__()
                graph.graph = graph.workflow.compile(checkpointer=saver)

            init_agent_state = graph.propagator.create_initial_state(
                request.ticker, request.analysis_date.isoformat()
            )
            args = graph.propagator.get_graph_args(callbacks=[stats_handler])
            if config.get("checkpoint_enabled"):
                tid = thread_id(request.ticker, request.analysis_date.isoformat())
                args.setdefault("config", {}).setdefault("configurable", {})[
                    "thread_id"
                ] = tid

            self._add_system_message(
                run,
                f"Analyzing {request.ticker} on {request.analysis_date.isoformat()}",
            )
            self._set_first_agent_active(run)

            for chunk in graph.graph.stream(init_agent_state, **args):
                if run.cancel_requested:
                    run.status = "cancelled"
                    raise InterruptedError("Run cancelled")
                trace.append(chunk)
                self._process_chunk(run, chunk, stats_handler)

            if not trace:
                raise RuntimeError("TradingAgents graph produced no output")
            run.stats = {
                **stats_handler.get_stats(),
                "llm_fallbacks": len(graph.llm_fallback_events),
                "llm_fallback_events": graph.llm_fallback_events,
                "llm_routing": graph.llm_routing,
            }

            final_state = trace[-1]
            graph.curr_state = final_state
            graph._log_state(request.analysis_date.isoformat(), final_state)
            graph.memory_log.store_decision(
                ticker=request.ticker,
                trade_date=request.analysis_date.isoformat(),
                final_trade_decision=final_state["final_trade_decision"],
            )
            if config.get("checkpoint_enabled"):
                clear_checkpoint(
                    config["data_cache_dir"], request.ticker, request.analysis_date.isoformat()
                )

            decision = graph.process_signal(final_state["final_trade_decision"])
            self._complete_run(run, final_state, decision)
        except InterruptedError as exc:
            self._fail_or_cancel(run, str(exc), cancelled=True)
        except Exception as exc:
            self._fail_or_cancel(run, str(exc), cancelled=False)
        finally:
            if checkpointer_ctx is not None:
                checkpointer_ctx.__exit__(None, None, None)

    def _process_chunk(
        self, run: RunRecord, chunk: Dict[str, Any], stats_handler: StatsCallbackHandler
    ) -> None:
        with self._lock:
            for message in chunk.get("messages", []):
                msg_id = getattr(message, "id", None)
                if msg_id is not None:
                    if msg_id in run.accumulator.processed_message_ids:
                        continue
                    run.accumulator.processed_message_ids.add(msg_id)

                msg_type, content = classify_message_type(message)
                if content and content.strip():
                    item = run.accumulator.add_message(msg_type, content)
                    self._emit_locked(run, "message", item)

                tool_calls = getattr(message, "tool_calls", None)
                if tool_calls:
                    for tool_call in tool_calls:
                        if isinstance(tool_call, dict):
                            item = run.accumulator.add_tool_call(
                                tool_call["name"], tool_call["args"]
                            )
                        else:
                            item = run.accumulator.add_tool_call(
                                tool_call.name, tool_call.args
                            )
                        self._emit_locked(run, "tool_call", item)

            self._update_analysts_locked(run, chunk)
            self._update_research_locked(run, chunk)
            self._update_trader_locked(run, chunk)
            self._update_risk_locked(run, chunk)

            run.stats = stats_handler.get_stats()
            self._emit_locked(run, "stats", run.stats)
            self.storage.upsert_run(run.snapshot(), source=run.source)

    def _update_analysts_locked(self, run: RunRecord, chunk: Dict[str, Any]) -> None:
        found_active = False
        for analyst_key in ANALYST_ORDER:
            if analyst_key not in run.accumulator.selected_analysts:
                continue
            agent_name = ANALYST_AGENT_NAMES[analyst_key]
            report_key = ANALYST_REPORT_MAP[analyst_key]
            if chunk.get(report_key):
                self._update_report_locked(run, report_key, chunk[report_key])

            has_report = bool(run.accumulator.report_sections.get(report_key))
            if has_report:
                self._update_status_locked(run, agent_name, "completed")
            elif not found_active:
                self._update_status_locked(run, agent_name, "in_progress")
                found_active = True
            else:
                self._update_status_locked(run, agent_name, "pending")

        if not found_active and run.accumulator.selected_analysts:
            self._update_status_locked(run, "Bull Researcher", "in_progress")

    def _update_research_locked(self, run: RunRecord, chunk: Dict[str, Any]) -> None:
        debate_state = chunk.get("investment_debate_state")
        if not debate_state:
            return
        bull = debate_state.get("bull_history", "").strip()
        bear = debate_state.get("bear_history", "").strip()
        judge = debate_state.get("judge_decision", "").strip()
        if bull or bear:
            for agent in ["Bull Researcher", "Bear Researcher", "Research Manager"]:
                self._update_status_locked(run, agent, "in_progress")
        if bull:
            self._update_report_locked(run, "investment_plan", f"### Bull Researcher\n{bull}")
        if bear:
            self._update_report_locked(run, "investment_plan", f"### Bear Researcher\n{bear}")
        if judge:
            self._update_report_locked(run, "investment_plan", f"### Research Manager\n{judge}")
            for agent in ["Bull Researcher", "Bear Researcher", "Research Manager"]:
                self._update_status_locked(run, agent, "completed")
            self._update_status_locked(run, "Trader", "in_progress")

    def _update_trader_locked(self, run: RunRecord, chunk: Dict[str, Any]) -> None:
        plan = chunk.get("trader_investment_plan")
        if not plan:
            return
        self._update_report_locked(run, "trader_investment_plan", plan)
        self._update_status_locked(run, "Trader", "completed")
        self._update_status_locked(run, "Aggressive Analyst", "in_progress")

    def _update_risk_locked(self, run: RunRecord, chunk: Dict[str, Any]) -> None:
        risk_state = chunk.get("risk_debate_state")
        if not risk_state:
            return
        risk_map = [
            ("aggressive_history", "Aggressive Analyst"),
            ("conservative_history", "Conservative Analyst"),
            ("neutral_history", "Neutral Analyst"),
        ]
        for key, agent in risk_map:
            history = risk_state.get(key, "").strip()
            if history:
                self._update_status_locked(run, agent, "in_progress")
                self._update_report_locked(run, "final_trade_decision", f"### {agent}\n{history}")

        judge = risk_state.get("judge_decision", "").strip()
        if judge:
            self._update_status_locked(run, "Portfolio Manager", "in_progress")
            self._update_report_locked(run, "final_trade_decision", f"### Portfolio Manager\n{judge}")
            for agent in [
                "Aggressive Analyst",
                "Conservative Analyst",
                "Neutral Analyst",
                "Portfolio Manager",
            ]:
                self._update_status_locked(run, agent, "completed")

    def _complete_run(
        self, run: RunRecord, final_state: Dict[str, Any], decision: str
    ) -> None:
        with self._lock:
            for section in run.accumulator.report_sections:
                if final_state.get(section):
                    self._update_report_locked(run, section, final_state[section])
            for agent in list(run.accumulator.agent_status):
                self._update_status_locked(run, agent, "completed")
            run.decision = decision
            run.status = "completed"
            run.ended_at = _now_iso()
            self._emit_locked(run, "decision", {"decision": decision})
            self.storage.upsert_run(run.snapshot(), source=run.source)

        try:
            order_result = self._order_service().submit_decision(
                OrderIntent(
                    run_id=run.run_id,
                    ticker=run.request.ticker,
                    decision=decision,
                    trade_date=run.request.analysis_date.isoformat(),
                    source=run.source,
                )
            )
            portfolio = order_result["portfolio"]
            with self._lock:
                self._emit_locked(run, "order_update", order_result["order"])
                if order_result.get("risk"):
                    self._emit_locked(run, "risk_decision", order_result["risk"])
                self._emit_locked(run, "position_update", portfolio)
                self._emit_locked(run, "pnl_update", portfolio)
        except Exception as exc:
            with self._lock:
                self._emit_locked(run, "message", {"type": "System", "content": f"Ledger update skipped: {exc}"})

        with self._lock:
            self._emit_locked(run, "run_completed", run.snapshot())
            self.storage.write_complete_analysis(run.snapshot(), run.accumulator.report_sections)
            self.storage.upsert_run(run.snapshot(), source=run.source)
            self._release_run_lock(run)

    def _order_service(self) -> PaperOrderService:
        return PaperOrderService(ledger=self.ledger, storage=self.storage)

    def _fail_or_cancel(self, run: RunRecord, error: str, cancelled: bool) -> None:
        with self._lock:
            if cancelled:
                run.status = "cancelled"
            else:
                run.status = "error"
                run.error = error
            run.ended_at = _now_iso()
            self._emit_locked(
                run,
                "run_error" if not cancelled else "run_completed",
                {"status": run.status, "error": error},
            )
            self.storage.upsert_run(run.snapshot(), source=run.source)
            self._release_run_lock(run)

    def _set_first_agent_active(self, run: RunRecord) -> None:
        with self._lock:
            if run.request.analysts:
                self._update_status_locked(
                    run, ANALYST_AGENT_NAMES[run.request.analysts[0]], "in_progress"
                )

    def _add_system_message(self, run: RunRecord, content: str) -> None:
        with self._lock:
            item = run.accumulator.add_message("System", content)
            self._emit_locked(run, "message", item)

    def _update_status_locked(self, run: RunRecord, agent: str, status: str) -> None:
        item = run.accumulator.update_agent_status(agent, status)
        if item:
            self._emit_locked(run, "agent_status", item)

    def _update_report_locked(self, run: RunRecord, section: str, content: str) -> None:
        item = run.accumulator.update_report_section(section, content)
        if item:
            request = run.request.model_dump(mode="json")
            self.storage.upsert_section(
                run_id=run.run_id,
                ticker=request["ticker"],
                analysis_date=request["analysis_date"],
                section=section,
                title=item["title"],
                content=content,
            )
            self._emit_locked(run, "report_section", item)

    def _emit_locked(
        self, run: RunRecord, event_type: str, payload: Dict[str, Any]
    ) -> None:
        event = RunEvent(
            seq=len(run.events) + 1,
            run_id=run.run_id,
            type=event_type,
            created_at=_now_iso(),
            payload=deepcopy(payload),
        )
        run.events.append(event)
        self.storage.insert_event(event.model_dump(mode="json"))

    def _acquire_run_lock(self) -> TextIO:
        RUN_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        lock_file = RUN_LOCK_PATH.open("w", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise RuntimeError("Another TradingAgents run is already active") from exc
        lock_file.write(_now_iso())
        lock_file.flush()
        return lock_file

    def _release_run_lock(self, run: RunRecord) -> None:
        if not run.lock_file:
            return
        try:
            fcntl.flock(run.lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            run.lock_file.close()
            run.lock_file = None

    def _portfolio_performance(self, history: List[Dict[str, Any]]) -> Dict[str, Any]:
        snapshot = self.ledger.snapshot()
        return portfolio_performance(
            history,
            self.storage.trades(limit=5000),
            float(snapshot.get("initial_cash", 0.0)),
        )


def encode_sse(event: Dict[str, Any]) -> str:
    return (
        f"id: {event['seq']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    )


async def event_stream(store: RunStore, run_id: str, after: int = 0):
    last_seq = after
    idle_ticks = 0
    while True:
        events = store.get_events_after(run_id, last_seq)
        if events:
            idle_ticks = 0
            for event in events:
                last_seq = event["seq"]
                yield encode_sse(event)
        elif store.is_terminal(run_id):
            idle_ticks += 1
            if idle_ticks > 2:
                break
        await asyncio.sleep(0.25)
