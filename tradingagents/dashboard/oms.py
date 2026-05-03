from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, List, Optional

from tradingagents.dashboard.brokers import BrokerAdapter, broker_from_env
from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.storage import DashboardStorage, now_iso


ALLOWED_MODES = {"DEMO", "PAPER", "LIVE_DISABLED"}


@dataclass(frozen=True)
class RiskConfig:
    mode: str = "PAPER"
    require_manual_approval: bool = False
    max_position_pct: float = 0.25
    max_trade_notional: float = 25_000.0
    max_daily_trades: int = 20
    daily_loss_limit: float = 5_000.0
    forbidden_tickers: tuple[str, ...] = ()

    @classmethod
    def from_env(cls) -> "RiskConfig":
        mode = os.getenv("TRADINGAGENTS_TRADING_MODE", "PAPER").strip().upper()
        if mode not in ALLOWED_MODES:
            mode = "LIVE_DISABLED"
        forbidden = tuple(
            sorted(
                {
                    item.strip().upper()
                    for item in os.getenv("TRADINGAGENTS_FORBIDDEN_TICKERS", "").split(",")
                    if item.strip()
                }
            )
        )
        return cls(
            mode=mode,
            require_manual_approval=_env_bool("TRADINGAGENTS_REQUIRE_ORDER_APPROVAL", False),
            max_position_pct=_env_float("TRADINGAGENTS_MAX_POSITION_PCT", 0.25),
            max_trade_notional=_env_float("TRADINGAGENTS_MAX_TRADE_NOTIONAL", 25_000.0),
            max_daily_trades=_env_int("TRADINGAGENTS_MAX_DAILY_TRADES", 20),
            daily_loss_limit=_env_float("TRADINGAGENTS_DAILY_LOSS_LIMIT", 5_000.0),
            forbidden_tickers=forbidden,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "require_manual_approval": self.require_manual_approval,
            "max_position_pct": self.max_position_pct,
            "max_trade_notional": self.max_trade_notional,
            "max_daily_trades": self.max_daily_trades,
            "daily_loss_limit": self.daily_loss_limit,
            "forbidden_tickers": list(self.forbidden_tickers),
        }


@dataclass(frozen=True)
class OrderIntent:
    run_id: str
    ticker: str
    decision: str
    trade_date: str
    source: str = "agent"

    @property
    def idempotency_key(self) -> str:
        return ":".join(
            [
                self.run_id,
                self.ticker.upper(),
                self.decision.strip() or "Hold",
                self.trade_date,
                self.source,
            ]
        )


class RiskEngine:
    def __init__(self, storage: DashboardStorage, config: Optional[RiskConfig] = None) -> None:
        self.storage = storage
        self.config = config or RiskConfig.from_env()

    def evaluate(self, order: Dict[str, Any], portfolio: Dict[str, Any]) -> Dict[str, Any]:
        checks: List[Dict[str, Any]] = []
        rejected = False

        def check(name: str, passed: bool, detail: str, value: Any = None, limit: Any = None) -> None:
            nonlocal rejected
            checks.append(
                {
                    "name": name,
                    "passed": passed,
                    "detail": detail,
                    "value": value,
                    "limit": limit,
                }
            )
            if not passed:
                rejected = True

        action = order["action"]
        ticker = order["ticker"].upper()
        equity = float(portfolio.get("equity", 0.0))
        notional = float(order.get("estimated_notional", 0.0))

        check(
            "mode",
            self.config.mode in ALLOWED_MODES and self.config.mode != "LIVE",
            f"Trading mode is {self.config.mode}",
            self.config.mode,
            "DEMO/PAPER/LIVE_DISABLED",
        )
        check(
            "forbidden_ticker",
            ticker not in self.config.forbidden_tickers,
            f"{ticker} is not forbidden",
            ticker,
            list(self.config.forbidden_tickers),
        )
        check(
            "max_trade_notional",
            action == "hold" or notional <= self.config.max_trade_notional,
            "Estimated order notional is within the configured cap",
            notional,
            self.config.max_trade_notional,
        )

        projected_pct = 0.0
        if equity > 0:
            projected_pct = float(order.get("projected_position_market_value", 0.0)) / equity
        check(
            "max_position_pct",
            action != "buy" or projected_pct <= self.config.max_position_pct,
            "Projected ticker exposure is within the configured cap",
            projected_pct,
            self.config.max_position_pct,
        )

        today = date.today().isoformat()
        daily_trades = self.storage.count_orders_on_date(
            today,
            actions=["buy", "sell"],
            statuses=["submitted", "filled", "pending_approval"],
        )
        check(
            "max_daily_trades",
            action == "hold" or daily_trades < self.config.max_daily_trades,
            "Daily trade count is below the configured cap",
            daily_trades,
            self.config.max_daily_trades,
        )

        daily_pnl = self.storage.portfolio_pnl_since(today)
        check(
            "daily_loss_limit",
            daily_pnl >= -self.config.daily_loss_limit,
            "Daily P&L is above the loss limit",
            daily_pnl,
            -self.config.daily_loss_limit,
        )

        return {
            "status": "rejected" if rejected else "approved",
            "reason": _rejection_reason(checks),
            "checks": checks,
            "config": self.config.as_dict(),
        }


class PaperOrderService:
    def __init__(
        self,
        *,
        ledger: PaperLedger,
        storage: DashboardStorage,
        risk_config: Optional[RiskConfig] = None,
        broker: Optional[BrokerAdapter] = None,
    ) -> None:
        self.ledger = ledger
        self.storage = storage
        self.risk_config = risk_config or RiskConfig.from_env()
        self.risk_engine = RiskEngine(storage, self.risk_config)
        self.broker = broker if broker is not None else broker_from_env()

    def submit_decision(self, intent: OrderIntent) -> Dict[str, Any]:
        existing = self.storage.order_by_idempotency_key(intent.idempotency_key)
        if existing:
            return {"order": existing, "risk": None, "portfolio": self.ledger.snapshot()}

        preview = self.ledger.preview_decision(ticker=intent.ticker, decision=intent.decision)
        order = self._new_order(intent, preview)
        self.storage.upsert_order(order)
        self.storage.insert_audit_event(
            "order_created",
            "order",
            order["order_id"],
            order["run_id"],
            order,
        )

        portfolio = self.ledger.snapshot()
        risk = self.risk_engine.evaluate(order, portfolio)
        self.storage.insert_risk_decision(order["order_id"], order["run_id"], risk)

        if risk["status"] != "approved":
            order["status"] = "rejected"
            order["reason"] = risk["reason"]
            order["updated_at"] = now_iso()
            self.storage.upsert_order(order)
            self.storage.insert_audit_event(
                "order_rejected",
                "order",
                order["order_id"],
                order["run_id"],
                {"reason": risk["reason"], "checks": risk["checks"]},
            )
            return {"order": order, "risk": risk, "portfolio": portfolio}

        if self.risk_config.mode == "DEMO":
            order["status"] = "simulated"
            order["updated_at"] = now_iso()
            self.storage.upsert_order(order)
            self.storage.insert_audit_event(
                "order_simulated", "order", order["order_id"], order["run_id"], order
            )
            return {"order": order, "risk": risk, "portfolio": portfolio}

        if self.risk_config.mode == "LIVE_DISABLED" or self.risk_config.require_manual_approval:
            order["status"] = "pending_approval"
            order["updated_at"] = now_iso()
            self.storage.upsert_order(order)
            self.storage.insert_audit_event(
                "order_pending_approval",
                "order",
                order["order_id"],
                order["run_id"],
                order,
            )
            return {"order": order, "risk": risk, "portfolio": portfolio}

        if self.broker:
            return self.submit_broker_order(order, risk=risk)
        return self.execute_order(order, risk=risk)

    def approve_order(self, order_id: str) -> Dict[str, Any]:
        order = self.storage.order_detail(order_id)
        if not order:
            raise ValueError(f"Order not found: {order_id}")
        if order["status"] != "pending_approval":
            raise ValueError(f"Order is not pending approval: {order['status']}")
        if self.risk_config.mode == "LIVE_DISABLED":
            raise ValueError("Order execution is disabled while mode is LIVE_DISABLED")
        risk = self.risk_engine.evaluate(order, self.ledger.snapshot())
        self.storage.insert_risk_decision(order["order_id"], order["run_id"], risk)
        if risk["status"] != "approved":
            order["status"] = "rejected"
            order["reason"] = risk["reason"]
            order["updated_at"] = now_iso()
            self.storage.upsert_order(order)
            return {"order": order, "risk": risk, "portfolio": self.ledger.snapshot()}
        if self.broker:
            return self.submit_broker_order(order, risk=risk)
        return self.execute_order(order, risk=risk)

    def submit_broker_order(
        self, order: Dict[str, Any], risk: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        if not self.broker:
            raise ValueError("No broker is configured")
        order["status"] = "submitted"
        order["updated_at"] = now_iso()
        self.storage.upsert_order(order)
        self.storage.insert_audit_event(
            "broker_order_submitted",
            "order",
            order["order_id"],
            order["run_id"],
            {"broker": self.broker.name, "order": order},
        )

        broker_order = self.broker.submit_order(order)
        order["status"] = _broker_order_status(broker_order)
        order["broker"] = self.broker.name
        order["broker_order_id"] = broker_order.get("id") or broker_order.get("order_id", "")
        order["broker_order"] = broker_order
        order["updated_at"] = now_iso()

        fill = _fill_from_broker_order(order, broker_order)
        if fill:
            self.storage.insert_fill(fill)
            order["quantity"] = fill["quantity"]
            order["estimated_price"] = fill["price"]
            order["estimated_notional"] = fill["notional"]
        self.storage.upsert_order(order)
        self.storage.insert_audit_event(
            "broker_order_updated",
            "order",
            order["order_id"],
            order["run_id"],
            {"broker": self.broker.name, "broker_order": broker_order},
        )
        return {"order": order, "risk": risk, "portfolio": self.ledger.snapshot()}

    def reject_order(self, order_id: str, reason: str = "Rejected manually") -> Dict[str, Any]:
        order = self.storage.order_detail(order_id)
        if not order:
            raise ValueError(f"Order not found: {order_id}")
        if order["status"] not in {"pending_approval", "pending"}:
            raise ValueError(f"Order cannot be rejected from status: {order['status']}")
        order["status"] = "rejected"
        order["reason"] = reason
        order["updated_at"] = now_iso()
        self.storage.upsert_order(order)
        self.storage.insert_audit_event(
            "order_rejected_manually", "order", order_id, order["run_id"], {"reason": reason}
        )
        return {"order": order, "risk": None, "portfolio": self.ledger.snapshot()}

    def execute_order(self, order: Dict[str, Any], risk: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        order["status"] = "submitted"
        order["updated_at"] = now_iso()
        self.storage.upsert_order(order)
        self.storage.insert_audit_event(
            "order_submitted", "order", order["order_id"], order["run_id"], order
        )

        portfolio = self.ledger.apply_decision(
            ticker=order["ticker"],
            decision=order["decision"],
            trade_date=order["trade_date"],
            run_id=order["run_id"],
        )
        trade = _latest_trade_for_order(portfolio.get("trades", []), order)
        if trade:
            fill = {
                "fill_id": str(uuid.uuid4()),
                "order_id": order["order_id"],
                "run_id": order["run_id"],
                "ticker": order["ticker"],
                "action": trade.get("action", order["action"]),
                "quantity": float(trade.get("quantity", 0.0)),
                "price": float(trade.get("price", order.get("estimated_price", 0.0))),
                "notional": float(trade.get("quantity", 0.0))
                * float(trade.get("price", order.get("estimated_price", 0.0))),
                "created_at": trade.get("created_at") or now_iso(),
                "trade": trade,
            }
            self.storage.insert_fill(fill)
            order["quantity"] = fill["quantity"]
            order["estimated_price"] = fill["price"]
            order["estimated_notional"] = fill["notional"]

        order["status"] = "filled"
        order["updated_at"] = now_iso()
        self.storage.upsert_order(order)
        self.storage.insert_audit_event(
            "order_filled", "order", order["order_id"], order["run_id"], order
        )
        return {"order": order, "risk": risk, "portfolio": portfolio}

    def _new_order(self, intent: OrderIntent, preview: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "order_id": str(uuid.uuid4()),
            "run_id": intent.run_id,
            "ticker": intent.ticker.upper(),
            "decision": intent.decision,
            "action": preview["action"],
            "status": "pending",
            "quantity": float(preview["quantity"]),
            "estimated_price": float(preview["price"]),
            "estimated_notional": float(preview["notional"]),
            "projected_position_market_value": float(
                preview["projected_position_market_value"]
            ),
            "mode": self.risk_config.mode,
            "idempotency_key": intent.idempotency_key,
            "reason": "",
            "trade_date": intent.trade_date,
            "source": intent.source,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }


def _latest_trade_for_order(
    trades: List[Dict[str, Any]], order: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    for trade in reversed(trades):
        if (
            trade.get("run_id") == order["run_id"]
            and trade.get("ticker") == order["ticker"]
            and trade.get("decision") == order["decision"]
        ):
            return trade
    return None


def _broker_order_status(broker_order: Dict[str, Any]) -> str:
    status = str(broker_order.get("status", "submitted")).lower()
    if status == "filled":
        return "filled"
    if status in {"canceled", "expired", "rejected"}:
        return status
    return "broker_submitted"


def _fill_from_broker_order(
    order: Dict[str, Any], broker_order: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    if str(broker_order.get("status", "")).lower() != "filled":
        return None
    quantity = float(broker_order.get("filled_qty") or order.get("quantity", 0.0))
    price = float(
        broker_order.get("filled_avg_price")
        or broker_order.get("limit_price")
        or order.get("estimated_price", 0.0)
    )
    return {
        "fill_id": str(uuid.uuid4()),
        "order_id": order["order_id"],
        "run_id": order["run_id"],
        "ticker": order["ticker"],
        "action": order["action"],
        "quantity": quantity,
        "price": price,
        "notional": quantity * price,
        "created_at": broker_order.get("filled_at") or now_iso(),
        "trade": {"broker_order": broker_order},
    }


def _rejection_reason(checks: List[Dict[str, Any]]) -> str:
    failed = [check["name"] for check in checks if not check["passed"]]
    if not failed:
        return ""
    return "Rejected by risk gates: " + ", ".join(failed)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default
