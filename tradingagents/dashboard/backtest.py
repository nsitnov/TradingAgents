from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

from tradingagents.dashboard.performance import portfolio_performance
from tradingagents.dashboard.validation import validate_backtest_result
from tradingagents.default_config import DEFAULT_CONFIG


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_decision(value: str) -> str:
    text = (value or "Hold").strip()
    lower = text.lower()
    exact = {
        "buy": "Buy",
        "sell": "Sell",
        "hold": "Hold",
        "overweight": "Overweight",
        "underweight": "Underweight",
    }
    if lower in exact:
        return exact[lower]
    for keyword, decision in (
        ("overweight", "Overweight"),
        ("underweight", "Underweight"),
        ("buy", "Buy"),
        ("sell", "Sell"),
        ("hold", "Hold"),
    ):
        if keyword in lower:
            return decision
    return "Hold"


@dataclass(frozen=True)
class TransactionCostModel:
    spread_bps: float = 5.0
    slippage_bps: float = 5.0
    fee_bps: float = 0.0
    fixed_fee: float = 0.0

    def validate(self) -> None:
        for name, value in asdict(self).items():
            if float(value) < 0:
                raise ValueError(f"{name} cannot be negative")


@dataclass(frozen=True)
class BacktestConfig:
    tickers: List[str]
    start: date
    end: date
    initial_cash: float = 100_000.0
    benchmarks: List[str] = field(default_factory=lambda: ["SPY", "QQQ"])
    decision_provider: str = "fixed"
    fixed_decision: str = "Hold"
    transaction_costs: TransactionCostModel = field(default_factory=TransactionCostModel)
    analysts: List[str] = field(
        default_factory=lambda: ["market", "social", "news", "fundamentals"]
    )
    research_depth: int = 1
    llm_provider: str = "openai"
    shallow_thinker: str = "gpt-5.4-mini"
    deep_thinker: str = "gpt-5.4"
    backend_url: Optional[str] = None
    output_language: str = "English"
    openai_reasoning_effort: Optional[str] = None

    def validate(self) -> None:
        if not self.tickers:
            raise ValueError("At least one ticker is required")
        if self.end < self.start:
            raise ValueError("Backtest end date must be on or after start date")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.decision_provider not in {"fixed", "tradingagents"}:
            raise ValueError("decision_provider must be fixed or tradingagents")
        if self.research_depth < 1:
            raise ValueError("research_depth must be at least 1")
        self.transaction_costs.validate()

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["start"] = self.start.isoformat()
        data["end"] = self.end.isoformat()
        return data


class BacktestRequest(BaseModel):
    tickers: List[str] = Field(default_factory=lambda: ["SPY"], min_length=1)
    start: date
    end: date
    initial_cash: float = Field(default=100_000.0, gt=0)
    benchmarks: List[str] = Field(default_factory=lambda: ["SPY", "QQQ"])
    decision_provider: str = "fixed"
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

    @field_validator("tickers", "benchmarks")
    @classmethod
    def normalize_symbols(cls, value: List[str]) -> List[str]:
        symbols = [item.strip().upper() for item in value if item.strip()]
        if not symbols:
            raise ValueError("At least one symbol is required")
        return list(dict.fromkeys(symbols))

    @field_validator("fixed_decision")
    @classmethod
    def normalize_fixed_decision(cls, value: str) -> str:
        return normalize_decision(value)

    @field_validator("decision_provider")
    @classmethod
    def normalize_provider(cls, value: str) -> str:
        provider = value.strip().lower()
        if provider not in {"fixed", "tradingagents"}:
            raise ValueError("decision_provider must be fixed or tradingagents")
        return provider

    @model_validator(mode="after")
    def validate_dates(self) -> "BacktestRequest":
        if self.end < self.start:
            raise ValueError("end must be on or after start")
        return self

    def to_config(self) -> BacktestConfig:
        cost_model = TransactionCostModel(**self.transaction_costs)
        config = BacktestConfig(
            tickers=self.tickers,
            start=self.start,
            end=self.end,
            initial_cash=self.initial_cash,
            benchmarks=self.benchmarks,
            decision_provider=self.decision_provider,
            fixed_decision=self.fixed_decision,
            transaction_costs=cost_model,
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


class PriceProvider(Protocol):
    def prepare(self, tickers: Iterable[str], start: date, end: date) -> None:
        ...

    def close(self, ticker: str, day: date) -> float:
        ...

    def price_range(self, ticker: str, start: date, end: date) -> Tuple[float, float]:
        ...


class HistoricalPriceProvider:
    """Historical EOD close provider backed by yfinance.

    The provider exposes "close on or before date" semantics to avoid breaking
    on weekends and market holidays. That keeps replay deterministic while still
    making missing-data problems explicit.
    """

    def __init__(self) -> None:
        self._prices: Dict[str, Dict[date, float]] = {}

    def prepare(self, tickers: Iterable[str], start: date, end: date) -> None:
        for ticker in tickers:
            symbol = ticker.upper().strip()
            if symbol and symbol not in self._prices:
                self._prices[symbol] = self._load(symbol, start, end)

    def close(self, ticker: str, day: date) -> float:
        symbol = ticker.upper().strip()
        if symbol not in self._prices:
            self.prepare([symbol], day, day)
        prices = self._prices.get(symbol, {})
        candidates = [price_date for price_date in prices if price_date <= day]
        if not candidates:
            raise ValueError(f"No historical close for {symbol} on or before {day}")
        return float(prices[max(candidates)])

    def price_range(self, ticker: str, start: date, end: date) -> Tuple[float, float]:
        self.prepare([ticker], start, end)
        return self.close(ticker, start), self.close(ticker, end)

    def _load(self, ticker: str, start: date, end: date) -> Dict[date, float]:
        import yfinance as yf

        query_start = start - timedelta(days=10)
        query_end = end + timedelta(days=1)
        history = yf.Ticker(ticker).history(
            start=query_start.isoformat(),
            end=query_end.isoformat(),
        )
        if history.empty or "Close" not in history:
            raise ValueError(f"No historical price data for {ticker}")
        closes = history["Close"].dropna()
        if closes.empty:
            raise ValueError(f"No historical close prices for {ticker}")
        return {
            index.date(): float(value)
            for index, value in closes.items()
            if float(value) > 0
        }


class DecisionProvider(Protocol):
    def decide(self, ticker: str, day: date, portfolio: Dict[str, Any]) -> str:
        ...


class FixedDecisionProvider:
    def __init__(self, decision: str) -> None:
        self.decision = normalize_decision(decision)

    def decide(self, ticker: str, day: date, portfolio: Dict[str, Any]) -> str:
        return self.decision


class TradingAgentsDecisionProvider:
    """Decision provider that reuses the original multi-agent graph."""

    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self._graph = None

    def decide(self, ticker: str, day: date, portfolio: Dict[str, Any]) -> str:
        if self._graph is None:
            from tradingagents.graph.trading_graph import TradingAgentsGraph

            graph_config = DEFAULT_CONFIG.copy()
            graph_config["max_debate_rounds"] = self.config.research_depth
            graph_config["max_risk_discuss_rounds"] = self.config.research_depth
            graph_config["quick_think_llm"] = self.config.shallow_thinker
            graph_config["deep_think_llm"] = self.config.deep_thinker
            graph_config["backend_url"] = self.config.backend_url
            graph_config["llm_provider"] = self.config.llm_provider.lower()
            graph_config["openai_reasoning_effort"] = self.config.openai_reasoning_effort
            graph_config["output_language"] = self.config.output_language
            graph_config["checkpoint_enabled"] = False
            self._graph = TradingAgentsGraph(
                self.config.analysts,
                config=graph_config,
                debug=False,
            )
        _, processed_decision = self._graph.propagate(ticker, day.isoformat())
        return normalize_decision(str(processed_decision))


class BacktestEngine:
    def __init__(
        self,
        config: BacktestConfig,
        *,
        price_provider: Optional[PriceProvider] = None,
        decision_provider: Optional[DecisionProvider] = None,
    ) -> None:
        config.validate()
        self.config = config
        self.price_provider = price_provider or HistoricalPriceProvider()
        self.decision_provider = decision_provider or self._decision_provider(config)

    def run(self) -> Dict[str, Any]:
        backtest_id = str(uuid.uuid4())
        started_at = now_iso()
        ledger = self._empty_ledger()
        history: List[Dict[str, Any]] = []
        trades: List[Dict[str, Any]] = []
        decisions: List[Dict[str, Any]] = []
        skipped: List[Dict[str, Any]] = []

        try:
            symbols = list(dict.fromkeys([*self.config.tickers, *self.config.benchmarks]))
            self.price_provider.prepare(symbols, self.config.start, self.config.end)
            history.append(self._snapshot(ledger, self.config.start, phase="initial"))

            for day in _business_days(self.config.start, self.config.end):
                self._mark_positions(ledger, day, skipped)
                for ticker in self.config.tickers:
                    try:
                        price = self.price_provider.close(ticker, day)
                        decision = normalize_decision(
                            self.decision_provider.decide(
                                ticker,
                                day,
                                self._snapshot(ledger, day, phase="pre_trade"),
                            )
                        )
                        decisions.append(
                            {
                                "ticker": ticker,
                                "trade_date": day.isoformat(),
                                "decision": decision,
                                "price": price,
                            }
                        )
                        trade = self._apply_decision(ledger, ticker, decision, price, day)
                        if trade:
                            trades.append(trade)
                    except Exception as exc:
                        skipped.append(
                            {
                                "ticker": ticker,
                                "trade_date": day.isoformat(),
                                "error": str(exc),
                            }
                        )
                self._mark_positions(ledger, day, skipped)
                history.append(self._snapshot(ledger, day, phase="close"))

            performance = portfolio_performance(
                history,
                trades,
                self.config.initial_cash,
                benchmarks=self.config.benchmarks,
                price_provider=self.price_provider.price_range,
            )
            result = {
                "backtest_id": backtest_id,
                "status": "completed",
                "started_at": started_at,
                "ended_at": now_iso(),
                "config": self.config.as_dict(),
                "summary": self._summary(performance, history, trades, skipped),
                "performance": performance,
                "history": history,
                "trades": trades,
                "decisions": decisions,
                "skipped": skipped,
                "portfolio": ledger,
            }
            result["validation"] = validate_backtest_result(result)
            return result
        except Exception as exc:
            return {
                "backtest_id": backtest_id,
                "status": "error",
                "started_at": started_at,
                "ended_at": now_iso(),
                "config": self.config.as_dict(),
                "summary": {},
                "performance": {},
                "history": history,
                "trades": trades,
                "decisions": decisions,
                "skipped": skipped,
                "portfolio": ledger,
                "error": str(exc),
            }

    def _decision_provider(self, config: BacktestConfig) -> DecisionProvider:
        if config.decision_provider == "tradingagents":
            return TradingAgentsDecisionProvider(config)
        return FixedDecisionProvider(config.fixed_decision)

    def _empty_ledger(self) -> Dict[str, Any]:
        return {
            "cash": float(self.config.initial_cash),
            "initial_cash": float(self.config.initial_cash),
            "positions": {},
            "market_value": 0.0,
            "equity": float(self.config.initial_cash),
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
        }

    def _apply_decision(
        self,
        ledger: Dict[str, Any],
        ticker: str,
        decision: str,
        close_price: float,
        day: date,
    ) -> Optional[Dict[str, Any]]:
        ticker = ticker.upper().strip()
        position = ledger["positions"].setdefault(
            ticker,
            {
                "quantity": 0.0,
                "avg_cost": 0.0,
                "realized_pnl": 0.0,
                "last_price": close_price,
                "market_value": 0.0,
                "unrealized_pnl": 0.0,
            },
        )
        current_qty = float(position.get("quantity", 0.0))
        action = "hold"
        quantity = 0.0

        if decision == "Buy":
            action = "buy"
            quantity = self._affordable_quantity(ledger, close_price, 0.20)
        elif decision == "Overweight":
            action = "buy"
            quantity = self._affordable_quantity(ledger, close_price, 0.10)
        elif decision == "Underweight" and current_qty > 0:
            action = "sell"
            quantity = current_qty * 0.50
        elif decision == "Sell" and current_qty > 0:
            action = "sell"
            quantity = current_qty

        if quantity <= 1e-9 or action == "hold":
            return None

        execution_price = self._execution_price(close_price, action)
        gross_notional = quantity * execution_price
        fee = self._fee(gross_notional)

        if action == "buy":
            total_cost = gross_notional + fee
            if total_cost > float(ledger["cash"]) + 1e-6:
                quantity = self._affordable_quantity(ledger, close_price, 1.0)
                gross_notional = quantity * execution_price
                fee = self._fee(gross_notional)
                total_cost = gross_notional + fee
            if quantity <= 1e-9 or total_cost <= 0:
                return None
            previous_cost = current_qty * float(position.get("avg_cost", 0.0))
            new_qty = current_qty + quantity
            position["quantity"] = new_qty
            position["avg_cost"] = (previous_cost + total_cost) / new_qty
            ledger["cash"] = float(ledger["cash"]) - total_cost
            realized = 0.0
        else:
            quantity = min(quantity, current_qty)
            gross_notional = quantity * execution_price
            fee = self._fee(gross_notional)
            proceeds = gross_notional - fee
            if quantity <= 1e-9 or proceeds <= 0:
                return None
            cost_basis = quantity * float(position.get("avg_cost", 0.0))
            realized = proceeds - cost_basis
            position["quantity"] = max(0.0, current_qty - quantity)
            position["realized_pnl"] = float(position.get("realized_pnl", 0.0)) + realized
            ledger["cash"] = float(ledger["cash"]) + proceeds

        position["last_price"] = close_price
        self._recalculate(ledger)
        return {
            "ticker": ticker,
            "decision": decision,
            "action": action,
            "quantity": quantity,
            "price": execution_price,
            "reference_price": close_price,
            "notional": gross_notional,
            "fees": fee,
            "realized_pnl": realized,
            "trade_date": day.isoformat(),
            "created_at": f"{day.isoformat()}T16:00:00+00:00",
            "source": "backtest",
        }

    def _affordable_quantity(
        self, ledger: Dict[str, Any], close_price: float, cash_fraction: float
    ) -> float:
        costs = self.config.transaction_costs
        execution_price = self._execution_price(close_price, "buy")
        budget = float(ledger["cash"]) * cash_fraction
        if budget <= costs.fixed_fee:
            return 0.0
        fee_multiplier = 1.0 + costs.fee_bps / 10_000.0
        return (budget - costs.fixed_fee) / (execution_price * fee_multiplier)

    def _execution_price(self, close_price: float, action: str) -> float:
        costs = self.config.transaction_costs
        adjustment = (costs.spread_bps + costs.slippage_bps) / 10_000.0
        if action == "buy":
            return close_price * (1.0 + adjustment)
        if action == "sell":
            return close_price * (1.0 - adjustment)
        return close_price

    def _fee(self, notional: float) -> float:
        costs = self.config.transaction_costs
        return notional * (costs.fee_bps / 10_000.0) + costs.fixed_fee

    def _mark_positions(
        self,
        ledger: Dict[str, Any],
        day: date,
        skipped: List[Dict[str, Any]],
    ) -> None:
        for ticker, position in list(ledger["positions"].items()):
            try:
                position["last_price"] = self.price_provider.close(ticker, day)
            except Exception as exc:
                skipped.append(
                    {
                        "ticker": ticker,
                        "trade_date": day.isoformat(),
                        "error": str(exc),
                    }
                )
        self._recalculate(ledger)

    def _recalculate(self, ledger: Dict[str, Any]) -> None:
        market_value = 0.0
        realized_pnl = 0.0
        unrealized_pnl = 0.0
        for position in ledger["positions"].values():
            quantity = float(position.get("quantity", 0.0))
            last_price = float(position.get("last_price", 0.0))
            avg_cost = float(position.get("avg_cost", 0.0))
            position["market_value"] = quantity * last_price
            position["unrealized_pnl"] = (last_price - avg_cost) * quantity
            market_value += float(position["market_value"])
            realized_pnl += float(position.get("realized_pnl", 0.0))
            unrealized_pnl += float(position["unrealized_pnl"])
        ledger["market_value"] = market_value
        ledger["realized_pnl"] = realized_pnl
        ledger["unrealized_pnl"] = unrealized_pnl
        ledger["equity"] = float(ledger["cash"]) + market_value
        ledger["total_pnl"] = ledger["equity"] - float(ledger["initial_cash"])

    def _snapshot(self, ledger: Dict[str, Any], day: date, *, phase: str) -> Dict[str, Any]:
        created_at = (
            f"{day.isoformat()}T00:00:00+00:00"
            if phase == "initial"
            else f"{day.isoformat()}T23:59:59+00:00"
        )
        return {
            "created_at": created_at,
            "phase": phase,
            "cash": float(ledger["cash"]),
            "market_value": float(ledger["market_value"]),
            "equity": float(ledger["equity"]),
            "realized_pnl": float(ledger["realized_pnl"]),
            "unrealized_pnl": float(ledger["unrealized_pnl"]),
            "total_pnl": float(ledger["total_pnl"]),
            "positions": {
                ticker: dict(position)
                for ticker, position in ledger.get("positions", {}).items()
                if float(position.get("quantity", 0.0)) > 1e-9
            },
        }

    def _summary(
        self,
        performance: Dict[str, Any],
        history: List[Dict[str, Any]],
        trades: List[Dict[str, Any]],
        skipped: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        return {
            "tickers": self.config.tickers,
            "start": self.config.start.isoformat(),
            "end": self.config.end.isoformat(),
            "trading_days": max(0, len(history) - 1),
            "trade_count": len(trades),
            "skipped_count": len(skipped),
            "start_equity": performance.get("start_equity", self.config.initial_cash),
            "end_equity": performance.get("end_equity", self.config.initial_cash),
            "total_pnl": performance.get("total_pnl", 0.0),
            "total_return_pct": performance.get("total_return_pct", 0.0),
            "max_drawdown": performance.get("max_drawdown", 0.0),
            "benchmark_count": len(performance.get("benchmarks", [])),
        }


def _business_days(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def _parse_symbols(raw: str) -> List[str]:
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a TradingAgents paper backtest.")
    parser.add_argument("--tickers", required=True, help="Comma-separated ticker list")
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--benchmarks", default="SPY,QQQ")
    parser.add_argument(
        "--decision-provider", choices=["fixed", "tradingagents"], default="fixed"
    )
    parser.add_argument("--fixed-decision", default="Hold")
    parser.add_argument("--spread-bps", type=float, default=5.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--fee-bps", type=float, default=0.0)
    parser.add_argument("--fixed-fee", type=float, default=0.0)
    args = parser.parse_args(argv)

    request = BacktestRequest(
        tickers=_parse_symbols(args.tickers),
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
        initial_cash=args.initial_cash,
        benchmarks=_parse_symbols(args.benchmarks),
        decision_provider=args.decision_provider,
        fixed_decision=args.fixed_decision,
        transaction_costs={
            "spread_bps": args.spread_bps,
            "slippage_bps": args.slippage_bps,
            "fee_bps": args.fee_bps,
            "fixed_fee": args.fixed_fee,
        },
    )
    result = BacktestEngine(request.to_config()).run()
    print(
        json.dumps(
            {"summary": result.get("summary", {}), "status": result["status"]},
            indent=2,
        )
    )
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
