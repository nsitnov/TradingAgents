from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


DEFAULT_DB_PATH = Path.home() / ".tradingagents" / "dashboard" / "dashboard.sqlite3"
DEFAULT_ANALYSES_DIR = Path.home() / ".tradingagents" / "dashboard" / "analyses"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def _loads(raw: Optional[str], default: Any = None) -> Any:
    if not raw:
        return default
    return json.loads(raw)


class DashboardStorage:
    """SQLite-backed run, portfolio, automation, and cost history."""

    def __init__(
        self,
        db_path: Path = DEFAULT_DB_PATH,
        analyses_dir: Path = DEFAULT_ANALYSES_DIR,
    ) -> None:
        self.db_path = Path(db_path)
        self.analyses_dir = Path(analyses_dir)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    analysis_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    decision TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    request_json TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    error TEXT,
                    source TEXT NOT NULL DEFAULT 'manual',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, seq)
                );

                CREATE TABLE IF NOT EXISTS analysis_sections (
                    run_id TEXT NOT NULL,
                    section TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    file_path TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, section)
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    trade_date TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    trade_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    created_at TEXT NOT NULL,
                    cash REAL NOT NULL,
                    market_value REAL NOT NULL,
                    equity REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    total_pnl REAL NOT NULL,
                    snapshot_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS daily_jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    tickers_json TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS openai_costs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    start_time INTEGER NOT NULL,
                    end_time INTEGER NOT NULL,
                    amount REAL NOT NULL,
                    currency TEXT NOT NULL,
                    line_item TEXT,
                    project_id TEXT,
                    payload_json TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    estimated_price REAL NOT NULL,
                    estimated_notional REAL NOT NULL,
                    projected_position_market_value REAL NOT NULL,
                    mode TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    reason TEXT,
                    trade_date TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    order_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    ticker TEXT NOT NULL,
                    action TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    price REAL NOT NULL,
                    notional REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    fill_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS risk_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    checks_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    run_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS backtests (
                    backtest_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    config_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_replay_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    config_json TEXT NOT NULL,
                    progress_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scanner_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_hash TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    region TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    url TEXT,
                    published_at TEXT NOT NULL,
                    language TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scanner_signals (
                    signal_id TEXT PRIMARY KEY,
                    event_id INTEGER NOT NULL,
                    event_hash TEXT NOT NULL,
                    entity TEXT NOT NULL,
                    region TEXT NOT NULL,
                    category TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    score REAL NOT NULL,
                    confidence REAL NOT NULL,
                    reason TEXT NOT NULL,
                    targets_json TEXT NOT NULL,
                    signal_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS scanner_dislocations (
                    dislocation_id TEXT PRIMARY KEY,
                    signal_id TEXT NOT NULL,
                    event_id INTEGER NOT NULL,
                    entity TEXT NOT NULL,
                    reference_symbol TEXT NOT NULL,
                    target_symbol TEXT NOT NULL,
                    reference_move_pct REAL NOT NULL,
                    target_move_pct REAL NOT NULL,
                    gap_pct REAL NOT NULL,
                    z_score REAL NOT NULL,
                    spread_mean REAL NOT NULL,
                    spread_std REAL NOT NULL,
                    lookback_days INTEGER NOT NULL,
                    is_dislocated INTEGER NOT NULL,
                    direction TEXT NOT NULL,
                    dislocation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_unique
                ON trades(run_id, ticker, action, trade_date, created_at);
                """
            )

    def upsert_run(self, run: Dict[str, Any], source: str = "manual") -> None:
        request = run.get("request", {})
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, ticker, analysis_date, status, decision, started_at,
                    ended_at, request_json, stats_json, error, source, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status=excluded.status,
                    decision=excluded.decision,
                    ended_at=excluded.ended_at,
                    stats_json=excluded.stats_json,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    run["run_id"],
                    request.get("ticker", ""),
                    request.get("analysis_date", ""),
                    run.get("status", "unknown"),
                    run.get("decision"),
                    run.get("started_at") or now_iso(),
                    run.get("ended_at"),
                    _json(request),
                    _json(run.get("stats", {})),
                    run.get("error"),
                    source,
                    now_iso(),
                ),
            )

    def insert_event(self, event: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO run_events
                (run_id, seq, type, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event["run_id"],
                    event["seq"],
                    event["type"],
                    event["created_at"],
                    _json(event.get("payload", {})),
                ),
            )

    def upsert_section(
        self,
        *,
        run_id: str,
        ticker: str,
        analysis_date: str,
        section: str,
        title: str,
        content: str,
    ) -> None:
        path = self._analysis_dir(analysis_date, ticker, run_id) / f"{section}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO analysis_sections
                (run_id, section, title, content, file_path, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, section) DO UPDATE SET
                    title=excluded.title,
                    content=excluded.content,
                    file_path=excluded.file_path,
                    updated_at=excluded.updated_at
                """,
                (run_id, section, title, content, str(path), now_iso()),
            )

    def write_complete_analysis(
        self, run: Dict[str, Any], sections: Dict[str, Any]
    ) -> None:
        request = run.get("request", {})
        directory = self._analysis_dir(
            request.get("analysis_date", "unknown"),
            request.get("ticker", "UNKNOWN"),
            run["run_id"],
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "run.json").write_text(_json(run), encoding="utf-8")
        parts = [f"# {request.get('ticker', 'UNKNOWN')} Analysis", ""]
        for key, content in sections.items():
            if content:
                title = key.replace("_", " ").title()
                parts.extend([f"## {title}", str(content), ""])
        (directory / "complete_report.md").write_text("\n".join(parts), encoding="utf-8")

    def insert_trade(self, trade: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO trades
                (run_id, ticker, decision, action, quantity, price, trade_date, created_at, trade_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trade.get("run_id", ""),
                    trade.get("ticker", ""),
                    trade.get("decision", ""),
                    trade.get("action", ""),
                    float(trade.get("quantity", 0.0)),
                    float(trade.get("price", 0.0)),
                    trade.get("trade_date", ""),
                    trade.get("created_at") or now_iso(),
                    _json(trade),
                ),
            )

    def insert_portfolio_snapshot(self, snapshot: Dict[str, Any], run_id: str = "") -> None:
        created_at = snapshot.get("updated_at") or now_iso()
        with self._lock, self._connect() as conn:
            existing = conn.execute(
                """
                SELECT 1 FROM portfolio_snapshots
                WHERE run_id = ? AND created_at = ?
                LIMIT 1
                """,
                (run_id, created_at),
            ).fetchone()
            if existing:
                return
            conn.execute(
                """
                INSERT INTO portfolio_snapshots
                (run_id, created_at, cash, market_value, equity, realized_pnl,
                 unrealized_pnl, total_pnl, snapshot_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    created_at,
                    float(snapshot.get("cash", 0.0)),
                    float(snapshot.get("market_value", 0.0)),
                    float(snapshot.get("equity", 0.0)),
                    float(snapshot.get("realized_pnl", 0.0)),
                    float(snapshot.get("unrealized_pnl", 0.0)),
                    float(snapshot.get("total_pnl", 0.0)),
                    _json(snapshot),
                ),
            )

    def recent_runs(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_run(row) for row in rows]

    def run_detail(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                return None
            events = conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
            sections = conn.execute(
                "SELECT * FROM analysis_sections WHERE run_id = ?", (run_id,)
            ).fetchall()
        run = self._row_to_run(row)
        run["events"] = [
            {
                "seq": event["seq"],
                "run_id": event["run_id"],
                "type": event["type"],
                "created_at": event["created_at"],
                "payload": _loads(event["payload_json"], {}),
            }
            for event in events
        ]
        run["report_sections"] = {section["section"]: section["content"] for section in sections}
        return run

    def trades(self, limit: int = 250) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT trade_json FROM trades ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_loads(row["trade_json"], {}) for row in rows]

    def portfolio_history(self, limit: int = 500) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM portfolio_snapshots
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "cash": row["cash"],
                "market_value": row["market_value"],
                "equity": row["equity"],
                "realized_pnl": row["realized_pnl"],
                "unrealized_pnl": row["unrealized_pnl"],
                "total_pnl": row["total_pnl"],
                "snapshot": _loads(row["snapshot_json"], {}),
            }
            for row in reversed(rows)
        ]

    def create_daily_job(self, job_date: str, tickers: Iterable[str]) -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO daily_jobs
                (job_date, status, started_at, tickers_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (job_date, "running", now_iso(), _json(list(tickers)), now_iso()),
            )
            return int(cursor.lastrowid)

    def finish_daily_job(self, job_id: int, status: str, error: str = "") -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE daily_jobs
                SET status = ?, error = ?, ended_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, error or None, now_iso(), now_iso(), job_id),
            )

    def daily_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM daily_jobs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "job_date": row["job_date"],
                "status": row["status"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "tickers": _loads(row["tickers_json"], []),
                "error": row["error"],
            }
            for row in rows
        ]

    def replace_openai_costs(self, costs: List[Dict[str, Any]]) -> None:
        if not costs:
            return
        fetched_at = now_iso()
        with self._lock, self._connect() as conn:
            for cost in costs:
                conn.execute(
                    """
                    INSERT INTO openai_costs
                    (start_time, end_time, amount, currency, line_item, project_id, payload_json, fetched_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(cost.get("start_time", 0)),
                        int(cost.get("end_time", 0)),
                        float(cost.get("amount", 0.0)),
                        cost.get("currency", "usd"),
                        cost.get("line_item"),
                        cost.get("project_id"),
                        _json(cost),
                        fetched_at,
                    ),
                )

    def openai_costs(self, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM openai_costs ORDER BY start_time DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "start_time": row["start_time"],
                "end_time": row["end_time"],
                "amount": row["amount"],
                "currency": row["currency"],
                "line_item": row["line_item"],
                "project_id": row["project_id"],
                "payload": _loads(row["payload_json"], {}),
                "fetched_at": row["fetched_at"],
            }
            for row in rows
        ]

    def upsert_order(self, order: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO orders (
                    order_id, run_id, ticker, decision, action, status, quantity,
                    estimated_price, estimated_notional, projected_position_market_value,
                    mode, idempotency_key, reason, trade_date, source, created_at,
                    updated_at, order_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    status=excluded.status,
                    quantity=excluded.quantity,
                    estimated_price=excluded.estimated_price,
                    estimated_notional=excluded.estimated_notional,
                    projected_position_market_value=excluded.projected_position_market_value,
                    reason=excluded.reason,
                    updated_at=excluded.updated_at,
                    order_json=excluded.order_json
                """,
                (
                    order["order_id"],
                    order.get("run_id", ""),
                    order.get("ticker", ""),
                    order.get("decision", ""),
                    order.get("action", ""),
                    order.get("status", ""),
                    float(order.get("quantity", 0.0)),
                    float(order.get("estimated_price", 0.0)),
                    float(order.get("estimated_notional", 0.0)),
                    float(order.get("projected_position_market_value", 0.0)),
                    order.get("mode", ""),
                    order.get("idempotency_key", ""),
                    order.get("reason") or None,
                    order.get("trade_date", ""),
                    order.get("source", "agent"),
                    order.get("created_at") or now_iso(),
                    order.get("updated_at") or now_iso(),
                    _json(order),
                ),
            )

    def orders(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_order(row) for row in rows]

    def order_detail(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
        return self._row_to_order(row) if row else None

    def order_by_idempotency_key(self, idempotency_key: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return self._row_to_order(row) if row else None

    def count_orders_on_date(
        self,
        order_date: str,
        *,
        actions: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
    ) -> int:
        clauses = ["substr(created_at, 1, 10) = ?"]
        params: List[Any] = [order_date]
        if actions:
            clauses.append(f"action IN ({','.join('?' for _ in actions)})")
            params.extend(actions)
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        query = f"SELECT COUNT(*) AS count FROM orders WHERE {' AND '.join(clauses)}"
        with self._lock, self._connect() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row["count"] if row else 0)

    def portfolio_pnl_since(self, start_date: str) -> float:
        with self._lock, self._connect() as conn:
            first = conn.execute(
                """
                SELECT equity FROM portfolio_snapshots
                WHERE substr(created_at, 1, 10) >= ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (start_date,),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT equity FROM portfolio_snapshots
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
        if not first or not latest:
            return 0.0
        return float(latest["equity"]) - float(first["equity"])

    def insert_fill(self, fill: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO fills
                (fill_id, order_id, run_id, ticker, action, quantity, price, notional, created_at, fill_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fill["fill_id"],
                    fill.get("order_id", ""),
                    fill.get("run_id", ""),
                    fill.get("ticker", ""),
                    fill.get("action", ""),
                    float(fill.get("quantity", 0.0)),
                    float(fill.get("price", 0.0)),
                    float(fill.get("notional", 0.0)),
                    fill.get("created_at") or now_iso(),
                    _json(fill),
                ),
            )

    def fills(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT fill_json FROM fills ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_loads(row["fill_json"], {}) for row in rows]

    def insert_risk_decision(
        self, order_id: str, run_id: str, decision: Dict[str, Any]
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO risk_decisions
                (order_id, run_id, status, reason, checks_json, decision_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    run_id,
                    decision.get("status", ""),
                    decision.get("reason") or None,
                    _json(decision.get("checks", [])),
                    _json(decision),
                    now_iso(),
                ),
            )

    def risk_decisions(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM risk_decisions
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "order_id": row["order_id"],
                "run_id": row["run_id"],
                "status": row["status"],
                "reason": row["reason"],
                "checks": _loads(row["checks_json"], []),
                "decision": _loads(row["decision_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def insert_audit_event(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        run_id: Optional[str],
        payload: Dict[str, Any],
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO audit_events
                (event_type, entity_type, entity_id, run_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    entity_type,
                    entity_id,
                    run_id,
                    _json(payload),
                    now_iso(),
                ),
            )

    def audit_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM audit_events
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "event_type": row["event_type"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "run_id": row["run_id"],
                "payload": _loads(row["payload_json"], {}),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def upsert_backtest(self, result: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO backtests (
                    backtest_id, status, started_at, ended_at, config_json,
                    summary_json, result_json, error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(backtest_id) DO UPDATE SET
                    status=excluded.status,
                    ended_at=excluded.ended_at,
                    config_json=excluded.config_json,
                    summary_json=excluded.summary_json,
                    result_json=excluded.result_json,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    result["backtest_id"],
                    result.get("status", "unknown"),
                    result.get("started_at") or now_iso(),
                    result.get("ended_at"),
                    _json(result.get("config", {})),
                    _json(result.get("summary", {})),
                    _json(result),
                    result.get("error"),
                    now_iso(),
                ),
            )

    def backtests(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM backtests
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_backtest(row, include_result=False) for row in rows]

    def backtest_detail(self, backtest_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM backtests WHERE backtest_id = ?", (backtest_id,)
            ).fetchone()
        return self._row_to_backtest(row, include_result=True) if row else None

    def upsert_agent_replay_job(self, job: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agent_replay_jobs (
                    job_id, status, started_at, ended_at, config_json,
                    progress_json, result_json, error, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status=excluded.status,
                    ended_at=excluded.ended_at,
                    config_json=excluded.config_json,
                    progress_json=excluded.progress_json,
                    result_json=excluded.result_json,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    job["job_id"],
                    job.get("status", "unknown"),
                    job.get("started_at") or now_iso(),
                    job.get("ended_at"),
                    _json(job.get("config", {})),
                    _json(job.get("progress", {})),
                    _json(job.get("result", {})),
                    job.get("error"),
                    now_iso(),
                ),
            )

    def agent_replay_jobs(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM agent_replay_jobs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            self._row_to_agent_replay_job(row, include_result=False)
            for row in rows
        ]

    def agent_replay_job_detail(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_replay_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_agent_replay_job(row, include_result=True) if row else None

    def upsert_scanner_event(self, event: Dict[str, Any]) -> int:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scanner_events (
                    event_hash, source, region, title, summary, url, published_at,
                    language, event_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_hash) DO UPDATE SET
                    source=excluded.source,
                    region=excluded.region,
                    title=excluded.title,
                    summary=excluded.summary,
                    url=excluded.url,
                    published_at=excluded.published_at,
                    language=excluded.language,
                    event_json=excluded.event_json
                """,
                (
                    event["event_hash"],
                    event.get("source", ""),
                    event.get("region", "GLOBAL"),
                    event.get("title", ""),
                    event.get("summary", ""),
                    event.get("url"),
                    event.get("published_at") or now_iso(),
                    event.get("language", "en"),
                    _json(event),
                    event.get("created_at") or now_iso(),
                ),
            )
            row = conn.execute(
                "SELECT event_id FROM scanner_events WHERE event_hash = ?",
                (event["event_hash"],),
            ).fetchone()
        return int(row["event_id"])

    def scanner_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scanner_events
                ORDER BY published_at DESC, event_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_scanner_event(row) for row in rows]

    def scanner_event_detail(self, event_id: int) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scanner_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        return self._row_to_scanner_event(row) if row else None

    def upsert_scanner_signal(self, signal: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scanner_signals (
                    signal_id, event_id, event_hash, entity, region, category,
                    direction, score, confidence, reason, targets_json,
                    signal_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(signal_id) DO UPDATE SET
                    event_id=excluded.event_id,
                    entity=excluded.entity,
                    region=excluded.region,
                    category=excluded.category,
                    direction=excluded.direction,
                    score=excluded.score,
                    confidence=excluded.confidence,
                    reason=excluded.reason,
                    targets_json=excluded.targets_json,
                    signal_json=excluded.signal_json
                """,
                (
                    signal["signal_id"],
                    int(signal["event_id"]),
                    signal.get("event_hash", ""),
                    signal.get("entity", ""),
                    signal.get("region", "GLOBAL"),
                    signal.get("category", ""),
                    signal.get("direction", "watch"),
                    float(signal.get("score", 0.0)),
                    float(signal.get("confidence", 0.0)),
                    signal.get("reason", ""),
                    _json(signal.get("us_targets", [])),
                    _json(signal),
                    signal.get("created_at") or now_iso(),
                ),
            )

    def scanner_signals(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scanner_signals
                ORDER BY score DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_scanner_signal(row) for row in rows]

    def scanner_signal_detail(self, signal_id: str) -> Optional[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM scanner_signals WHERE signal_id = ?", (signal_id,)
            ).fetchone()
        return self._row_to_scanner_signal(row) if row else None

    def upsert_scanner_dislocation(self, row: Dict[str, Any]) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scanner_dislocations (
                    dislocation_id, signal_id, event_id, entity, reference_symbol,
                    target_symbol, reference_move_pct, target_move_pct, gap_pct,
                    z_score, spread_mean, spread_std, lookback_days, is_dislocated,
                    direction, dislocation_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dislocation_id) DO UPDATE SET
                    event_id=excluded.event_id,
                    entity=excluded.entity,
                    reference_symbol=excluded.reference_symbol,
                    target_symbol=excluded.target_symbol,
                    reference_move_pct=excluded.reference_move_pct,
                    target_move_pct=excluded.target_move_pct,
                    gap_pct=excluded.gap_pct,
                    z_score=excluded.z_score,
                    spread_mean=excluded.spread_mean,
                    spread_std=excluded.spread_std,
                    lookback_days=excluded.lookback_days,
                    is_dislocated=excluded.is_dislocated,
                    direction=excluded.direction,
                    dislocation_json=excluded.dislocation_json,
                    created_at=excluded.created_at
                """,
                (
                    row["dislocation_id"],
                    row["signal_id"],
                    int(row["event_id"]),
                    row.get("entity", ""),
                    row.get("reference_symbol", ""),
                    row.get("target_symbol", ""),
                    float(row.get("reference_move_pct", 0.0)),
                    float(row.get("target_move_pct", 0.0)),
                    float(row.get("gap_pct", 0.0)),
                    float(row.get("z_score", 0.0)),
                    float(row.get("spread_mean", 0.0)),
                    float(row.get("spread_std", 0.0)),
                    int(row.get("lookback_days", 0)),
                    1 if row.get("is_dislocated") else 0,
                    row.get("direction", ""),
                    _json(row),
                    row.get("created_at") or now_iso(),
                ),
            )

    def scanner_dislocations(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM scanner_dislocations
                ORDER BY is_dislocated DESC, ABS(z_score) DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_scanner_dislocation(row) for row in rows]

    def _row_to_run(self, row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "request": _loads(row["request_json"], {}),
            "stats": _loads(row["stats_json"], {}),
            "decision": row["decision"],
            "error": row["error"],
            "source": row["source"],
        }

    def _row_to_order(self, row: sqlite3.Row) -> Dict[str, Any]:
        order = _loads(row["order_json"], {})
        order.update(
            {
                "order_id": row["order_id"],
                "run_id": row["run_id"],
                "ticker": row["ticker"],
                "decision": row["decision"],
                "action": row["action"],
                "status": row["status"],
                "quantity": row["quantity"],
                "estimated_price": row["estimated_price"],
                "estimated_notional": row["estimated_notional"],
                "projected_position_market_value": row[
                    "projected_position_market_value"
                ],
                "mode": row["mode"],
                "idempotency_key": row["idempotency_key"],
                "reason": row["reason"] or "",
                "trade_date": row["trade_date"],
                "source": row["source"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
        return order

    def _row_to_backtest(
        self, row: sqlite3.Row, *, include_result: bool
    ) -> Dict[str, Any]:
        item = {
            "backtest_id": row["backtest_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "config": _loads(row["config_json"], {}),
            "summary": _loads(row["summary_json"], {}),
            "error": row["error"],
        }
        if include_result:
            item["result"] = _loads(row["result_json"], {})
        return item

    def _row_to_agent_replay_job(
        self, row: sqlite3.Row, *, include_result: bool
    ) -> Dict[str, Any]:
        item = {
            "job_id": row["job_id"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "config": _loads(row["config_json"], {}),
            "progress": _loads(row["progress_json"], {}),
            "error": row["error"],
        }
        if include_result:
            item["result"] = _loads(row["result_json"], {})
        return item

    def _row_to_scanner_event(self, row: sqlite3.Row) -> Dict[str, Any]:
        event = _loads(row["event_json"], {})
        event.update(
            {
                "event_id": row["event_id"],
                "event_hash": row["event_hash"],
                "source": row["source"],
                "region": row["region"],
                "title": row["title"],
                "summary": row["summary"],
                "url": row["url"],
                "published_at": row["published_at"],
                "language": row["language"],
                "created_at": row["created_at"],
            }
        )
        return event

    def _row_to_scanner_signal(self, row: sqlite3.Row) -> Dict[str, Any]:
        signal = _loads(row["signal_json"], {})
        signal.update(
            {
                "signal_id": row["signal_id"],
                "event_id": row["event_id"],
                "event_hash": row["event_hash"],
                "entity": row["entity"],
                "region": row["region"],
                "category": row["category"],
                "direction": row["direction"],
                "score": row["score"],
                "confidence": row["confidence"],
                "reason": row["reason"],
                "us_targets": _loads(row["targets_json"], []),
                "created_at": row["created_at"],
            }
        )
        return signal

    def _row_to_scanner_dislocation(self, row: sqlite3.Row) -> Dict[str, Any]:
        item = _loads(row["dislocation_json"], {})
        item.update(
            {
                "dislocation_id": row["dislocation_id"],
                "signal_id": row["signal_id"],
                "event_id": row["event_id"],
                "entity": row["entity"],
                "reference_symbol": row["reference_symbol"],
                "target_symbol": row["target_symbol"],
                "reference_move_pct": row["reference_move_pct"],
                "target_move_pct": row["target_move_pct"],
                "gap_pct": row["gap_pct"],
                "z_score": row["z_score"],
                "spread_mean": row["spread_mean"],
                "spread_std": row["spread_std"],
                "lookback_days": row["lookback_days"],
                "is_dislocated": bool(row["is_dislocated"]),
                "direction": row["direction"],
                "created_at": row["created_at"],
            }
        )
        return item

    def _analysis_dir(self, analysis_date: str, ticker: str, run_id: str) -> Path:
        safe_ticker = "".join(ch for ch in ticker.upper() if ch.isalnum() or ch in ".-_")
        return self.analyses_dir / analysis_date / safe_ticker / run_id
