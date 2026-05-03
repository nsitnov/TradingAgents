from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

from tradingagents.dashboard.performance import portfolio_performance
from tradingagents.dashboard.readiness import ReadinessReporter
from tradingagents.dashboard.scanner_calibration import ScannerCalibrationReporter
from tradingagents.dashboard.storage import DashboardStorage


DEFAULT_PROGRESS_TIMEZONE = "Europe/Sofia"
DEFAULT_WEEKLY_COST_BUDGET_USD = 35.0

STATUS_IMPROVING = "improving"
STATUS_FLAT = "flat"
STATUS_DEGRADING = "degrading"
STATUS_INSUFFICIENT = "insufficient_data"


@dataclass(frozen=True)
class ProgressPeriod:
    start: datetime
    end: datetime
    timezone_name: str

    def as_dict(self) -> Dict[str, str]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "timezone": self.timezone_name,
            "label": (
                f"{self.start.strftime('%Y-%m-%d')} to "
                f"{self.end.strftime('%Y-%m-%d %H:%M')} {self.timezone_name}"
            ),
        }


class ProgressReporter:
    def __init__(
        self,
        storage: DashboardStorage,
        *,
        scanner_calibration: Optional[ScannerCalibrationReporter] = None,
        readiness: Optional[ReadinessReporter] = None,
        timezone_name: str = DEFAULT_PROGRESS_TIMEZONE,
        weekly_cost_budget_usd: float = DEFAULT_WEEKLY_COST_BUDGET_USD,
    ) -> None:
        self.storage = storage
        self.scanner_calibration = scanner_calibration or ScannerCalibrationReporter(storage)
        self.readiness = readiness or ReadinessReporter(storage)
        self.timezone_name = timezone_name
        self.weekly_cost_budget_usd = weekly_cost_budget_usd

    def weekly(self, *, as_of: Optional[datetime] = None) -> Dict[str, Any]:
        period = progress_period(as_of=as_of, timezone_name=self.timezone_name)
        portfolio = self._portfolio(period)
        backtests = self._backtests(period)
        replays = self._agent_replays(period)
        scanner = self._scanner()
        risk = self._risk()
        autopilot = self._autopilot(period)
        costs = self._costs(period)
        llm = self._llm_usage(period)

        profitability_status = self._profitability_status(portfolio, backtests, replays)
        autopilot_status = self._autopilot_status(autopilot)
        scanner_status = self._scanner_status(scanner)
        risk_status = self._risk_status(risk)
        cost_status = self._cost_status(costs)
        overall_status = self._overall_status(
            profitability_status=profitability_status,
            autopilot_status=autopilot_status,
            scanner_status=scanner_status,
            risk_status=risk_status,
            cost_status=cost_status,
        )

        statuses = {
            "overall": overall_status,
            "profitability": profitability_status,
            "autopilot": autopilot_status,
            "scanner": scanner_status,
            "risk": risk_status,
            "cost": cost_status,
        }
        return {
            "period": period.as_dict(),
            "overall_status": overall_status,
            "statuses": statuses,
            "score": self._score(statuses),
            "portfolio": portfolio,
            "backtests": backtests,
            "agent_replays": replays,
            "scanner": scanner,
            "risk": risk,
            "autopilot": autopilot,
            "costs": costs,
            "llm": llm,
            "recommendations": self._recommendations(
                statuses=statuses,
                portfolio=portfolio,
                backtests=backtests,
                scanner=scanner,
                risk=risk,
                autopilot=autopilot,
                costs=costs,
            ),
        }

    def history(self, *, weeks: int = 8, as_of: Optional[datetime] = None) -> Dict[str, Any]:
        weeks = max(1, min(int(weeks), 52))
        tz = ZoneInfo(self.timezone_name)
        current = as_of or datetime.now(tz)
        if current.tzinfo is None:
            current = current.replace(tzinfo=tz)
        current = current.astimezone(tz)
        rows = []
        for offset in range(weeks):
            row = self.weekly(as_of=current - timedelta(days=offset * 7))
            rows.append(
                {
                    "period": row["period"],
                    "overall_status": row["overall_status"],
                    "score": row["score"],
                    "total_return_pct": row["portfolio"]["performance"]["total_return_pct"],
                    "total_pnl": row["portfolio"]["performance"]["total_pnl"],
                    "max_drawdown": row["portfolio"]["performance"]["max_drawdown"],
                    "orders": row["autopilot"]["paper_orders"],
                    "autopilot_jobs": row["autopilot"]["jobs"],
                    "openai_cost_usd": row["costs"]["total_usd"],
                }
            )
        return {"weeks": list(reversed(rows))}

    def _portfolio(self, period: ProgressPeriod) -> Dict[str, Any]:
        all_history = _rows_with_time(
            self.storage.portfolio_history(limit=5000),
            period,
            keys=("created_at", "updated_at"),
        )
        snapshots_before_end = [
            item for item in all_history if item["_time"] <= period.end
        ]
        snapshots_in_period = [
            item for item in snapshots_before_end if period.start <= item["_time"] <= period.end
        ]
        start_snapshot = _latest_at_or_before(all_history, period.start)
        if not start_snapshot and snapshots_in_period:
            start_snapshot = snapshots_in_period[0]
        end_snapshot = snapshots_before_end[-1] if snapshots_before_end else None
        history = _portfolio_series(start_snapshot, snapshots_in_period, end_snapshot)
        trades = _filter_by_time(
            self.storage.trades(limit=5000),
            period,
            keys=("created_at", "trade_date"),
        )
        start_equity = float(history[0].get("equity", 0.0)) if history else 0.0
        performance = portfolio_performance(
            history,
            trades,
            start_equity,
            benchmarks=(),
        )
        return {
            "snapshot_count": len(snapshots_in_period),
            "performance_snapshot_count": len(history),
            "trade_count": len(trades),
            "performance": performance,
        }

    def _backtests(self, period: ProgressPeriod) -> Dict[str, Any]:
        rows = _filter_by_time(
            self.storage.backtests(limit=200),
            period,
            keys=("started_at", "ended_at"),
        )
        completed = [row for row in rows if row.get("status") == "completed"]
        latest_detail = None
        if completed:
            latest_detail = self.storage.backtest_detail(completed[-1]["backtest_id"])
        benchmark_alpha = _benchmark_alpha(latest_detail.get("result", {}) if latest_detail else {})
        return {
            "jobs": len(rows),
            "completed": len(completed),
            "errors": len([row for row in rows if row.get("status") == "error"]),
            "latest": latest_detail,
            "benchmark_alpha": benchmark_alpha,
        }

    def _agent_replays(self, period: ProgressPeriod) -> Dict[str, Any]:
        rows = _filter_by_time(
            self.storage.agent_replay_jobs(limit=200),
            period,
            keys=("started_at", "ended_at"),
        )
        completed = [row for row in rows if row.get("status") == "completed"]
        latest_detail = None
        if completed:
            latest_detail = self.storage.agent_replay_job_detail(completed[-1]["job_id"])
        return {
            "jobs": len(rows),
            "completed": len(completed),
            "errors": len([row for row in rows if row.get("status") == "error"]),
            "latest": latest_detail,
        }

    def _scanner(self) -> Dict[str, Any]:
        report = self.scanner_calibration.report(limit=1000)
        scanner = report.get("scanner", {})
        perf = scanner.get("performance", {})
        report["closed_trade_count"] = perf.get("closed_trade_count", 0)
        report["profit_factor"] = perf.get("profit_factor")
        report["orders"] = scanner.get("orders", 0)
        report["filled_orders"] = scanner.get("filled_orders", 0)
        report["rejection_rate"] = scanner.get("rejection_rate", 0.0)
        return report

    def _risk(self) -> Dict[str, Any]:
        gate = self.readiness.stability_gate()
        metrics = self.readiness.metrics()
        return {"gate": gate, "metrics": metrics}

    def _autopilot(self, period: ProgressPeriod) -> Dict[str, Any]:
        jobs = _filter_by_time(
            self.storage.autopilot_jobs(limit=500),
            period,
            keys=("started_at", "ended_at"),
        )
        completed = [job for job in jobs if job.get("status") == "completed"]
        errored = [job for job in jobs if job.get("status") == "error"]
        paper_orders = sum(
            int(_nested(job, ("result", "summary", "paper_executions"), 0) or 0)
            for job in jobs
        )
        scanner_signals = sum(
            int(_nested(job, ("result", "summary", "scanner_signals"), 0) or 0)
            for job in jobs
        )
        error_rate = len(errored) / len(jobs) if jobs else 0.0
        return {
            "jobs": len(jobs),
            "completed": len(completed),
            "errors": len(errored),
            "error_rate": round(error_rate, 4),
            "paper_orders": paper_orders,
            "scanner_signals": scanner_signals,
            "latest": jobs[-1] if jobs else None,
        }

    def _costs(self, period: ProgressPeriod) -> Dict[str, Any]:
        costs = [
            cost
            for cost in self.storage.openai_costs(limit=1000)
            if _cost_in_period(cost, period)
        ]
        total = round(sum(float(cost.get("amount", 0.0)) for cost in costs), 4)
        return {
            "rows": len(costs),
            "total_usd": total,
            "budget_usd": self.weekly_cost_budget_usd,
            "budget_used_pct": (
                round(total / self.weekly_cost_budget_usd, 4)
                if self.weekly_cost_budget_usd
                else 0.0
            ),
        }

    def _llm_usage(self, period: ProgressPeriod) -> Dict[str, Any]:
        runs = _filter_by_time(
            self.storage.recent_runs(limit=500),
            period,
            keys=("started_at", "ended_at"),
        )
        quick_providers: Dict[str, int] = {}
        critical_providers: Dict[str, int] = {}
        fallback_count = 0
        for run in runs:
            stats = run.get("stats", {})
            routing = stats.get("llm_routing") or {}
            quick = str(
                routing.get("quick_llm_provider")
                or run.get("request", {}).get("quick_llm_provider")
                or run.get("request", {}).get("llm_provider", "unknown")
            )
            critical = str(
                routing.get("critical_llm_provider")
                or run.get("request", {}).get("critical_llm_provider")
                or run.get("request", {}).get("llm_provider", "unknown")
            )
            quick_providers[quick] = quick_providers.get(quick, 0) + 1
            critical_providers[critical] = critical_providers.get(critical, 0) + 1
            fallback_count += int(stats.get("llm_fallbacks", 0) or 0)
        return {
            "runs": len(runs),
            "quick_providers": quick_providers,
            "critical_providers": critical_providers,
            "fallback_count": fallback_count,
        }

    def _profitability_status(
        self,
        portfolio: Dict[str, Any],
        backtests: Dict[str, Any],
        replays: Dict[str, Any],
    ) -> str:
        perf = portfolio["performance"]
        closed_trades = int(perf.get("closed_trade_count", 0) or 0)
        sufficient = (
            portfolio["performance_snapshot_count"] >= 2
            and (
                closed_trades >= 3
                or backtests["completed"] >= 1
                or replays["completed"] >= 1
            )
        )
        if not sufficient:
            return STATUS_INSUFFICIENT
        alpha_values = [
            value for value in backtests.get("benchmark_alpha", {}).values() if value is not None
        ]
        max_drawdown = float(perf.get("max_drawdown", 0.0) or 0.0)
        total_return = float(perf.get("total_return_pct", 0.0) or 0.0)
        if max_drawdown < -0.10:
            return STATUS_DEGRADING
        if alpha_values:
            if all(value < 0 for value in alpha_values):
                return STATUS_DEGRADING
            if any(value > 0 for value in alpha_values):
                return STATUS_IMPROVING
        if total_return < 0:
            return STATUS_DEGRADING
        if total_return > 0:
            return STATUS_IMPROVING
        return STATUS_FLAT

    def _autopilot_status(self, autopilot: Dict[str, Any]) -> str:
        if autopilot["jobs"] == 0:
            return STATUS_INSUFFICIENT
        if autopilot["error_rate"] > 0.25:
            return STATUS_DEGRADING
        if autopilot["completed"] > 0:
            return STATUS_IMPROVING
        return STATUS_FLAT

    def _scanner_status(self, scanner: Dict[str, Any]) -> str:
        if scanner.get("orders", 0) < 5 and scanner.get("funnel", {}).get("confluence_reviews", 0) < 20:
            return STATUS_INSUFFICIENT
        if float(scanner.get("rejection_rate", 0.0) or 0.0) > 0.35:
            return STATUS_DEGRADING
        closed = int(scanner.get("closed_trade_count", 0) or 0)
        profit_factor = scanner.get("profit_factor")
        if closed >= 5 and profit_factor is not None:
            return STATUS_IMPROVING if float(profit_factor) >= 1.2 else STATUS_DEGRADING
        return STATUS_FLAT

    def _risk_status(self, risk: Dict[str, Any]) -> str:
        gate = risk["gate"]
        if float(gate.get("max_drawdown", 0.0) or 0.0) < -0.10:
            return STATUS_DEGRADING
        if float(gate.get("risk_rejection_rate", 0.0) or 0.0) > 0.20:
            return STATUS_DEGRADING
        return STATUS_IMPROVING if gate.get("status") == "ready_for_review" else STATUS_FLAT

    def _cost_status(self, costs: Dict[str, Any]) -> str:
        if costs["rows"] == 0:
            return STATUS_INSUFFICIENT
        if costs["total_usd"] > costs["budget_usd"]:
            return STATUS_DEGRADING
        return STATUS_IMPROVING

    def _overall_status(
        self,
        *,
        profitability_status: str,
        autopilot_status: str,
        scanner_status: str,
        risk_status: str,
        cost_status: str,
    ) -> str:
        if profitability_status == STATUS_DEGRADING or risk_status == STATUS_DEGRADING:
            return STATUS_DEGRADING
        if profitability_status == STATUS_INSUFFICIENT:
            return STATUS_INSUFFICIENT
        if STATUS_DEGRADING in {autopilot_status, scanner_status, cost_status}:
            return STATUS_FLAT
        if profitability_status == STATUS_IMPROVING:
            return STATUS_IMPROVING
        return STATUS_FLAT

    def _score(self, statuses: Dict[str, str]) -> int:
        weights = {
            "profitability": 35,
            "risk": 25,
            "autopilot": 15,
            "scanner": 15,
            "cost": 10,
        }
        points = 0.0
        multipliers = {
            STATUS_IMPROVING: 1.0,
            STATUS_FLAT: 0.65,
            STATUS_INSUFFICIENT: 0.35,
            STATUS_DEGRADING: 0.0,
        }
        for key, weight in weights.items():
            points += weight * multipliers.get(statuses.get(key), 0.0)
        return int(round(points))

    def _recommendations(
        self,
        *,
        statuses: Dict[str, str],
        portfolio: Dict[str, Any],
        backtests: Dict[str, Any],
        scanner: Dict[str, Any],
        risk: Dict[str, Any],
        autopilot: Dict[str, Any],
        costs: Dict[str, Any],
    ) -> List[str]:
        recommendations: List[str] = []
        if statuses["profitability"] == STATUS_INSUFFICIENT:
            recommendations.append(
                "Keep collecting paper data; require either 3 closed trades or at least one completed backtest/replay this week."
            )
        elif statuses["profitability"] == STATUS_DEGRADING:
            recommendations.append(
                "Do not scale automation; review losing tickers, recent agent decisions, and benchmark alpha before adding risk."
            )
        elif statuses["profitability"] == STATUS_IMPROVING:
            recommendations.append(
                "Profitability is improving; keep paper-only mode and confirm the edge across another weekly cycle."
            )

        if statuses["risk"] == STATUS_DEGRADING:
            recommendations.append(
                "Risk profile is deteriorating; keep the kill-switch posture and tighten position sizing until drawdown/rejections normalize."
            )
        elif risk["gate"].get("status") == "blocked":
            recommendations.append("Production readiness is still blocked; live trading remains disabled by design.")

        if statuses["autopilot"] == STATUS_INSUFFICIENT:
            recommendations.append("Autopilot has no weekly jobs; confirm the systemd timer is firing.")
        elif statuses["autopilot"] == STATUS_DEGRADING:
            recommendations.append("Autopilot error rate is high; inspect latest failed job before relying on weekly signals.")

        if statuses["scanner"] == STATUS_INSUFFICIENT:
            recommendations.append("Scanner still needs more confluence reviews and paper executions before judging signal quality.")
        elif statuses["scanner"] == STATUS_DEGRADING:
            recommendations.append("Scanner quality is weak; raise thresholds or pause scanner executions until calibration improves.")

        if statuses["cost"] == STATUS_DEGRADING:
            recommendations.append(
                f"OpenAI spend exceeded the weekly budget (${costs['total_usd']:.2f} / ${costs['budget_usd']:.2f}); reduce replay fanout or model depth."
            )

        if backtests["completed"] == 0:
            recommendations.append("Run at least one weekly backtest/replay batch so benchmark alpha can be measured.")
        if not recommendations:
            recommendations.append("No action required; continue the current paper-autopilot collection cycle.")
        return recommendations[:6]


def progress_period(
    *,
    as_of: Optional[datetime] = None,
    timezone_name: str = DEFAULT_PROGRESS_TIMEZONE,
) -> ProgressPeriod:
    tz = ZoneInfo(timezone_name)
    current = as_of or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    current = current.astimezone(tz)
    start_date = current.date() - timedelta(days=current.weekday())
    return ProgressPeriod(
        start=datetime.combine(start_date, time.min, tzinfo=tz),
        end=current,
        timezone_name=timezone_name,
    )


def _filter_by_time(
    rows: Iterable[Dict[str, Any]],
    period: ProgressPeriod,
    *,
    keys: Sequence[str],
) -> List[Dict[str, Any]]:
    return [
        {key: value for key, value in row.items() if key != "_time"}
        for row in _rows_with_time(rows, period, keys=keys)
        if period.start <= row["_time"] <= period.end
    ]


def _rows_with_time(
    rows: Iterable[Dict[str, Any]],
    period: ProgressPeriod,
    *,
    keys: Sequence[str],
) -> List[Dict[str, Any]]:
    parsed = []
    for row in rows:
        when = None
        for key in keys:
            if row.get(key):
                when = _parse_time(row[key]).astimezone(ZoneInfo(period.timezone_name))
                break
        if when:
            item = dict(row)
            item["_time"] = when
            parsed.append(item)
    return sorted(parsed, key=lambda item: item["_time"])


def _latest_at_or_before(
    rows: Sequence[Dict[str, Any]],
    when: datetime,
) -> Optional[Dict[str, Any]]:
    candidates = [row for row in rows if row["_time"] <= when]
    return candidates[-1] if candidates else None


def _portfolio_series(
    start_snapshot: Optional[Dict[str, Any]],
    snapshots_in_period: Sequence[Dict[str, Any]],
    end_snapshot: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for snapshot in [start_snapshot, *snapshots_in_period, end_snapshot]:
        if not snapshot:
            continue
        identity = (snapshot.get("id"), snapshot.get("created_at"))
        if identity in seen:
            continue
        seen.add(identity)
        rows.append({key: value for key, value in snapshot.items() if key != "_time"})
    return rows


def _parse_time(raw: Any) -> datetime:
    value = str(raw or "1970-01-01")
    if "T" not in value:
        value = f"{value[:10]}T00:00:00+00:00"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cost_in_period(cost: Dict[str, Any], period: ProgressPeriod) -> bool:
    start_time = int(cost.get("start_time", 0) or 0)
    if not start_time:
        return False
    when = datetime.fromtimestamp(start_time, tz=timezone.utc).astimezone(
        ZoneInfo(period.timezone_name)
    )
    return period.start <= when <= period.end


def _benchmark_alpha(result: Dict[str, Any]) -> Dict[str, Optional[float]]:
    benchmarks = result.get("performance", {}).get("benchmarks", [])
    alpha: Dict[str, Optional[float]] = {"SPY": None, "QQQ": None}
    for row in benchmarks:
        ticker = str(row.get("ticker", "")).upper()
        if ticker in alpha and row.get("alpha_pct") is not None:
            alpha[ticker] = float(row["alpha_pct"])
    return alpha


def _nested(row: Dict[str, Any], path: Sequence[str], default: Any = None) -> Any:
    current: Any = row
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current
