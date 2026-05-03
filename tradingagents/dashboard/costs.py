from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dt_time, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from tradingagents.dashboard.storage import DashboardStorage


OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"
DEFAULT_OPENAI_COST_BASELINE_PATH = (
    Path.home() / ".tradingagents" / "dashboard" / "openai_cost_baseline.json"
)


def day_start_epoch(day: date) -> int:
    return int(datetime.combine(day, dt_time.min, tzinfo=timezone.utc).timestamp())


def cost_window_start(days: int) -> int:
    if days <= 1:
        return day_start_epoch(date.today())
    return int(time.time()) - days * 24 * 60 * 60


def cached_openai_costs(storage: DashboardStorage, *, days: int = 1) -> List[Dict[str, Any]]:
    start = cost_window_start(days)
    return [
        item
        for item in storage.openai_costs(limit=1000)
        if int(item.get("start_time", 0) or 0) >= start
    ]


def openai_cost_baseline_path() -> Path:
    return Path(
        os.getenv(
            "TRADINGAGENTS_OPENAI_COST_BASELINE_PATH",
            str(DEFAULT_OPENAI_COST_BASELINE_PATH),
        )
    )


def load_openai_cost_baseline(path: Optional[Path] = None) -> Dict[str, Any]:
    baseline_path = Path(path or openai_cost_baseline_path())
    if not baseline_path.exists():
        return {}
    with baseline_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_openai_cost_baseline(
    *,
    baseline_spend_usd: float,
    path: Optional[Path] = None,
    created_at: Optional[str] = None,
) -> Dict[str, Any]:
    baseline = {
        "date": date.today().isoformat(),
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "baseline_spend_usd": float(baseline_spend_usd),
    }
    baseline_path = Path(path or openai_cost_baseline_path())
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    with baseline_path.open("w", encoding="utf-8") as f:
        json.dump(baseline, f, indent=2, sort_keys=True)
    return baseline


def apply_openai_cost_baseline(raw_total: float, *, days: int = 1) -> Dict[str, Any]:
    baseline = load_openai_cost_baseline()
    baseline_amount = 0.0
    if days <= 1 and baseline.get("date") == date.today().isoformat():
        baseline_amount = float(baseline.get("baseline_spend_usd", 0.0) or 0.0)
    return {
        "total": max(float(raw_total) - baseline_amount, 0.0),
        "raw_total": float(raw_total),
        "baseline": baseline,
        "baseline_applied_usd": baseline_amount,
    }


def parse_costs_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    costs: List[Dict[str, Any]] = []
    for bucket in payload.get("data", []):
        start_time = bucket.get("start_time", 0)
        end_time = bucket.get("end_time", 0)
        for result in bucket.get("results", []):
            amount = result.get("amount", {})
            costs.append(
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "amount": float(amount.get("value", 0.0)),
                    "currency": amount.get("currency", "usd"),
                    "line_item": result.get("line_item"),
                    "project_id": result.get("project_id"),
                    "payload": result,
                }
            )
    return costs


def fetch_openai_costs(
    *,
    admin_key: str,
    start_time: int,
    limit: int = 30,
    timeout: int = 20,
) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"start_time": start_time, "bucket_width": "1d", "limit": limit}
    )
    request = urllib.request.Request(
        f"{OPENAI_COSTS_URL}?{params}",
        headers={"Authorization": f"Bearer {admin_key}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return parse_costs_payload(payload)


def refresh_openai_costs(
    storage: DashboardStorage,
    *,
    days: int = 1,
    admin_key: Optional[str] = None,
) -> Dict[str, Any]:
    start = cost_window_start(days)
    key = admin_key or os.getenv("OPENAI_ADMIN_KEY")
    if not key:
        cached = cached_openai_costs(storage, days=days)
        raw_total = sum(item["amount"] for item in cached)
        totals = apply_openai_cost_baseline(raw_total, days=days)
        return {
            "configured": False,
            "costs": cached,
            "total": totals["total"],
            "raw_total": totals["raw_total"],
            "baseline": totals["baseline"],
            "baseline_applied_usd": totals["baseline_applied_usd"],
            "period_days": days,
            "period_start": start,
        }
    costs = fetch_openai_costs(admin_key=key, start_time=start, limit=days)
    storage.replace_openai_costs(costs)
    raw_total = sum(item["amount"] for item in costs)
    totals = apply_openai_cost_baseline(raw_total, days=days)
    return {
        "configured": True,
        "costs": costs,
        "total": totals["total"],
        "raw_total": totals["raw_total"],
        "baseline": totals["baseline"],
        "baseline_applied_usd": totals["baseline_applied_usd"],
        "period_days": days,
        "period_start": start,
    }


def raw_today_openai_spend(storage: DashboardStorage, admin_key: Optional[str] = None) -> float:
    key = admin_key or os.getenv("OPENAI_ADMIN_KEY")
    if key:
        costs = fetch_openai_costs(
            admin_key=key,
            start_time=day_start_epoch(date.today()),
            limit=1,
        )
        storage.replace_openai_costs(costs)
        return sum(item["amount"] for item in costs)

    today_start = day_start_epoch(date.today())
    return sum(
        item["amount"]
        for item in storage.openai_costs()
        if int(item.get("start_time", 0)) >= today_start
    )


def today_openai_spend(storage: DashboardStorage, admin_key: Optional[str] = None) -> float:
    raw_total = raw_today_openai_spend(storage, admin_key=admin_key)
    return float(apply_openai_cost_baseline(raw_total, days=1)["total"])


def reset_openai_cost_baseline(
    storage: DashboardStorage,
    *,
    admin_key: Optional[str] = None,
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    raw_total = raw_today_openai_spend(storage, admin_key=admin_key)
    baseline = save_openai_cost_baseline(
        baseline_spend_usd=raw_total,
        path=path,
    )
    return {
        "status": "reset",
        "baseline": baseline,
        "raw_total": raw_total,
        "effective_total": 0.0,
    }
