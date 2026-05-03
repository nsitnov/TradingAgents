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

    def _analysis_dir(self, analysis_date: str, ticker: str, run_id: str) -> Path:
        safe_ticker = "".join(ch for ch in ticker.upper() if ch.isalnum() or ch in ".-_")
        return self.analyses_dir / analysis_date / safe_ticker / run_id
