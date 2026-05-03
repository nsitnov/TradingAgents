from __future__ import annotations

import argparse
import html
import json
import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

from tradingagents.dashboard.ledger import PaperLedger
from tradingagents.dashboard.storage import DashboardStorage


RESEND_EMAILS_URL = "https://api.resend.com/emails"
DEFAULT_REPORT_TIMEZONE = "Europe/Sofia"


@dataclass(frozen=True)
class ReportPeriod:
    start: datetime
    end: datetime
    timezone_name: str


def load_dashboard_env() -> None:
    load_dotenv()
    load_dotenv(".env.enterprise", override=False)
    env_path = os.getenv(
        "TRADINGAGENTS_DASHBOARD_ENV",
        "/home/nsitnov/.config/tradingagents-dashboard.env",
    )
    if Path(env_path).exists():
        load_dotenv(env_path, override=False)


def weekly_period(
    now: Optional[datetime] = None,
    timezone_name: str = DEFAULT_REPORT_TIMEZONE,
) -> ReportPeriod:
    tz = ZoneInfo(timezone_name)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    current = current.astimezone(tz)
    week_start_date = current.date()
    week_start_date = week_start_date.fromordinal(
        week_start_date.toordinal() - current.weekday()
    )
    return ReportPeriod(
        start=datetime.combine(week_start_date, time.min, tzinfo=tz),
        end=current,
        timezone_name=timezone_name,
    )


def build_weekly_report(
    storage: DashboardStorage,
    *,
    now: Optional[datetime] = None,
    timezone_name: str = DEFAULT_REPORT_TIMEZONE,
) -> Dict[str, Any]:
    period = weekly_period(now=now, timezone_name=timezone_name)
    history = _snapshots_with_time(storage.portfolio_history(limit=5000), period)
    trades = _trades_with_time(storage.trades(limit=5000), period)

    snapshots_before_end = [item for item in history if item["_time"] <= period.end]
    snapshots_in_period = [
        item for item in snapshots_before_end if period.start <= item["_time"] <= period.end
    ]

    start_snapshot = _latest_at_or_before(history, period.start)
    if not start_snapshot and snapshots_in_period:
        start_snapshot = snapshots_in_period[0]
    end_snapshot = snapshots_before_end[-1] if snapshots_before_end else None

    start_metrics = _snapshot_metrics(start_snapshot)
    end_metrics = _snapshot_metrics(end_snapshot)
    series = _equity_series(start_snapshot, snapshots_in_period, end_snapshot)
    gross_gain, gross_loss = _gross_gain_loss(series)
    trades_in_period = [
        trade for trade in trades if period.start <= trade["_time"] <= period.end
    ]

    net_pnl = end_metrics["equity"] - start_metrics["equity"]
    start_equity = start_metrics["equity"]
    return {
        "period": {
            "start": period.start.isoformat(),
            "end": period.end.isoformat(),
            "timezone": period.timezone_name,
            "label": _period_label(period),
        },
        "portfolio": {
            "start_equity": start_equity,
            "end_equity": end_metrics["equity"],
            "net_pnl": net_pnl,
            "net_pnl_pct": (net_pnl / start_equity * 100.0) if start_equity else 0.0,
            "gross_gain": gross_gain,
            "gross_loss": gross_loss,
            "realized_pnl_change": end_metrics["realized_pnl"]
            - start_metrics["realized_pnl"],
            "unrealized_pnl_change": end_metrics["unrealized_pnl"]
            - start_metrics["unrealized_pnl"],
            "cash": end_metrics["cash"],
            "market_value": end_metrics["market_value"],
            "total_pnl": end_metrics["total_pnl"],
            "snapshot_count": len(snapshots_in_period),
        },
        "trades": _trade_summary(trades_in_period),
        "positions": _position_summary(end_snapshot),
        "has_data": bool(start_snapshot and end_snapshot),
    }


def render_report_text(report: Dict[str, Any]) -> str:
    portfolio = report["portfolio"]
    trades = report["trades"]
    positions = report["positions"]
    lines = [
        f"TradingAgents weekly portfolio report: {report['period']['label']}",
        "",
        f"Start equity: {_money(portfolio['start_equity'])}",
        f"End equity: {_money(portfolio['end_equity'])}",
        f"Net P&L: {_signed_money(portfolio['net_pnl'])} ({_pct(portfolio['net_pnl_pct'])})",
        f"Gross gains: {_money(portfolio['gross_gain'])}",
        f"Gross losses: {_money(portfolio['gross_loss'])}",
        f"Realized P&L change: {_signed_money(portfolio['realized_pnl_change'])}",
        f"Unrealized P&L change: {_signed_money(portfolio['unrealized_pnl_change'])}",
        f"Cash: {_money(portfolio['cash'])}",
        f"Market value: {_money(portfolio['market_value'])}",
        "",
        f"Trades: {trades['count']} total, {trades['buys']} buys, {trades['sells']} sells, {trades['holds']} holds",
        f"Buy notional: {_money(trades['buy_notional'])}",
        f"Sell notional: {_money(trades['sell_notional'])}",
        "",
        "Open positions:",
    ]
    if positions:
        for position in positions:
            lines.append(
                "- {ticker}: qty {quantity:.4f}, value {market_value}, unrealized {unrealized}".format(
                    ticker=position["ticker"],
                    quantity=position["quantity"],
                    market_value=_money(position["market_value"]),
                    unrealized=_signed_money(position["unrealized_pnl"]),
                )
            )
    else:
        lines.append("- No open positions")
    if not report["has_data"]:
        lines.extend(
            [
                "",
                "No portfolio snapshots were available for this period. The report is empty.",
            ]
        )
    return "\n".join(lines)


def render_report_html(report: Dict[str, Any]) -> str:
    portfolio = report["portfolio"]
    trades = report["trades"]
    positions = report["positions"]
    pnl_color = "#116329" if portfolio["net_pnl"] >= 0 else "#b42318"
    position_rows = "\n".join(
        "<tr><td>{ticker}</td><td>{quantity:.4f}</td><td>{market_value}</td><td>{unrealized}</td></tr>".format(
            ticker=html.escape(position["ticker"]),
            quantity=position["quantity"],
            market_value=html.escape(_money(position["market_value"])),
            unrealized=html.escape(_signed_money(position["unrealized_pnl"])),
        )
        for position in positions
    )
    if not position_rows:
        position_rows = '<tr><td colspan="4">No open positions</td></tr>'
    no_data = ""
    if not report["has_data"]:
        no_data = (
            "<p><strong>No portfolio snapshots were available for this period.</strong> "
            "The report is empty.</p>"
        )

    return f"""<!doctype html>
<html>
  <body style="font-family: Arial, sans-serif; color: #111827; line-height: 1.5;">
    <h1 style="font-size: 20px;">TradingAgents weekly portfolio report</h1>
    <p>{html.escape(report["period"]["label"])}</p>
    {no_data}
    <table cellpadding="6" cellspacing="0" style="border-collapse: collapse;">
      <tr><td>Start equity</td><td><strong>{html.escape(_money(portfolio["start_equity"]))}</strong></td></tr>
      <tr><td>End equity</td><td><strong>{html.escape(_money(portfolio["end_equity"]))}</strong></td></tr>
      <tr><td>Net P&amp;L</td><td style="color: {pnl_color};"><strong>{html.escape(_signed_money(portfolio["net_pnl"]))} ({html.escape(_pct(portfolio["net_pnl_pct"]))})</strong></td></tr>
      <tr><td>Gross gains</td><td>{html.escape(_money(portfolio["gross_gain"]))}</td></tr>
      <tr><td>Gross losses</td><td>{html.escape(_money(portfolio["gross_loss"]))}</td></tr>
      <tr><td>Realized P&amp;L change</td><td>{html.escape(_signed_money(portfolio["realized_pnl_change"]))}</td></tr>
      <tr><td>Unrealized P&amp;L change</td><td>{html.escape(_signed_money(portfolio["unrealized_pnl_change"]))}</td></tr>
      <tr><td>Cash</td><td>{html.escape(_money(portfolio["cash"]))}</td></tr>
      <tr><td>Market value</td><td>{html.escape(_money(portfolio["market_value"]))}</td></tr>
    </table>
    <h2 style="font-size: 16px;">Trades</h2>
    <p>{trades["count"]} total, {trades["buys"]} buys, {trades["sells"]} sells, {trades["holds"]} holds</p>
    <p>Buy notional: {html.escape(_money(trades["buy_notional"]))}<br>Sell notional: {html.escape(_money(trades["sell_notional"]))}</p>
    <h2 style="font-size: 16px;">Open positions</h2>
    <table cellpadding="6" cellspacing="0" style="border-collapse: collapse; border: 1px solid #e5e7eb;">
      <tr><th align="left">Ticker</th><th align="right">Quantity</th><th align="right">Market value</th><th align="right">Unrealized P&amp;L</th></tr>
      {position_rows}
    </table>
  </body>
</html>"""


def send_report_email(
    report: Dict[str, Any],
    *,
    api_key: str,
    sender: str,
    recipients: Sequence[str],
    subject_prefix: str = "TradingAgents weekly portfolio report",
    timeout: float = 20.0,
) -> Dict[str, Any]:
    subject = f"{subject_prefix}: {report['period']['label']}"
    payload = {
        "from": sender,
        "to": list(recipients),
        "subject": subject,
        "html": render_report_html(report),
        "text": render_report_text(report),
    }
    response = requests.post(
        RESEND_EMAILS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": _idempotency_key(report),
        },
        json=payload,
        timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Resend email failed with HTTP {response.status_code}: {response.text}"
        )
    return response.json()


def send_weekly_report(*, dry_run: bool = False) -> Dict[str, Any]:
    load_dashboard_env()
    timezone_name = os.getenv("TRADINGAGENTS_REPORT_TIMEZONE", DEFAULT_REPORT_TIMEZONE)
    storage = DashboardStorage()
    ledger = PaperLedger(storage=storage)
    ledger.sync_to_storage()
    current_snapshot = ledger.snapshot()
    current_snapshot["updated_at"] = datetime.now(timezone.utc).isoformat()
    storage.insert_portfolio_snapshot(current_snapshot, run_id="weekly-report")
    report = build_weekly_report(storage, timezone_name=timezone_name)

    if dry_run:
        return {"status": "dry_run", "report": report, "text": render_report_text(report)}

    api_key = _required_env("RESEND_API_KEY")
    sender = _required_env("TRADINGAGENTS_REPORT_FROM")
    recipients = _parse_recipients(_required_env("TRADINGAGENTS_REPORT_TO"))
    subject_prefix = os.getenv(
        "TRADINGAGENTS_REPORT_SUBJECT_PREFIX",
        "TradingAgents weekly portfolio report",
    )
    email = send_report_email(
        report,
        api_key=api_key,
        sender=sender,
        recipients=recipients,
        subject_prefix=subject_prefix,
    )
    return {"status": "sent", "email": email, "report": report}


def _snapshots_with_time(
    snapshots: Iterable[Dict[str, Any]], period: ReportPeriod
) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for snapshot in snapshots:
        created_at = snapshot.get("created_at")
        if not created_at:
            continue
        item = dict(snapshot)
        item["_time"] = _parse_datetime(created_at).astimezone(ZoneInfo(period.timezone_name))
        parsed.append(item)
    return sorted(parsed, key=lambda item: item["_time"])


def _trades_with_time(
    trades: Iterable[Dict[str, Any]], period: ReportPeriod
) -> List[Dict[str, Any]]:
    parsed: List[Dict[str, Any]] = []
    for trade in trades:
        created_at = trade.get("created_at") or trade.get("trade_date")
        if not created_at:
            continue
        item = dict(trade)
        item["_time"] = _parse_datetime(created_at).astimezone(ZoneInfo(period.timezone_name))
        parsed.append(item)
    return sorted(parsed, key=lambda item: item["_time"])


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    if "T" not in normalized and len(normalized) == 10:
        normalized = f"{normalized}T00:00:00+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _latest_at_or_before(
    snapshots: Sequence[Dict[str, Any]], when: datetime
) -> Optional[Dict[str, Any]]:
    candidates = [snapshot for snapshot in snapshots if snapshot["_time"] <= when]
    return candidates[-1] if candidates else None


def _snapshot_metrics(snapshot: Optional[Dict[str, Any]]) -> Dict[str, float]:
    if not snapshot:
        return {
            "cash": 0.0,
            "market_value": 0.0,
            "equity": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "total_pnl": 0.0,
        }
    return {
        "cash": float(snapshot.get("cash", 0.0)),
        "market_value": float(snapshot.get("market_value", 0.0)),
        "equity": float(snapshot.get("equity", 0.0)),
        "realized_pnl": float(snapshot.get("realized_pnl", 0.0)),
        "unrealized_pnl": float(snapshot.get("unrealized_pnl", 0.0)),
        "total_pnl": float(snapshot.get("total_pnl", 0.0)),
    }


def _equity_series(
    start_snapshot: Optional[Dict[str, Any]],
    snapshots_in_period: Sequence[Dict[str, Any]],
    end_snapshot: Optional[Dict[str, Any]],
) -> List[float]:
    snapshots: List[Dict[str, Any]] = []
    if start_snapshot:
        snapshots.append(start_snapshot)
    snapshots.extend(
        snapshot
        for snapshot in snapshots_in_period
        if not start_snapshot or snapshot.get("id") != start_snapshot.get("id")
    )
    if end_snapshot and all(snapshot.get("id") != end_snapshot.get("id") for snapshot in snapshots):
        snapshots.append(end_snapshot)
    return [float(snapshot.get("equity", 0.0)) for snapshot in snapshots]


def _gross_gain_loss(series: Sequence[float]) -> tuple[float, float]:
    gross_gain = 0.0
    gross_loss = 0.0
    for previous, current in zip(series, series[1:]):
        delta = current - previous
        if delta >= 0:
            gross_gain += delta
        else:
            gross_loss += abs(delta)
    return gross_gain, gross_loss


def _trade_summary(trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    buys = [trade for trade in trades if trade.get("action") == "buy"]
    sells = [trade for trade in trades if trade.get("action") == "sell"]
    holds = [trade for trade in trades if trade.get("action") == "hold"]
    return {
        "count": len(trades),
        "buys": len(buys),
        "sells": len(sells),
        "holds": len(holds),
        "tickers": sorted({str(trade.get("ticker", "")).upper() for trade in trades}),
        "buy_notional": sum(_trade_notional(trade) for trade in buys),
        "sell_notional": sum(_trade_notional(trade) for trade in sells),
    }


def _trade_notional(trade: Dict[str, Any]) -> float:
    return float(trade.get("quantity", 0.0)) * float(trade.get("price", 0.0))


def _position_summary(snapshot: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not snapshot:
        return []
    positions = snapshot.get("snapshot", {}).get("positions", {})
    rows = []
    for ticker, position in positions.items():
        quantity = float(position.get("quantity", 0.0))
        if quantity <= 0:
            continue
        rows.append(
            {
                "ticker": str(ticker).upper(),
                "quantity": quantity,
                "market_value": float(position.get("market_value", 0.0)),
                "unrealized_pnl": float(position.get("unrealized_pnl", 0.0)),
            }
        )
    return sorted(rows, key=lambda row: row["market_value"], reverse=True)


def _period_label(period: ReportPeriod) -> str:
    return (
        f"{period.start.strftime('%Y-%m-%d')} to "
        f"{period.end.strftime('%Y-%m-%d %H:%M')} {period.timezone_name}"
    )


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _signed_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.2f}"


def _pct(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}{abs(value):.2f}%"


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


def _idempotency_key(report: Dict[str, Any]) -> str:
    start = str(report["period"]["start"])[:10]
    end = str(report["period"]["end"])[:10]
    return f"tradingagents-weekly-report-{start}-{end}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Send TradingAgents weekly portfolio report")
    parser.add_argument("--dry-run", action="store_true", help="Print the report without sending")
    args = parser.parse_args()

    result = send_weekly_report(dry_run=args.dry_run)
    if args.dry_run:
        print(result["text"])
    else:
        print(json.dumps({"status": result["status"], "email": result["email"]}, indent=2))


if __name__ == "__main__":
    main()
