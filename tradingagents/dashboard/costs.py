from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, time as dt_time, timezone
from typing import Any, Dict, List, Optional

from tradingagents.dashboard.storage import DashboardStorage


OPENAI_COSTS_URL = "https://api.openai.com/v1/organization/costs"


def day_start_epoch(day: date) -> int:
    return int(datetime.combine(day, dt_time.min, tzinfo=timezone.utc).timestamp())


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
    days: int = 30,
    admin_key: Optional[str] = None,
) -> Dict[str, Any]:
    key = admin_key or os.getenv("OPENAI_ADMIN_KEY")
    if not key:
        return {
            "configured": False,
            "costs": storage.openai_costs(),
            "total": sum(item["amount"] for item in storage.openai_costs()),
        }
    start = int(time.time()) - days * 24 * 60 * 60
    costs = fetch_openai_costs(admin_key=key, start_time=start, limit=days)
    storage.replace_openai_costs(costs)
    return {
        "configured": True,
        "costs": costs,
        "total": sum(item["amount"] for item in costs),
    }


def today_openai_spend(storage: DashboardStorage, admin_key: Optional[str] = None) -> float:
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
