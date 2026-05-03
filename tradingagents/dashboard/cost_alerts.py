from __future__ import annotations

import argparse
import html
import json
import os
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import requests

from tradingagents.dashboard.costs import (
    cached_openai_costs,
    reset_openai_cost_baseline,
    today_openai_spend,
)
from tradingagents.dashboard.storage import DashboardStorage
from tradingagents.dashboard.weekly_report import (
    RESEND_EMAILS_URL,
    load_dashboard_env,
)


DEFAULT_ALERT_THRESHOLD_USD = 5.0
DEFAULT_AUTOMATION_CONFIG_PATH = (
    Path.home() / ".tradingagents" / "dashboard" / "automation.json"
)


def daily_openai_alert_threshold() -> float:
    configured = os.getenv("TRADINGAGENTS_DAILY_OPENAI_ALERT_USD")
    if configured:
        return float(configured)
    try:
        with DEFAULT_AUTOMATION_CONFIG_PATH.open("r", encoding="utf-8") as f:
            config = json.load(f)
        return float(config.get("daily_openai_budget_usd", DEFAULT_ALERT_THRESHOLD_USD))
    except Exception:
        return DEFAULT_ALERT_THRESHOLD_USD


def check_daily_openai_cost_alert(
    *,
    storage: Optional[DashboardStorage] = None,
    threshold_usd: Optional[float] = None,
    dry_run: bool = False,
    force: bool = False,
) -> Dict[str, Any]:
    load_dashboard_env()
    store = storage or DashboardStorage()
    threshold = float(threshold_usd if threshold_usd is not None else daily_openai_alert_threshold())
    spend = float(today_openai_spend(store))
    alert_date = date.today().isoformat()
    alert_id = _alert_id(alert_date, threshold)
    payload = {
        "alert_id": alert_id,
        "date": alert_date,
        "spend_usd": spend,
        "threshold_usd": threshold,
        "cost_rows": _today_rows(store),
    }

    if spend < threshold:
        return {"status": "below_threshold", **payload}

    if not force and _alert_already_sent(store, alert_id):
        return {"status": "already_sent", **payload}

    if dry_run:
        return {
            "status": "would_send",
            **payload,
            "email": _email_payload(payload),
        }

    email = send_daily_openai_cost_alert(payload)
    store.insert_audit_event(
        "cost_alert_sent",
        "openai_cost_alert",
        alert_id,
        None,
        {**payload, "email": email},
    )
    return {"status": "sent", "email": email, **payload}


def send_daily_openai_cost_alert(
    alert: Dict[str, Any],
    *,
    api_key: Optional[str] = None,
    sender: Optional[str] = None,
    recipients: Optional[Sequence[str]] = None,
    timeout: float = 20.0,
) -> Dict[str, Any]:
    key = api_key or _required_env("RESEND_API_KEY")
    from_address = sender or _required_env("TRADINGAGENTS_REPORT_FROM")
    to_addresses = list(recipients or _parse_recipients(_required_env("TRADINGAGENTS_REPORT_TO")))
    payload = _email_payload(alert, sender=from_address, recipients=to_addresses)
    response = requests.post(
        RESEND_EMAILS_URL,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Idempotency-Key": str(alert["alert_id"]),
        },
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Resend email failed with HTTP {response.status_code}: {response.text}"
        )
    return response.json()


def _today_rows(storage: DashboardStorage) -> List[Dict[str, Any]]:
    rows = []
    for item in cached_openai_costs(storage, days=1):
        rows.append(
            {
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "amount": float(item.get("amount", 0.0)),
                "currency": item.get("currency", "usd"),
                "line_item": item.get("line_item"),
                "project_id": item.get("project_id"),
            }
        )
    return rows[:25]


def _email_payload(
    alert: Dict[str, Any],
    *,
    sender: Optional[str] = None,
    recipients: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    spend = float(alert["spend_usd"])
    threshold = float(alert["threshold_usd"])
    subject = f"TradingAgents OpenAI daily spend alert: ${spend:.2f} / ${threshold:.2f}"
    rows = alert.get("cost_rows", [])
    row_lines = [
        "- {project}: {amount} {currency}".format(
            project=row.get("project_id") or "unknown project",
            amount=_money(float(row.get("amount", 0.0))),
            currency=str(row.get("currency", "usd")).upper(),
        )
        for row in rows[:10]
    ]
    if not row_lines:
        row_lines = ["- No detailed cost rows were available."]
    text = "\n".join(
        [
            "TradingAgents OpenAI daily spend alert",
            "",
            f"Date: {alert['date']}",
            f"OpenAI organization/project spend today: {_money(spend)}",
            f"Alert threshold: {_money(threshold)}",
            "",
            "This tracks OpenAI Costs API spend. Local Ollama models do not create OpenAI API charges.",
            "",
            "Cost rows:",
            *row_lines,
        ]
    )
    html_rows = "".join(
        "<tr><td>{project}</td><td>{amount}</td><td>{currency}</td></tr>".format(
            project=html.escape(str(row.get("project_id") or "unknown project")),
            amount=html.escape(_money(float(row.get("amount", 0.0)))),
            currency=html.escape(str(row.get("currency", "usd")).upper()),
        )
        for row in rows[:10]
    )
    if not html_rows:
        html_rows = '<tr><td colspan="3">No detailed cost rows were available.</td></tr>'
    payload = {
        "subject": subject,
        "text": text,
        "html": f"""<!doctype html>
<html>
  <body style="font-family: Arial, sans-serif; color: #111827; line-height: 1.5;">
    <h1 style="font-size: 20px;">TradingAgents OpenAI daily spend alert</h1>
    <p><strong>Date:</strong> {html.escape(str(alert["date"]))}</p>
    <p><strong>Spend today:</strong> {html.escape(_money(spend))}<br>
       <strong>Threshold:</strong> {html.escape(_money(threshold))}</p>
    <p>This tracks OpenAI Costs API spend. Local Ollama models do not create OpenAI API charges.</p>
    <table cellpadding="6" cellspacing="0" style="border-collapse: collapse; border: 1px solid #e5e7eb;">
      <tr><th align="left">Project</th><th align="right">Amount</th><th align="left">Currency</th></tr>
      {html_rows}
    </table>
  </body>
</html>""",
    }
    if sender:
        payload["from"] = sender
    if recipients:
        payload["to"] = list(recipients)
    return payload


def _alert_id(alert_date: str, threshold: float) -> str:
    return f"openai-daily-cost-{alert_date}-{threshold:.2f}"


def _alert_already_sent(storage: DashboardStorage, alert_id: str) -> bool:
    return any(
        event.get("event_type") == "cost_alert_sent"
        and event.get("entity_type") == "openai_cost_alert"
        and event.get("entity_id") == alert_id
        for event in storage.audit_events(limit=1000)
    )


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _parse_recipients(raw: str) -> List[str]:
    recipients = [item.strip() for item in raw.split(",") if item.strip()]
    if not recipients:
        raise RuntimeError("TRADINGAGENTS_REPORT_TO must include at least one email")
    return recipients


def _money(value: float) -> str:
    return f"${value:,.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Check OpenAI daily spend and send a Resend alert")
    parser.add_argument("--threshold-usd", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--reset-baseline", action="store_true")
    args = parser.parse_args()

    if args.reset_baseline:
        load_dashboard_env()
        result = reset_openai_cost_baseline(DashboardStorage())
    else:
        result = check_daily_openai_cost_alert(
            threshold_usd=args.threshold_usd,
            dry_run=args.dry_run,
            force=args.force,
        )
    safe = dict(result)
    safe.pop("email", None)
    print(json.dumps(safe, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
