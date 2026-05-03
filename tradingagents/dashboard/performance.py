from __future__ import annotations

from datetime import datetime
from math import sqrt
from typing import Any, Dict, List


def portfolio_performance(
    history: List[Dict[str, Any]], trades: List[Dict[str, Any]], initial_cash: float
) -> Dict[str, Any]:
    if not history:
        return {
            "start_equity": initial_cash,
            "end_equity": initial_cash,
            "total_pnl": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown": 0.0,
            "snapshot_count": 0,
            **trade_performance(trades),
        }

    equities = [float(item.get("equity", 0.0)) for item in history]
    start_equity = equities[0]
    end_equity = equities[-1]
    returns = _period_returns(equities)
    total_pnl = end_equity - start_equity
    return {
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_pnl": total_pnl,
        "total_return_pct": (total_pnl / start_equity) if start_equity else 0.0,
        "max_drawdown": max_drawdown(equities),
        "volatility": _stdev(returns),
        "sharpe_like": _sharpe_like(returns),
        "snapshot_count": len(history),
        **trade_performance(trades),
    }


def trade_performance(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    closed = _closed_trade_pnls(trades)
    wins = [pnl for pnl in closed if pnl > 0]
    losses = [pnl for pnl in closed if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trade_count": len(trades),
        "closed_trade_count": len(closed),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": (len(wins) / len(closed)) if closed else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
        "average_closed_trade_pnl": (sum(closed) / len(closed)) if closed else 0.0,
    }


def max_drawdown(equities: List[float]) -> float:
    if not equities:
        return 0.0
    peak = equities[0]
    worst = 0.0
    for equity in equities:
        peak = max(peak, equity)
        if peak:
            worst = min(worst, (equity - peak) / peak)
    return worst


def _closed_trade_pnls(trades: List[Dict[str, Any]]) -> List[float]:
    lots: Dict[str, List[List[float]]] = {}
    realized: List[float] = []
    for trade in sorted(trades, key=lambda item: _trade_time(item)):
        ticker = str(trade.get("ticker", "")).upper()
        action = trade.get("action")
        quantity = float(trade.get("quantity", 0.0))
        price = float(trade.get("price", 0.0))
        if quantity <= 0:
            continue
        if action == "buy":
            lots.setdefault(ticker, []).append([quantity, price])
        elif action == "sell":
            remaining = quantity
            ticker_lots = lots.setdefault(ticker, [])
            while remaining > 1e-9 and ticker_lots:
                lot_qty, lot_price = ticker_lots[0]
                matched = min(remaining, lot_qty)
                realized.append((price - lot_price) * matched)
                lot_qty -= matched
                remaining -= matched
                if lot_qty <= 1e-9:
                    ticker_lots.pop(0)
                else:
                    ticker_lots[0][0] = lot_qty
    return realized


def _trade_time(trade: Dict[str, Any]) -> datetime:
    raw = trade.get("created_at") or trade.get("trade_date") or "1970-01-01"
    if "T" not in raw:
        raw = f"{raw}T00:00:00+00:00"
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _period_returns(equities: List[float]) -> List[float]:
    returns = []
    for previous, current in zip(equities, equities[1:]):
        if previous:
            returns.append((current - previous) / previous)
    return returns


def _stdev(values: List[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return sqrt(variance)


def _sharpe_like(returns: List[float]) -> float:
    volatility = _stdev(returns)
    if not returns or not volatility:
        return 0.0
    return (sum(returns) / len(returns)) / volatility
