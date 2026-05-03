from pathlib import Path

import pytest

from tradingagents.dashboard.llm_eval import (
    LLMEvalRequest,
    LLMEvalService,
    OllamaModel,
    _parse_ollama_ps,
)
from tradingagents.dashboard.storage import DashboardStorage


class FakeRuntime:
    def __init__(self, *, installed=None, running=None, sizes=None, responses=None):
        self.installed = set(installed or [])
        self.running = dict(running or {})
        self.sizes = dict(sizes or {})
        self.responses = dict(responses or {})
        self.stopped = []
        self.generated = []
        self.pulled = []

    def installed_models(self):
        return sorted(self.installed)

    def running_models(self):
        return [
            OllamaModel(name=name, size_gib=size, processor="GPU")
            for name, size in self.running.items()
        ]

    def pull(self, model):
        self.pulled.append(model)
        self.installed.add(model)

    def stop(self, model):
        self.stopped.append(model)
        self.running.pop(model, None)

    def available_memory_gib(self):
        return 80.0

    def generate(self, model, prompt, *, timeout=120.0):
        self.generated.append(model)
        self.running[model] = self.sizes.get(model, 17.0)
        if "ready" in prompt.lower():
            return "ready"
        response = self.responses.get(model)
        if isinstance(response, dict):
            if "TSMC" in prompt:
                return response.get("schema_scanner_mapping")
            if "kill switch" in prompt:
                return response.get("risk_kill_switch")
            return response.get("contradiction_check")
        return response or '{"action":"hold","tickers":["SPY"],"confidence":0.8,"reason":"ok"}'


def _storage(tmp_path):
    return DashboardStorage(
        db_path=tmp_path / "dashboard.sqlite3",
        analyses_dir=tmp_path / "analyses",
    )


def test_parse_ollama_ps_extracts_runtime_size():
    rows = _parse_ollama_ps(
        "NAME           ID              SIZE     PROCESSOR    CONTEXT    UNTIL\n"
        "gpt-oss:20b    17052f91a42e    17 GB    100% GPU     131072     4 minutes\n"
    )

    assert rows[0].name == "gpt-oss:20b"
    assert rows[0].size_gib == 17.0


def test_llm_eval_never_stops_foreign_models(tmp_path):
    runtime = FakeRuntime(
        installed={"gpt-oss:20b", "qwen3.6:27b"},
        running={"gpt-oss:20b": 17.0, "foreign-model:latest": 9.0},
        sizes={"gpt-oss:20b": 17.0, "qwen3.6:27b": 24.0},
    )
    service = LLMEvalService(storage=_storage(tmp_path), runtime=runtime)

    result = service.run(
        LLMEvalRequest(
            models=["qwen3.6:27b"],
            baseline_model="gpt-oss:20b",
            auto_promote=False,
        )
    )

    assert result["status"] == "completed"
    assert "foreign-model:latest" not in runtime.stopped
    assert "foreign-model:latest" in result["foreign_running_models"]
    assert result["baseline_restored"] is True
    assert result["restored_model"] == "gpt-oss:20b"


def test_llm_eval_skips_candidate_already_running_as_foreign(tmp_path):
    runtime = FakeRuntime(
        installed={"gpt-oss:20b", "qwen3.6:27b"},
        running={"gpt-oss:20b": 17.0, "qwen3.6:27b": 24.0},
        sizes={"gpt-oss:20b": 17.0, "qwen3.6:27b": 24.0},
    )
    service = LLMEvalService(storage=_storage(tmp_path), runtime=runtime)

    result = service.run(
        LLMEvalRequest(
            models=["qwen3.6:27b"],
            baseline_model="gpt-oss:20b",
            auto_promote=False,
        )
    )

    candidate = next(item for item in result["model_results"] if item["model"] == "qwen3.6:27b")
    assert candidate["status"] == "protected_foreign_running"
    assert "qwen3.6:27b" not in runtime.stopped
    assert "qwen3.6:27b" in result["foreign_running_models"]


def test_llm_eval_rejects_oversized_known_model_without_pull(tmp_path):
    runtime = FakeRuntime(installed={"gpt-oss:20b"}, running={}, sizes={"gpt-oss:20b": 17.0})
    service = LLMEvalService(storage=_storage(tmp_path), runtime=runtime)

    result = service.run(
        LLMEvalRequest(
            models=["nemotron-3-super"],
            baseline_model="gpt-oss:20b",
            allow_pull=True,
            auto_promote=False,
        )
    )

    nemotron = next(item for item in result["model_results"] if item["model"] == "nemotron-3-super")
    assert nemotron["status"] == "too_large"
    assert "nemotron-3-super" not in runtime.pulled


def test_llm_eval_auto_promotes_only_after_gates_pass(tmp_path):
    automation_path = tmp_path / "automation.json"
    autopilot_path = tmp_path / "autopilot.json"
    baseline_response = '{"action":"hold","tickers":[],"confidence":0.4,"reason":"weak"}'
    candidate_response = {
        "schema_scanner_mapping": '{"action":"hold","tickers":["NVDA"],"confidence":0.9,"reason":"disciplined"}',
        "risk_kill_switch": '{"action":"hold","tickers":["SPY"],"confidence":0.9,"reason":"disciplined"}',
        "contradiction_check": '{"action":"hold","tickers":["AAPL"],"confidence":0.9,"reason":"disciplined"}',
    }
    runtime = FakeRuntime(
        installed={"gpt-oss:20b", "qwen3.6:27b"},
        running={"gpt-oss:20b": 17.0},
        sizes={"gpt-oss:20b": 17.0, "qwen3.6:27b": 24.0},
        responses={"gpt-oss:20b": baseline_response, "qwen3.6:27b": candidate_response},
    )
    service = LLMEvalService(
        storage=_storage(tmp_path),
        runtime=runtime,
        automation_config_path=automation_path,
        autopilot_config_path=autopilot_path,
    )

    result = service.run(LLMEvalRequest(models=["qwen3.6:27b"], baseline_model="gpt-oss:20b"))

    assert result["recommendation"]["decision"] == "promote"
    assert result["recommendation"]["promoted"] is True
    assert result["restored_model"] == "qwen3.6:27b"
    assert "qwen3.6:27b" in automation_path.read_text()
    assert "qwen3.6:27b" in autopilot_path.read_text()


def test_llm_eval_blocks_when_platform_job_is_active(tmp_path):
    storage = _storage(tmp_path)
    storage.upsert_run(
        {
            "run_id": "run-active",
            "status": "running",
            "started_at": "2026-05-03T08:00:00+00:00",
            "request": {"ticker": "SPY", "analysis_date": "2026-05-03"},
            "stats": {},
        }
    )
    service = LLMEvalService(
        storage=storage,
        runtime=FakeRuntime(installed={"gpt-oss:20b"}, sizes={"gpt-oss:20b": 17.0}),
    )

    with pytest.raises(RuntimeError):
        service.run(LLMEvalRequest(models=[], baseline_model="gpt-oss:20b"))


def test_llm_eval_storage_roundtrip(tmp_path):
    storage = _storage(tmp_path)
    result = {
        "eval_id": "llm-eval-1",
        "status": "completed",
        "baseline_model": "gpt-oss:20b",
        "started_at": "2026-05-03T08:00:00+00:00",
        "ended_at": "2026-05-03T08:01:00+00:00",
        "config": {"max_runtime_gib": 35},
        "recommendation": {"decision": "keep", "model": "gpt-oss:20b"},
        "model_results": [
            {
                "model": "gpt-oss:20b",
                "status": "completed",
                "composite_score": 90.0,
                "metrics": {"runtime_gib": 17.0, "p95_latency_ms": 100.0},
                "decision": {"decision": "candidate"},
                "prompt_results": [
                    {"prompt_id": "p1", "status": "completed", "latency_ms": 100.0}
                ],
            }
        ],
    }

    storage.upsert_llm_eval_run(result)

    assert storage.llm_eval_runs()[0]["eval_id"] == "llm-eval-1"
    assert storage.llm_eval_run_detail("llm-eval-1")["model_results"][0]["model"] == "gpt-oss:20b"
    assert storage.latest_llm_eval_scorecard()["recommendation"]["decision"] == "keep"
