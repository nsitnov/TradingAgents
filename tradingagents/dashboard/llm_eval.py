from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field

from tradingagents.dashboard.automation import (
    DEFAULT_AUTOMATION_CONFIG_PATH,
    load_automation_config,
    save_automation_config,
)
from tradingagents.dashboard.autopilot import (
    DEFAULT_AUTOPILOT_CONFIG_PATH,
    load_autopilot_config,
    save_autopilot_config,
)
from tradingagents.dashboard.storage import DashboardStorage, now_iso


TERMINAL_STATUSES = {"completed", "error", "cancelled", "skipped", "disabled"}
DEFAULT_BASELINE_MODEL = "gpt-oss:20b"
DEFAULT_CANDIDATE_MODELS = ["qwen3.6:27b", "qwen3.6:35b"]
DEFAULT_MAX_RUNTIME_GIB = 35.0
DEFAULT_MEMORY_RESERVE_GIB = 32.0
DEFAULT_BACKEND_URL = "http://localhost:11434"
KNOWN_MODEL_DISK_GIB = {
    "gpt-oss:20b": 13.0,
    "qwen3.6:27b": 17.0,
    "qwen3.6:35b": 24.0,
    "nemotron-3-super": 87.0,
}


DEFAULT_EVAL_PROMPTS = [
    {
        "prompt_id": "schema_scanner_mapping",
        "category": "schema",
        "allowed_tickers": ["NVDA", "AMD", "SMH"],
        "risk_case": False,
        "prompt": (
            "Return strict JSON only with keys action, tickers, confidence, reason. "
            "Event: TSMC reports stronger AI-chip demand. Map likely US-listed "
            "semiconductor beneficiaries from NVDA, AMD, SMH. Do not add tickers "
            "outside that list."
        ),
    },
    {
        "prompt_id": "risk_kill_switch",
        "category": "risk",
        "allowed_tickers": ["SPY"],
        "risk_case": True,
        "prompt": (
            "Return strict JSON only with keys action, tickers, confidence, reason. "
            "The weekly drawdown kill switch is active after a 12% loss. A scanner "
            "signal says buy SPY. Respect the risk gate."
        ),
    },
    {
        "prompt_id": "contradiction_check",
        "category": "reasoning",
        "allowed_tickers": ["AAPL", "QQQ"],
        "risk_case": False,
        "prompt": (
            "Return strict JSON only with keys action, tickers, confidence, reason. "
            "AAPL revenue beat expectations, but guidance was cut and QQQ is down "
            "2% premarket. Choose a cautious paper-trading action."
        ),
    },
]


class LLMEvalRequest(BaseModel):
    models: List[str] = Field(default_factory=lambda: list(DEFAULT_CANDIDATE_MODELS))
    baseline_model: str = DEFAULT_BASELINE_MODEL
    max_runtime_gib: float = DEFAULT_MAX_RUNTIME_GIB
    memory_reserve_gib: float = DEFAULT_MEMORY_RESERVE_GIB
    auto_promote: bool = True
    allow_pull: bool = False
    backend_url: str = DEFAULT_BACKEND_URL


@dataclass
class OllamaModel:
    name: str
    size_gib: float = 0.0
    processor: str = ""
    context: str = ""


class OllamaRuntime:
    def __init__(self, backend_url: str = DEFAULT_BACKEND_URL) -> None:
        self.backend_url = backend_url.rstrip("/")

    def installed_models(self) -> List[str]:
        payload = self._api_get("/api/tags", timeout=5.0)
        return [str(item.get("name", "")) for item in payload.get("models", [])]

    def running_models(self) -> List[OllamaModel]:
        result = subprocess.run(
            ["ollama", "ps"],
            check=True,
            capture_output=True,
            text=True,
        )
        return _parse_ollama_ps(result.stdout)

    def pull(self, model: str) -> None:
        subprocess.run(["ollama", "pull", model], check=True)

    def stop(self, model: str) -> None:
        subprocess.run(["ollama", "stop", model], check=False, capture_output=True, text=True)

    def available_memory_gib(self) -> float:
        try:
            raw = Path("/proc/meminfo").read_text(encoding="utf-8")
            match = re.search(r"^MemAvailable:\s+(\d+)\s+kB", raw, re.M)
            if not match:
                return 0.0
            return int(match.group(1)) / 1024 / 1024
        except OSError:
            return 0.0

    def generate(self, model: str, prompt: str, *, timeout: float = 120.0) -> str:
        payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
        request = urllib.request.Request(
            f"{self.backend_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        return str(data.get("response", ""))

    def _api_get(self, path: str, *, timeout: float) -> Dict[str, Any]:
        with urllib.request.urlopen(f"{self.backend_url}{path}", timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))


@dataclass
class LLMEvalService:
    storage: DashboardStorage
    runtime: OllamaRuntime = field(default_factory=OllamaRuntime)
    automation_config_path: Path = DEFAULT_AUTOMATION_CONFIG_PATH
    autopilot_config_path: Path = DEFAULT_AUTOPILOT_CONFIG_PATH

    def run(self, request: LLMEvalRequest) -> Dict[str, Any]:
        active_jobs = self._active_platform_jobs()
        if active_jobs:
            raise RuntimeError(f"LLM evaluation blocked by active platform jobs: {active_jobs}")

        started_at = now_iso()
        eval_id = f"llm-eval-{uuid4().hex[:12]}"
        models = _ordered_unique([request.baseline_model, *request.models])
        owned_models = set(models)
        initial_running = self.runtime.running_models()
        protected_models = {
            model.name for model in initial_running if model.name != request.baseline_model
        }
        foreign_running = sorted(protected_models)
        result: Dict[str, Any] = {
            "eval_id": eval_id,
            "status": "running",
            "baseline_model": request.baseline_model,
            "started_at": started_at,
            "ended_at": None,
            "config": request.model_dump(),
            "foreign_running_models": foreign_running,
            "model_results": [],
            "recommendation": {},
            "error": None,
        }
        restore_model = request.baseline_model
        self.storage.upsert_llm_eval_run(result)

        try:
            installed = set(self.runtime.installed_models())
            for model in models:
                self._stop_owned_running_models(owned_models, protected_models)
                model_result = self._evaluate_model(model, request, installed, protected_models)
                result["model_results"].append(model_result)
                self.storage.upsert_llm_eval_run(result)

            result["recommendation"] = self._recommend(result, request)
            if result["recommendation"].get("decision") == "promote" and request.auto_promote:
                self._promote_model(str(result["recommendation"]["model"]))
                result["recommendation"]["promoted"] = True
                restore_model = str(result["recommendation"]["model"])
            else:
                result["recommendation"]["promoted"] = False
            result["status"] = "completed"
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)
        finally:
            try:
                self._stop_owned_running_models(owned_models, protected_models)
                self.runtime.generate(restore_model, "Reply with exactly: ready", timeout=60.0)
                result["baseline_restored"] = True
                result["restored_model"] = restore_model
            except Exception as exc:
                result["baseline_restored"] = False
                result["restore_error"] = str(exc)
            result["ended_at"] = now_iso()
            self.storage.upsert_llm_eval_run(result)
        return result

    def runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.storage.llm_eval_runs(limit=limit)

    def detail(self, eval_id: str) -> Optional[Dict[str, Any]]:
        return self.storage.llm_eval_run_detail(eval_id)

    def scorecard(self) -> Dict[str, Any]:
        return self.storage.latest_llm_eval_scorecard()

    def _evaluate_model(
        self,
        model: str,
        request: LLMEvalRequest,
        installed: set[str],
        protected_models: set[str],
    ) -> Dict[str, Any]:
        created_at = now_iso()
        if model in protected_models:
            return _skipped_model_result(
                model,
                "protected_foreign_running",
                "Model was already running before evaluation and is treated as foreign",
            )
        if not self._is_model_allowed_by_estimate(model, request.max_runtime_gib):
            return _skipped_model_result(model, "too_large", "Estimated model size exceeds policy")
        if model not in installed:
            if not request.allow_pull:
                return _skipped_model_result(model, "missing", "Model is not installed")
            self.runtime.pull(model)
            installed.add(model)

        available_gib = self.runtime.available_memory_gib()
        if available_gib and available_gib < request.memory_reserve_gib:
            return _skipped_model_result(
                model,
                "memory_reserved",
                "Available memory is below the configured reserve",
            )

        prompt_results: List[Dict[str, Any]] = []
        for case in DEFAULT_EVAL_PROMPTS:
            started = time.perf_counter()
            try:
                response = self.runtime.generate(model, case["prompt"])
                latency_ms = (time.perf_counter() - started) * 1000.0
                prompt_results.append(_score_prompt_response(case, response, latency_ms))
            except Exception as exc:
                latency_ms = (time.perf_counter() - started) * 1000.0
                prompt_results.append(
                    {
                        "prompt_id": case["prompt_id"],
                        "status": "error",
                        "latency_ms": round(latency_ms, 2),
                        "valid_json": False,
                        "risk_violation": False,
                        "hallucinated_ticker": False,
                        "judge_score": 0.0,
                        "error": str(exc),
                    }
                )

        running = {item.name: item for item in self.runtime.running_models()}
        runtime_gib = running.get(model, OllamaModel(model)).size_gib
        if runtime_gib > request.max_runtime_gib:
            metrics = _metrics(prompt_results, runtime_gib)
            return {
                "model": model,
                "status": "too_large",
                "created_at": created_at,
                "composite_score": 0.0,
                "metrics": metrics,
                "decision": {"decision": "reject", "reason": "Runtime footprint exceeds policy"},
                "prompt_results": prompt_results,
            }

        metrics = _metrics(prompt_results, runtime_gib)
        composite = _composite_score(metrics)
        return {
            "model": model,
            "status": "completed",
            "created_at": created_at,
            "composite_score": composite,
            "metrics": metrics,
            "decision": {"decision": "candidate", "reason": "Evaluation completed"},
            "prompt_results": prompt_results,
        }

    def _recommend(self, result: Dict[str, Any], request: LLMEvalRequest) -> Dict[str, Any]:
        completed = {
            item["model"]: item
            for item in result.get("model_results", [])
            if item.get("status") == "completed"
        }
        baseline = completed.get(request.baseline_model)
        if not baseline:
            return {
                "decision": "keep",
                "model": request.baseline_model,
                "reason": "Baseline did not complete; no promotion is allowed",
            }
        best = max(completed.values(), key=lambda item: float(item.get("composite_score", 0.0)))
        if best["model"] == request.baseline_model:
            return {
                "decision": "keep",
                "model": request.baseline_model,
                "reason": "Baseline remains the best completed model",
            }
        gates = _promotion_gates(best, baseline, request.max_runtime_gib)
        if gates:
            return {
                "decision": "reject",
                "model": best["model"],
                "reason": "; ".join(gates),
                "baseline_model": request.baseline_model,
            }
        return {
            "decision": "promote",
            "model": best["model"],
            "reason": "Candidate passed all quality, safety, and memory gates",
            "baseline_model": request.baseline_model,
        }

    def _promote_model(self, model: str) -> None:
        automation = load_automation_config(self.automation_config_path)
        automation.setdefault("run_request", {})["shallow_thinker"] = model
        automation["run_request"]["quick_llm_provider"] = "ollama"
        automation["run_request"]["quick_backend_url"] = "http://localhost:11434/v1"
        save_automation_config(automation, self.automation_config_path)

        autopilot = load_autopilot_config(self.autopilot_config_path)
        autopilot.setdefault("llm_routing", {})["quick_think_llm"] = model
        autopilot["llm_routing"]["quick_llm_provider"] = "ollama"
        autopilot["llm_routing"]["quick_backend_url"] = "http://localhost:11434/v1"
        save_autopilot_config(autopilot, self.autopilot_config_path)

    def _active_platform_jobs(self) -> List[str]:
        active: List[str] = []
        for run in self.storage.recent_runs(limit=25):
            if str(run.get("status", "")).lower() not in TERMINAL_STATUSES:
                active.append(f"run:{run.get('run_id')}")
        for job in self.storage.agent_replay_jobs(limit=25):
            if str(job.get("status", "")).lower() not in TERMINAL_STATUSES:
                active.append(f"agent_replay:{job.get('job_id')}")
        for job in self.storage.autopilot_jobs(limit=25):
            if str(job.get("status", "")).lower() not in TERMINAL_STATUSES:
                active.append(f"autopilot:{job.get('job_id')}")
        for backtest in self.storage.backtests(limit=25):
            if str(backtest.get("status", "")).lower() not in TERMINAL_STATUSES:
                active.append(f"backtest:{backtest.get('backtest_id')}")
        return active

    def _stop_owned_running_models(
        self,
        owned_models: set[str],
        protected_models: Optional[set[str]] = None,
    ) -> None:
        protected = protected_models or set()
        for model in self.runtime.running_models():
            if model.name in owned_models and model.name not in protected:
                self.runtime.stop(model.name)

    def _is_model_allowed_by_estimate(self, model: str, max_runtime_gib: float) -> bool:
        estimate = KNOWN_MODEL_DISK_GIB.get(model)
        if estimate is None:
            return True
        return estimate <= max_runtime_gib


def _parse_ollama_ps(output: str) -> List[OllamaModel]:
    models: List[OllamaModel] = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        name = parts[0]
        size_gib = _parse_size_gib(" ".join(parts[2:4]))
        processor = parts[4] if len(parts) > 4 else ""
        context = parts[5] if len(parts) > 5 else ""
        models.append(OllamaModel(name=name, size_gib=size_gib, processor=processor, context=context))
    return models


def _parse_size_gib(raw: str) -> float:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]i?B|[KMGT]B)", raw, re.I)
    if not match:
        return 0.0
    value = float(match.group(1))
    unit = match.group(2).upper()
    if unit.startswith("K"):
        return value / 1024 / 1024
    if unit.startswith("M"):
        return value / 1024
    if unit.startswith("T"):
        return value * 1024
    return value


def _score_prompt_response(case: Dict[str, Any], response: str, latency_ms: float) -> Dict[str, Any]:
    parsed = _extract_json(response)
    valid_json = isinstance(parsed, dict)
    tickers = [str(item).upper() for item in parsed.get("tickers", [])] if valid_json else []
    allowed = set(case.get("allowed_tickers", []))
    hallucinated = any(ticker not in allowed for ticker in tickers)
    action = str(parsed.get("action", "")).lower() if valid_json else ""
    risk_violation = bool(case.get("risk_case")) and action in {"buy", "sell", "long", "short"}
    judge_score = 1.0
    if not valid_json:
        judge_score = 0.0
    elif hallucinated or risk_violation:
        judge_score = 0.25
    elif not action or not tickers:
        judge_score = 0.7
    return {
        "prompt_id": case["prompt_id"],
        "status": "completed" if valid_json else "invalid_json",
        "latency_ms": round(latency_ms, 2),
        "valid_json": valid_json,
        "risk_violation": risk_violation,
        "hallucinated_ticker": hallucinated,
        "judge_score": judge_score,
        "response": response[:2000],
        "parsed": parsed if valid_json else None,
    }


def _extract_json(response: str) -> Any:
    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


def _metrics(prompt_results: List[Dict[str, Any]], runtime_gib: float) -> Dict[str, Any]:
    total = max(len(prompt_results), 1)
    latencies = [float(item.get("latency_ms", 0.0)) for item in prompt_results]
    sorted_latencies = sorted(latencies)
    p95 = sorted_latencies[-1] if len(sorted_latencies) < 2 else statistics.quantiles(sorted_latencies, n=20)[18]
    return {
        "prompt_count": len(prompt_results),
        "schema_success_rate": _ratio(prompt_results, "valid_json", total),
        "invalid_json_rate": 1.0 - _ratio(prompt_results, "valid_json", total),
        "hallucinated_ticker_rate": _ratio(prompt_results, "hallucinated_ticker", total),
        "risk_instruction_violation_rate": _ratio(prompt_results, "risk_violation", total),
        "fallback_needed_rate": len([item for item in prompt_results if item.get("status") == "error"]) / total,
        "judge_score": round(
            sum(float(item.get("judge_score", 0.0)) for item in prompt_results) / total,
            4,
        ),
        "p95_latency_ms": round(p95, 2),
        "runtime_gib": round(float(runtime_gib), 2),
    }


def _composite_score(metrics: Dict[str, Any]) -> float:
    correctness = float(metrics.get("judge_score", 0.0))
    schema = float(metrics.get("schema_success_rate", 0.0))
    risk = 1.0 - float(metrics.get("risk_instruction_violation_rate", 0.0))
    latency = max(0.0, 1.0 - min(float(metrics.get("p95_latency_ms", 0.0)), 60_000.0) / 60_000.0)
    memory = max(0.0, 1.0 - min(float(metrics.get("runtime_gib", 0.0)), DEFAULT_MAX_RUNTIME_GIB) / DEFAULT_MAX_RUNTIME_GIB)
    score = 100.0 * (
        0.40 * correctness
        + 0.25 * schema
        + 0.20 * risk
        + 0.10 * latency
        + 0.05 * memory
    )
    return round(score, 2)


def _promotion_gates(
    candidate: Dict[str, Any],
    baseline: Dict[str, Any],
    max_runtime_gib: float,
) -> List[str]:
    reasons: List[str] = []
    candidate_score = float(candidate.get("composite_score", 0.0))
    baseline_score = float(baseline.get("composite_score", 0.0))
    metrics = candidate.get("metrics", {})
    baseline_metrics = baseline.get("metrics", {})
    if candidate_score < baseline_score * 1.07:
        reasons.append("score improvement is below 7%")
    if float(metrics.get("schema_success_rate", 0.0)) < 0.98:
        reasons.append("schema success is below 98%")
    if float(metrics.get("risk_instruction_violation_rate", 0.0)) > 0.0:
        reasons.append("risk instruction violations were detected")
    if float(metrics.get("hallucinated_ticker_rate", 0.0)) > float(
        baseline_metrics.get("hallucinated_ticker_rate", 0.0)
    ):
        reasons.append("hallucinated ticker rate is worse than baseline")
    if float(metrics.get("runtime_gib", 0.0)) > max_runtime_gib:
        reasons.append("runtime footprint exceeds policy")
    if float(metrics.get("fallback_needed_rate", 0.0)) > float(
        baseline_metrics.get("fallback_needed_rate", 0.0)
    ):
        reasons.append("fallback-needed rate is worse than baseline")
    return reasons


def _ratio(items: List[Dict[str, Any]], key: str, total: int) -> float:
    return round(len([item for item in items if item.get(key)]) / total, 4)


def _skipped_model_result(model: str, status: str, reason: str) -> Dict[str, Any]:
    return {
        "model": model,
        "status": status,
        "created_at": now_iso(),
        "composite_score": 0.0,
        "metrics": {
            "prompt_count": 0,
            "schema_success_rate": 0.0,
            "invalid_json_rate": 0.0,
            "hallucinated_ticker_rate": 0.0,
            "risk_instruction_violation_rate": 0.0,
            "fallback_needed_rate": 0.0,
            "judge_score": 0.0,
            "p95_latency_ms": 0.0,
            "runtime_gib": 0.0,
        },
        "decision": {"decision": "reject", "reason": reason},
        "prompt_results": [],
    }


def _ordered_unique(items: List[str]) -> List[str]:
    seen: set[str] = set()
    unique: List[str] = []
    for item in items:
        if item and item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate local Ollama models for TradingAgents")
    parser.add_argument("--models", default=",".join(DEFAULT_CANDIDATE_MODELS))
    parser.add_argument("--baseline", default=DEFAULT_BASELINE_MODEL)
    parser.add_argument("--max-runtime-gib", type=float, default=DEFAULT_MAX_RUNTIME_GIB)
    parser.add_argument("--memory-reserve-gib", type=float, default=DEFAULT_MEMORY_RESERVE_GIB)
    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL)
    parser.add_argument("--pull", action="store_true")
    parser.add_argument("--no-auto-promote", action="store_true")
    args = parser.parse_args()

    service = LLMEvalService(
        storage=DashboardStorage(),
        runtime=OllamaRuntime(args.backend_url),
    )
    request = LLMEvalRequest(
        models=[item.strip() for item in args.models.split(",") if item.strip()],
        baseline_model=args.baseline,
        max_runtime_gib=args.max_runtime_gib,
        memory_reserve_gib=args.memory_reserve_gib,
        auto_promote=not args.no_auto_promote,
        allow_pull=args.pull,
        backend_url=args.backend_url,
    )
    result = service.run(request)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
