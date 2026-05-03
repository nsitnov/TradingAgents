from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from tradingagents.dashboard.performance import max_drawdown
from tradingagents.dashboard.storage import DashboardStorage


class ReadinessReporter:
    def __init__(self, storage: DashboardStorage) -> None:
        self.storage = storage

    def metrics(self) -> Dict[str, Any]:
        orders = self.storage.orders(limit=1000)
        fills = self.storage.fills(limit=1000)
        risk_decisions = self.storage.risk_decisions(limit=1000)
        audit_events = self.storage.audit_events(limit=1000)
        snapshots = self.storage.portfolio_history(limit=1000)
        scanner_reviews = self.storage.scanner_confluence_reviews(limit=1000)
        return {
            "orders_total": len(orders),
            "fills_total": len(fills),
            "risk_rejections_total": len(
                [item for item in risk_decisions if item.get("status") == "rejected"]
            ),
            "audit_events_total": len(audit_events),
            "portfolio_snapshots_total": len(snapshots),
            "scanner_confluence_reviews_total": len(scanner_reviews),
            "scanner_paper_executions_total": len(
                [item for item in scanner_reviews if item.get("execution_status")]
            ),
            "latest_audit_event_at": _latest(audit_events, "created_at"),
            "latest_portfolio_snapshot_at": _latest(snapshots, "created_at"),
            "broker_execution_enabled": False,
        }

    def stability_gate(self) -> Dict[str, Any]:
        orders = self.storage.orders(limit=1000)
        snapshots = list(reversed(self.storage.portfolio_history(limit=1000)))
        risk_decisions = self.storage.risk_decisions(limit=1000)
        closed_postmortems = self.trade_postmortems(limit=1000)["postmortems"]
        equities = [float(item.get("equity", 0.0)) for item in snapshots]
        drawdown = max_drawdown(equities) if equities else 0.0
        paper_days = _paper_days(snapshots, orders)
        rejected = [item for item in risk_decisions if item.get("status") == "rejected"]
        rejection_rate = (len(rejected) / len(risk_decisions)) if risk_decisions else 0.0

        conditions = [
            _condition(
                "broker_execution_disabled",
                True,
                "Broker execution remains disabled.",
            ),
            _condition(
                "paper_history_30_days",
                paper_days >= 30,
                f"Paper history covers {paper_days} days; require at least 30.",
            ),
            _condition(
                "minimum_paper_orders",
                len(orders) >= 20,
                f"Recorded {len(orders)} paper orders; require at least 20.",
            ),
            _condition(
                "minimum_closed_postmortems",
                len(closed_postmortems) >= 5,
                f"Recorded {len(closed_postmortems)} closed trade postmortems; require at least 5.",
            ),
            _condition(
                "risk_rejection_rate",
                rejection_rate <= 0.20,
                f"Risk rejection rate is {rejection_rate:.1%}; require <= 20%.",
            ),
            _condition(
                "max_drawdown_limit",
                drawdown >= -0.10,
                f"Paper max drawdown is {drawdown:.1%}; require no worse than -10%.",
            ),
            _condition(
                "weekly_report_configured",
                bool(os.getenv("RESEND_API_KEY") and os.getenv("TRADINGAGENTS_REPORT_TO")),
                "Weekly Resend report env is configured.",
            ),
        ]
        passed = all(item["passed"] for item in conditions)
        return {
            "status": "ready_for_review" if passed else "blocked",
            "live_trading_allowed": False,
            "reason": (
                "Paper readiness checks passed, but live trading still requires explicit manual decision."
                if passed
                else "Keep running paper mode until all readiness checks pass."
            ),
            "paper_days": paper_days,
            "max_drawdown": drawdown,
            "risk_rejection_rate": round(rejection_rate, 4),
            "conditions": conditions,
        }

    def trade_postmortems(self, limit: int = 100) -> Dict[str, Any]:
        trades = self.storage.trades(limit=1000)
        postmortems = _closed_trade_postmortems(trades)
        return {
            "postmortems": postmortems[:limit],
            "summary": {
                "closed_trades": len(postmortems),
                "wins": len([item for item in postmortems if item["pnl"] > 0]),
                "losses": len([item for item in postmortems if item["pnl"] < 0]),
                "total_pnl": round(sum(item["pnl"] for item in postmortems), 2),
            },
        }


def _closed_trade_postmortems(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    lots: Dict[str, List[Dict[str, Any]]] = {}
    postmortems = []
    for trade in sorted(trades, key=lambda item: _parse_time(item.get("created_at") or item.get("trade_date"))):
        ticker = str(trade.get("ticker", "")).upper()
        action = trade.get("action")
        quantity = float(trade.get("quantity", 0.0))
        price = float(trade.get("price", 0.0))
        if quantity <= 0 or not ticker:
            continue
        if action == "buy":
            lots.setdefault(ticker, []).append(
                {
                    "quantity": quantity,
                    "price": price,
                    "created_at": trade.get("created_at"),
                    "trade_date": trade.get("trade_date"),
                    "run_id": trade.get("run_id"),
                    "source": _source_from_run_id(str(trade.get("run_id", ""))),
                }
            )
        elif action == "sell":
            remaining = quantity
            ticker_lots = lots.setdefault(ticker, [])
            while remaining > 1e-9 and ticker_lots:
                lot = ticker_lots[0]
                matched = min(remaining, float(lot["quantity"]))
                pnl = (price - float(lot["price"])) * matched
                return_pct = (price - float(lot["price"])) / float(lot["price"]) if lot["price"] else 0.0
                postmortems.append(
                    {
                        "ticker": ticker,
                        "source": lot["source"],
                        "entry_date": (lot.get("trade_date") or lot.get("created_at") or "")[:10],
                        "exit_date": (trade.get("trade_date") or trade.get("created_at") or "")[:10],
                        "quantity": matched,
                        "entry_price": float(lot["price"]),
                        "exit_price": price,
                        "pnl": round(pnl, 2),
                        "return_pct": round(return_pct, 4),
                        "verdict": "win" if pnl > 0 else "loss" if pnl < 0 else "flat",
                        "notes": _postmortem_notes(pnl, return_pct, lot["source"]),
                    }
                )
                lot["quantity"] = float(lot["quantity"]) - matched
                remaining -= matched
                if float(lot["quantity"]) <= 1e-9:
                    ticker_lots.pop(0)
    return list(reversed(postmortems))


def _postmortem_notes(pnl: float, return_pct: float, source: str) -> List[str]:
    notes = [f"Source: {source}."]
    if pnl > 0:
        notes.append("Positive closed P&L; keep collecting similar setups.")
    elif pnl < 0:
        notes.append("Negative closed P&L; review entry timing and confluence threshold.")
    else:
        notes.append("Flat result; check whether capital was tied up productively.")
    if abs(return_pct) > 0.05:
        notes.append("Move exceeded 5%; verify slippage assumptions before scaling.")
    return notes


def _source_from_run_id(run_id: str) -> str:
    if run_id.startswith("scanner-confluence-"):
        return "scanner_confluence"
    return "single_shot_agents"


def _condition(name: str, passed: bool, detail: str) -> Dict[str, Any]:
    return {"name": name, "passed": passed, "detail": detail}


def _latest(items: List[Dict[str, Any]], key: str) -> Optional[str]:
    values = [str(item.get(key)) for item in items if item.get(key)]
    return max(values) if values else None


def _paper_days(snapshots: List[Dict[str, Any]], orders: List[Dict[str, Any]]) -> int:
    dates = []
    for item in snapshots:
        if item.get("created_at"):
            dates.append(_parse_time(item["created_at"]))
    for item in orders:
        if item.get("created_at"):
            dates.append(_parse_time(item["created_at"]))
    if not dates:
        return 0
    return max(1, (max(dates).date() - min(dates).date()).days + 1)


def _parse_time(raw: Any) -> datetime:
    value = str(raw or "1970-01-01")
    if "T" not in value:
        value = f"{value[:10]}T00:00:00+00:00"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
