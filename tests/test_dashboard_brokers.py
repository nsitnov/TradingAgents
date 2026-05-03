import pytest

from tradingagents.dashboard.brokers import AlpacaPaperBroker, BrokerError


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="{}"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, *, headers, json, timeout):
        self.posts.append(
            {"url": url, "headers": headers, "json": json, "timeout": timeout}
        )
        return FakeResponse(
            payload={
                "id": "alpaca-order-1",
                "status": "accepted",
                "client_order_id": json["client_order_id"],
            }
        )

    def get(self, url, *, headers, timeout, params=None):
        self.gets.append(
            {"url": url, "headers": headers, "timeout": timeout, "params": params}
        )
        return FakeResponse(payload=[{"symbol": "SPY", "qty": "1"}], text="[]")


def test_alpaca_paper_broker_submits_market_order_payload():
    session = FakeSession()
    broker = AlpacaPaperBroker(
        api_key="key",
        secret_key="secret",
        session=session,
        base_url="https://paper-api.alpaca.markets",
    )

    result = broker.submit_order(
        {
            "order_id": "order-1",
            "ticker": "SPY",
            "action": "buy",
            "quantity": 1.25,
        }
    )

    assert result["id"] == "alpaca-order-1"
    posted = session.posts[0]
    assert posted["url"] == "https://paper-api.alpaca.markets/v2/orders"
    assert posted["headers"]["APCA-API-KEY-ID"] == "key"
    assert posted["headers"]["APCA-API-SECRET-KEY"] == "secret"
    assert posted["json"] == {
        "symbol": "SPY",
        "qty": "1.25",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "client_order_id": "order-1",
    }


def test_alpaca_paper_broker_gets_positions_and_orders():
    session = FakeSession()
    broker = AlpacaPaperBroker(api_key="key", secret_key="secret", session=session)

    assert broker.get_positions() == [{"symbol": "SPY", "qty": "1"}]
    assert broker.get_orders(status="open", limit=10) == [{"symbol": "SPY", "qty": "1"}]

    assert session.gets[0]["url"].endswith("/v2/positions")
    assert session.gets[1]["url"].endswith("/v2/orders")
    assert session.gets[1]["params"] == {"status": "open", "limit": 10}


def test_alpaca_paper_broker_raises_on_error_response():
    class ErrorSession(FakeSession):
        def post(self, url, *, headers, json, timeout):
            return FakeResponse(status_code=403, text="forbidden")

    broker = AlpacaPaperBroker(api_key="key", secret_key="secret", session=ErrorSession())

    with pytest.raises(BrokerError, match="HTTP 403"):
        broker.submit_order(
            {
                "order_id": "order-1",
                "ticker": "SPY",
                "action": "buy",
                "quantity": 1,
            }
        )
