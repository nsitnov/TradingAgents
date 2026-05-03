from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

from langchain_core.runnables import Runnable

logger = logging.getLogger(__name__)


DEFAULT_LLM_ROUTING: Dict[str, Any] = {
    "quick_llm_provider": "ollama",
    "quick_think_llm": "gpt-oss:20b",
    "quick_backend_url": "http://localhost:11434/v1",
    "quick_fallback_llm_provider": "openai",
    "quick_fallback_think_llm": "gpt-5.4-mini",
    "quick_fallback_backend_url": None,
    "deep_llm_provider": "openai",
    "deep_think_llm": "gpt-5.4",
    "deep_backend_url": None,
    "critical_llm_provider": "openai",
    "critical_think_llm": "gpt-5.4",
    "critical_backend_url": None,
}

ROUTING_SELECTOR_KEYS = {
    "quick_llm_provider",
    "quick_backend_url",
    "quick_fallback_llm_provider",
    "quick_fallback_think_llm",
    "quick_fallback_backend_url",
    "deep_llm_provider",
    "deep_backend_url",
    "critical_llm_provider",
    "critical_think_llm",
    "critical_backend_url",
}


class FallbackLLM(Runnable[Any, Any]):
    """Small proxy that retries a failed primary LLM call on a fallback LLM."""

    def __init__(
        self,
        primary: Any,
        fallback: Optional[Any],
        *,
        role: str,
        on_fallback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.role = role
        self.on_fallback = on_fallback

    def invoke(self, input: Any, config: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Any:
        try:
            return self.primary.invoke(input, config=config, **kwargs)
        except Exception as exc:
            if self.fallback is None:
                raise
            self._record("invoke", exc)
            return self.fallback.invoke(input, config=config, **kwargs)

    def with_structured_output(self, *args: Any, **kwargs: Any) -> "FallbackLLM":
        primary_structured = None
        fallback_structured = None
        primary_error: Optional[Exception] = None
        fallback_error: Optional[Exception] = None

        try:
            primary_structured = self.primary.with_structured_output(*args, **kwargs)
        except Exception as exc:
            primary_error = exc

        if self.fallback is not None:
            try:
                fallback_structured = self.fallback.with_structured_output(*args, **kwargs)
            except Exception as exc:
                fallback_error = exc

        if primary_structured is None and fallback_structured is None:
            raise primary_error or fallback_error or NotImplementedError(
                "No structured-output LLM is available"
            )
        if primary_structured is None:
            self._record("with_structured_output", primary_error)
            primary_structured = fallback_structured
            fallback_structured = None

        return FallbackLLM(
            primary_structured,
            fallback_structured,
            role=self.role,
            on_fallback=self.on_fallback,
        )

    def bind_tools(self, *args: Any, **kwargs: Any) -> "FallbackLLM":
        primary_bound = None
        fallback_bound = None
        primary_error: Optional[Exception] = None
        fallback_error: Optional[Exception] = None

        try:
            primary_bound = self.primary.bind_tools(*args, **kwargs)
        except Exception as exc:
            primary_error = exc

        if self.fallback is not None:
            try:
                fallback_bound = self.fallback.bind_tools(*args, **kwargs)
            except Exception as exc:
                fallback_error = exc

        if primary_bound is None and fallback_bound is None:
            raise primary_error or fallback_error or NotImplementedError(
                "No tool-calling LLM is available"
            )
        if primary_bound is None:
            self._record("bind_tools", primary_error)
            primary_bound = fallback_bound
            fallback_bound = None

        return FallbackLLM(
            primary_bound,
            fallback_bound,
            role=self.role,
            on_fallback=self.on_fallback,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self.primary, name)

    def _record(self, phase: str, exc: Optional[Exception]) -> None:
        event = {
            "role": self.role,
            "phase": phase,
            "error": str(exc) if exc else "",
        }
        logger.warning(
            "%s LLM fallback used during %s: %s",
            self.role,
            phase,
            event["error"],
        )
        if self.on_fallback:
            self.on_fallback(event)


def routing_config(config: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(DEFAULT_LLM_ROUTING)
    legacy_provider = config.get("llm_provider")
    legacy_backend = config.get("backend_url")

    if not any(key in config for key in ROUTING_SELECTOR_KEYS):
        merged.update(
            {
                "quick_llm_provider": legacy_provider or "openai",
                "quick_think_llm": config.get("quick_think_llm", "gpt-5.4-mini"),
                "quick_backend_url": legacy_backend,
                "quick_fallback_llm_provider": None,
                "quick_fallback_think_llm": None,
                "quick_fallback_backend_url": None,
                "deep_llm_provider": legacy_provider or "openai",
                "deep_think_llm": config.get("deep_think_llm", "gpt-5.4"),
                "deep_backend_url": legacy_backend,
                "critical_llm_provider": legacy_provider or "openai",
                "critical_think_llm": config.get(
                    "critical_think_llm", config.get("deep_think_llm", "gpt-5.4")
                ),
                "critical_backend_url": legacy_backend,
            }
        )

    for key in DEFAULT_LLM_ROUTING:
        if key in config:
            merged[key] = config[key]
    return merged
