from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Protocol

import requests


ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


class BrokerError(RuntimeError):
    pass


class BrokerAdapter(Protocol):
    name: str

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        ...

    def get_positions(self) -> List[Dict[str, Any]]:
        ...

    def get_orders(self, status: str = "all", limit: int = 100) -> List[Dict[str, Any]]:
        ...


class AlpacaPaperBroker:
    name = "alpaca_paper"

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        base_url: str = ALPACA_PAPER_BASE_URL,
        timeout: float = 20.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        if not api_key or not secret_key:
            raise BrokerError("Alpaca paper broker requires API key and secret")
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls) -> "AlpacaPaperBroker":
        return cls(
            api_key=os.getenv("ALPACA_API_KEY_ID", "").strip(),
            secret_key=os.getenv("ALPACA_API_SECRET_KEY", "").strip(),
            base_url=os.getenv("ALPACA_BASE_URL", ALPACA_PAPER_BASE_URL).strip()
            or ALPACA_PAPER_BASE_URL,
        )

    def submit_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        action = order.get("action")
        if action not in {"buy", "sell"}:
            return {"status": "skipped", "reason": f"No broker order for action {action}"}

        payload = {
            "symbol": order["ticker"],
            "qty": _qty_string(float(order.get("quantity", 0.0))),
            "side": action,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": order["order_id"],
        }
        response = self.session.post(
            f"{self.base_url}/v2/orders",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        return self._decode(response)

    def get_positions(self) -> List[Dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/v2/positions",
            headers=self._headers(),
            timeout=self.timeout,
        )
        data = self._decode(response)
        if not isinstance(data, list):
            raise BrokerError("Alpaca positions response was not a list")
        return data

    def get_orders(self, status: str = "all", limit: int = 100) -> List[Dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/v2/orders",
            headers=self._headers(),
            params={"status": status, "limit": limit},
            timeout=self.timeout,
        )
        data = self._decode(response)
        if not isinstance(data, list):
            raise BrokerError("Alpaca orders response was not a list")
        return data

    def _headers(self) -> Dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

    def _decode(self, response: requests.Response) -> Any:
        if response.status_code >= 400:
            raise BrokerError(
                f"Alpaca paper broker request failed with HTTP {response.status_code}: "
                f"{response.text}"
            )
        if not response.text:
            return {}
        return response.json()


def broker_from_env() -> Optional[BrokerAdapter]:
    broker = os.getenv("TRADINGAGENTS_BROKER", "paper_ledger").strip().lower()
    if broker in {"", "paper", "paper_ledger", "local"}:
        return None
    if broker == "alpaca_paper":
        return AlpacaPaperBroker.from_env()
    raise BrokerError(f"Unsupported broker: {broker}")


def broker_config_from_env() -> Dict[str, Any]:
    broker = os.getenv("TRADINGAGENTS_BROKER", "paper_ledger").strip().lower()
    return {
        "broker": broker or "paper_ledger",
        "alpaca_configured": bool(
            os.getenv("ALPACA_API_KEY_ID") and os.getenv("ALPACA_API_SECRET_KEY")
        ),
        "alpaca_base_url": os.getenv("ALPACA_BASE_URL", ALPACA_PAPER_BASE_URL),
    }


def _qty_string(quantity: float) -> str:
    if quantity <= 0:
        raise BrokerError("Broker order quantity must be positive")
    return f"{quantity:.8f}".rstrip("0").rstrip(".")
