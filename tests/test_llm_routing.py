from tradingagents.graph.setup import GraphSetup
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.routing import FallbackLLM, routing_config


class FakeLLM:
    def __init__(self, name, *, fail=False, fail_bind_tools=False):
        self.name = name
        self.fail = fail
        self.fail_bind_tools = fail_bind_tools

    def invoke(self, prompt, config=None, **kwargs):
        if self.fail:
            raise RuntimeError(f"{self.name} failed")
        return type("Response", (), {"content": self.name})()

    def with_structured_output(self, *args, **kwargs):
        return self

    def bind_tools(self, *args, **kwargs):
        if self.fail_bind_tools:
            raise NotImplementedError("tool calling unavailable")
        return self


class FakeClient:
    def __init__(self, provider, model):
        self.provider = provider
        self.model = model

    def get_llm(self):
        return FakeLLM(f"{self.provider}:{self.model}")


class FakeConditionalLogic:
    def should_continue_market(self, state):
        return "Msg Clear Market"

    def should_continue_debate(self, state):
        return "Research Manager"

    def should_continue_risk_analysis(self, state):
        return "Portfolio Manager"


def test_routing_config_keeps_legacy_single_provider_when_no_routing_keys():
    config = {
        "llm_provider": "openai",
        "quick_think_llm": "gpt-5.4-mini",
        "deep_think_llm": "gpt-5.4",
        "backend_url": None,
    }

    routed = routing_config(config)

    assert routed["quick_llm_provider"] == "openai"
    assert routed["deep_llm_provider"] == "openai"
    assert routed["critical_llm_provider"] == "openai"
    assert routed["quick_fallback_llm_provider"] is None


def test_fallback_llm_retries_on_fallback_model():
    events = []
    llm = FallbackLLM(
        FakeLLM("local", fail=True),
        FakeLLM("openai"),
        role="quick",
        on_fallback=events.append,
    )

    result = llm.invoke("prompt")

    assert result.content == "openai"
    assert events[0]["role"] == "quick"


def test_fallback_llm_retries_tool_binding_on_fallback_model():
    events = []
    llm = FallbackLLM(
        FakeLLM("local", fail_bind_tools=True),
        FakeLLM("openai"),
        role="quick",
        on_fallback=events.append,
    )

    bound = llm.bind_tools([])

    assert bound.invoke("prompt").content == "openai"
    assert events[0]["phase"] == "bind_tools"


def test_graph_creates_mixed_provider_clients(monkeypatch):
    created = []

    def fake_create_llm_client(provider, model, base_url=None, **kwargs):
        created.append((provider, model, base_url))
        return FakeClient(provider, model)

    monkeypatch.setattr(
        "tradingagents.graph.trading_graph.create_llm_client",
        fake_create_llm_client,
    )

    config = {
        "project_dir": ".",
        "results_dir": "/tmp",
        "data_cache_dir": "/tmp",
        "memory_log_path": "/tmp/memory.md",
        "memory_log_max_entries": None,
        "llm_provider": "openai",
        "quick_think_llm": "gpt-oss:20b",
        "deep_think_llm": "gpt-5.4",
        "quick_llm_provider": "ollama",
        "quick_backend_url": "http://localhost:11434/v1",
        "quick_fallback_llm_provider": "openai",
        "quick_fallback_think_llm": "gpt-5.4-mini",
        "quick_fallback_backend_url": None,
        "deep_llm_provider": "openai",
        "deep_backend_url": None,
        "critical_llm_provider": "openai",
        "critical_think_llm": "gpt-5.4",
        "critical_backend_url": None,
        "backend_url": None,
        "google_thinking_level": None,
        "openai_reasoning_effort": None,
        "anthropic_effort": None,
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        "checkpoint_enabled": False,
        "data_vendors": {},
        "tool_vendors": {},
    }

    TradingAgentsGraph(selected_analysts=["market"], config=config)

    assert ("ollama", "gpt-oss:20b", "http://localhost:11434/v1") in created
    assert ("openai", "gpt-5.4-mini", None) in created
    assert ("openai", "gpt-5.4", None) in created


def test_graph_setup_routes_trader_and_portfolio_to_critical_llm(monkeypatch):
    calls = {}

    def node_factory(name):
        def node(state=None):
            return {}

        node.__name__ = name
        return node

    def record(name):
        def factory(llm):
            calls[name] = llm
            return node_factory(name)

        return factory

    monkeypatch.setattr(
        "tradingagents.graph.setup.create_market_analyst",
        record("market"),
    )
    monkeypatch.setattr(
        "tradingagents.graph.setup.create_bull_researcher",
        record("bull"),
    )
    monkeypatch.setattr(
        "tradingagents.graph.setup.create_bear_researcher",
        record("bear"),
    )
    monkeypatch.setattr(
        "tradingagents.graph.setup.create_research_manager",
        record("research_manager"),
    )
    monkeypatch.setattr(
        "tradingagents.graph.setup.create_trader",
        record("trader"),
    )
    monkeypatch.setattr(
        "tradingagents.graph.setup.create_aggressive_debator",
        record("aggressive"),
    )
    monkeypatch.setattr(
        "tradingagents.graph.setup.create_neutral_debator",
        record("neutral"),
    )
    monkeypatch.setattr(
        "tradingagents.graph.setup.create_conservative_debator",
        record("conservative"),
    )
    monkeypatch.setattr(
        "tradingagents.graph.setup.create_portfolio_manager",
        record("portfolio_manager"),
    )
    monkeypatch.setattr(
        "tradingagents.graph.setup.create_msg_delete",
        lambda: node_factory("delete"),
    )

    quick = object()
    deep = object()
    critical = object()
    setup = GraphSetup(
        quick,
        deep,
        critical,
        {"market": node_factory("tools_market")},
        FakeConditionalLogic(),
    )

    setup.setup_graph(["market"])

    assert calls["market"] is quick
    assert calls["bull"] is quick
    assert calls["research_manager"] is deep
    assert calls["trader"] is critical
    assert calls["portfolio_manager"] is critical
