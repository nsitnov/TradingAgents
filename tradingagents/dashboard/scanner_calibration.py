from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from tradingagents.dashboard.performance import trade_performance
from tradingagents.dashboard.storage import DashboardStorage


class ScannerCalibrationReporter:
    def __init__(self, storage: DashboardStorage) -> None:
        self.storage = storage

    def report(self, limit: int = 250) -> Dict[str, Any]:
        events = self.storage.scanner_events(limit=limit)
        signals = self.storage.scanner_signals(limit=limit)
        dislocations = self.storage.scanner_dislocations(limit=limit)
        reviews = self.storage.scanner_confluence_reviews(limit=limit)
        orders = self.storage.orders(limit=limit)
        fills = self.storage.fills(limit=limit)
        trades = self.storage.trades(limit=limit)

        scanner_orders = [order for order in orders if order.get("source") == "scanner_confluence"]
        agent_orders = [order for order in orders if order.get("source") != "scanner_confluence"]
        scanner_run_ids = {order["run_id"] for order in scanner_orders}
        agent_run_ids = {order["run_id"] for order in agent_orders}
        scanner_trades = [trade for trade in trades if trade.get("run_id") in scanner_run_ids]
        agent_trades = [trade for trade in trades if trade.get("run_id") in agent_run_ids]

        paper_candidates = [
            review for review in reviews if review.get("status") == "paper_candidate"
        ]
        executed_reviews = [review for review in reviews if review.get("execution_status")]
        rejected_reviews = [review for review in reviews if review.get("status") == "rejected"]

        return {
            "funnel": _funnel(
                events=events,
                signals=signals,
                dislocations=dislocations,
                reviews=reviews,
                paper_candidates=paper_candidates,
                executed_reviews=executed_reviews,
                rejected_reviews=rejected_reviews,
            ),
            "scanner": _strategy_block(
                name="scanner_confluence",
                orders=scanner_orders,
                fills=[fill for fill in fills if fill.get("run_id") in scanner_run_ids],
                trades=scanner_trades,
            ),
            "baseline": _strategy_block(
                name="single_shot_agents",
                orders=agent_orders,
                fills=[fill for fill in fills if fill.get("run_id") in agent_run_ids],
                trades=agent_trades,
            ),
            "review_quality": _review_quality(reviews),
            "recommendations": _recommendations(
                reviews=reviews,
                scanner_orders=scanner_orders,
                scanner_trades=scanner_trades,
            ),
        }


def _funnel(
    *,
    events: List[Dict[str, Any]],
    signals: List[Dict[str, Any]],
    dislocations: List[Dict[str, Any]],
    reviews: List[Dict[str, Any]],
    paper_candidates: List[Dict[str, Any]],
    executed_reviews: List[Dict[str, Any]],
    rejected_reviews: List[Dict[str, Any]],
) -> Dict[str, Any]:
    dislocated = [item for item in dislocations if item.get("is_dislocated")]
    return {
        "events": len(events),
        "signals": len(signals),
        "dislocations": len(dislocated),
        "confluence_reviews": len(reviews),
        "paper_candidates": len(paper_candidates),
        "paper_executions": len(executed_reviews),
        "rejected_reviews": len(rejected_reviews),
        "signal_per_event": _rate(len(signals), len(events)),
        "dislocation_per_signal": _rate(len(dislocated), len(signals)),
        "candidate_per_dislocation": _rate(len(paper_candidates), len(dislocated)),
        "execution_per_candidate": _rate(len(executed_reviews), len(paper_candidates)),
    }


def _strategy_block(
    *,
    name: str,
    orders: List[Dict[str, Any]],
    fills: List[Dict[str, Any]],
    trades: List[Dict[str, Any]],
) -> Dict[str, Any]:
    order_statuses = Counter(str(order.get("status", "unknown")) for order in orders)
    filled_orders = [order for order in orders if order.get("status") == "filled"]
    rejected_orders = [order for order in orders if order.get("status") == "rejected"]
    filled_notional = sum(float(fill.get("notional", 0.0)) for fill in fills)
    return {
        "name": name,
        "orders": len(orders),
        "filled_orders": len(filled_orders),
        "rejected_orders": len(rejected_orders),
        "fills": len(fills),
        "filled_notional": round(filled_notional, 2),
        "fill_rate": _rate(len(filled_orders), len(orders)),
        "rejection_rate": _rate(len(rejected_orders), len(orders)),
        "order_statuses": dict(order_statuses),
        "performance": trade_performance(trades),
    }


def _review_quality(reviews: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not reviews:
        return {
            "average_score": 0.0,
            "status_counts": {},
            "action_counts": {},
            "execution_counts": {},
        }
    return {
        "average_score": round(
            sum(float(review.get("total_score", 0.0)) for review in reviews) / len(reviews),
            4,
        ),
        "status_counts": dict(Counter(str(review.get("status", "unknown")) for review in reviews)),
        "action_counts": dict(Counter(str(review.get("action", "unknown")) for review in reviews)),
        "execution_counts": dict(
            Counter(str(review.get("execution_status", "none")) for review in reviews)
        ),
    }


def _recommendations(
    *,
    reviews: List[Dict[str, Any]],
    scanner_orders: List[Dict[str, Any]],
    scanner_trades: List[Dict[str, Any]],
) -> List[str]:
    recommendations = []
    quality = _review_quality(reviews)
    performance = trade_performance(scanner_trades)
    if len(reviews) < 20:
        recommendations.append("Collect at least 20 confluence reviews before changing thresholds.")
    if len(scanner_orders) < 10:
        recommendations.append("Run more paper executions before judging scanner edge.")
    if quality["average_score"] and quality["average_score"] < 0.7:
        recommendations.append("Raise min confluence or tighten Z/gap thresholds; average review score is low.")
    if performance["closed_trade_count"] < 5:
        recommendations.append("Wait for at least 5 closed scanner trades before comparing P&L.")
    if performance["closed_trade_count"] >= 5 and performance["profit_factor"] is not None:
        if performance["profit_factor"] < 1.2:
            recommendations.append("Scanner profit factor is weak; keep it paper-only and recalibrate.")
        else:
            recommendations.append("Scanner profit factor is promising; keep paper test running for stability.")
    if not recommendations:
        recommendations.append("No calibration action yet; continue paper collection.")
    return recommendations


def _rate(numerator: int, denominator: int) -> float:
    return round((numerator / denominator), 4) if denominator else 0.0
