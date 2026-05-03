from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import yfinance as yf

from tradingagents.dashboard.storage import DashboardStorage


DEFAULT_LEDGER_PATH = (
    Path.home() / ".tradingagents" / "dashboard" / "ledger.json"
)
DEFAULT_INITIAL_CASH = 100_000.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_ledger(initial_cash: float = DEFAULT_INITIAL_CASH) -> Dict[str, Any]:
    return {
        "cash": float(initial_cash),
        "initial_cash": float(initial_cash),
        "positions": {},
        "trades": [],
        "updated_at": _now_iso(),
    }


class PaperLedger:
    """Small JSON-backed paper ledger for dashboard P&L.

    The ledger intentionally avoids broker integration. Decisions are mapped to
    deterministic paper trades so the dashboard can show positions immediately.
    """

    def __init__(
        self,
        path: Path = DEFAULT_LEDGER_PATH,
        price_provider: Optional[Callable[[str], float]] = None,
        initial_cash: float = DEFAULT_INITIAL_CASH,
        storage: Optional[DashboardStorage] = None,
    ) -> None:
        self.path = Path(path)
        self.initial_cash = float(initial_cash)
        self._price_provider = price_provider or self._latest_close
        self.storage = storage
        self._lock = threading.Lock()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            ledger = self._load()
            self._recalculate_totals(ledger)
            return deepcopy(ledger)

    def sync_to_storage(self) -> None:
        if not self.storage:
            return
        with self._lock:
            ledger = self._load()
            self._recalculate_totals(ledger)
            for trade in ledger.get("trades", []):
                self.storage.insert_trade(trade)
            self.storage.insert_portfolio_snapshot(ledger, run_id="ledger-sync")

    def apply_decision(
        self,
        *,
        ticker: str,
        decision: str,
        trade_date: str,
        run_id: str,
    ) -> Dict[str, Any]:
        ticker = ticker.upper().strip()
        decision = (decision or "Hold").strip()

        with self._lock:
            ledger = self._load()
            price = self._price_provider(ticker)
            if price <= 0:
                raise ValueError(f"Could not resolve a positive price for {ticker}")

            position = ledger["positions"].setdefault(
                ticker,
                {
                    "quantity": 0.0,
                    "avg_cost": 0.0,
                    "realized_pnl": 0.0,
                    "last_price": price,
                    "market_value": 0.0,
                    "unrealized_pnl": 0.0,
                },
            )

            action = "hold"
            quantity = 0.0
            cash_before = float(ledger["cash"])
            current_qty = float(position["quantity"])

            if decision == "Buy":
                quantity = (cash_before * 0.20) / price
                action = "buy"
            elif decision == "Overweight":
                quantity = (cash_before * 0.10) / price
                action = "buy"
            elif decision == "Underweight" and current_qty > 0:
                quantity = current_qty * 0.50
                action = "sell"
            elif decision == "Sell" and current_qty > 0:
                quantity = current_qty
                action = "sell"

            if action == "buy" and quantity > 0:
                cost = quantity * price
                new_qty = current_qty + quantity
                current_cost = current_qty * float(position["avg_cost"])
                position["avg_cost"] = (current_cost + cost) / new_qty
                position["quantity"] = new_qty
                ledger["cash"] = cash_before - cost
            elif action == "sell" and quantity > 0:
                proceeds = quantity * price
                realized = (price - float(position["avg_cost"])) * quantity
                position["quantity"] = max(0.0, current_qty - quantity)
                position["realized_pnl"] = float(position["realized_pnl"]) + realized
                ledger["cash"] = cash_before + proceeds

            position["last_price"] = price
            self._recalculate_position(position)
            if position["quantity"] <= 1e-9:
                position["quantity"] = 0.0

            trade = {
                "run_id": run_id,
                "ticker": ticker,
                "decision": decision,
                "action": action,
                "quantity": quantity,
                "price": price,
                "trade_date": trade_date,
                "created_at": _now_iso(),
            }
            ledger["trades"].append(trade)
            self._recalculate_totals(ledger)
            self._save(ledger)
            if self.storage:
                self.storage.insert_trade(trade)
                self.storage.insert_portfolio_snapshot(ledger, run_id=run_id)
            return deepcopy(ledger)

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return _empty_ledger(self.initial_cash)
        with self.path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("cash", self.initial_cash)
        data.setdefault("initial_cash", self.initial_cash)
        data.setdefault("positions", {})
        data.setdefault("trades", [])
        return data

    def _save(self, ledger: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ledger["updated_at"] = _now_iso()
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2)

    def _latest_close(self, ticker: str) -> float:
        history = yf.Ticker(ticker).history(period="5d")
        if history.empty or "Close" not in history:
            raise ValueError(f"No recent close price for {ticker}")
        return float(history["Close"].dropna().iloc[-1])

    def _recalculate_position(self, position: Dict[str, Any]) -> None:
        qty = float(position.get("quantity", 0.0))
        price = float(position.get("last_price", 0.0))
        avg_cost = float(position.get("avg_cost", 0.0))
        position["market_value"] = qty * price
        position["unrealized_pnl"] = (price - avg_cost) * qty

    def _recalculate_totals(self, ledger: Dict[str, Any]) -> None:
        total_market_value = 0.0
        total_realized = 0.0
        total_unrealized = 0.0
        for position in ledger["positions"].values():
            self._recalculate_position(position)
            total_market_value += float(position.get("market_value", 0.0))
            total_realized += float(position.get("realized_pnl", 0.0))
            total_unrealized += float(position.get("unrealized_pnl", 0.0))

        equity = float(ledger["cash"]) + total_market_value
        ledger["market_value"] = total_market_value
        ledger["equity"] = equity
        ledger["realized_pnl"] = total_realized
        ledger["unrealized_pnl"] = total_unrealized
        ledger["total_pnl"] = equity - float(ledger.get("initial_cash", self.initial_cash))
