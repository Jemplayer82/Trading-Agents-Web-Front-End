"""SQLite persistence for the web service.

Stores user preferences (single row) and analysis history (one row per run).
DB file lives in the shared volume so both web restarts and CLI runs see the
same `~/.tradingagents/` tree.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

_HOME = Path(os.path.expanduser("~")) / ".tradingagents"
DB_PATH = Path(os.environ.get("TRADINGAGENTS_WEB_DB", str(_HOME / "web.db")))


SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    ticker TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    deep_model TEXT,
    quick_model TEXT,
    analysts TEXT,
    research_depth INTEGER,
    language TEXT,
    final_decision TEXT,
    processed_signal TEXT,
    market_report TEXT,
    sentiment_report TEXT,
    news_report TEXT,
    fundamentals_report TEXT,
    investment_plan TEXT,
    trader_plan TEXT,
    risk_judge TEXT,
    full_state TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_analyses_created_at ON analyses (created_at DESC);
"""


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def get_preferences() -> dict[str, Any]:
    with connect() as conn:
        row = conn.execute("SELECT data FROM preferences WHERE id = 1").fetchone()
    if not row:
        return {}
    return json.loads(row["data"])


def save_preferences(data: dict[str, Any]) -> None:
    payload = json.dumps(data)
    with connect() as conn:
        conn.execute(
            "INSERT INTO preferences (id, data) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (payload,),
        )


def create_analysis(params: dict[str, Any]) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO analyses (
                created_at, ticker, trade_date, status,
                provider, deep_model, quick_model, analysts, research_depth, language
            ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(timespec="seconds") + "Z",
                params["ticker"],
                params["trade_date"],
                params.get("provider"),
                params.get("deep_model"),
                params.get("quick_model"),
                json.dumps(params.get("analysts", [])),
                params.get("research_depth"),
                params.get("language"),
            ),
        )
        return int(cur.lastrowid)


def complete_analysis(analysis_id: int, final_state: dict[str, Any], processed_signal: str) -> None:
    def _get(key: str) -> str:
        val = final_state.get(key, "")
        return val if isinstance(val, str) else json.dumps(val)

    risk = final_state.get("risk_debate_state") or {}
    risk_judge = risk.get("judge_decision", "") if isinstance(risk, dict) else ""

    with connect() as conn:
        conn.execute(
            """
            UPDATE analyses SET
                status = 'completed',
                final_decision = ?,
                processed_signal = ?,
                market_report = ?,
                sentiment_report = ?,
                news_report = ?,
                fundamentals_report = ?,
                investment_plan = ?,
                trader_plan = ?,
                risk_judge = ?,
                full_state = ?
            WHERE id = ?
            """,
            (
                _get("final_trade_decision"),
                processed_signal,
                _get("market_report"),
                _get("sentiment_report"),
                _get("news_report"),
                _get("fundamentals_report"),
                _get("investment_plan"),
                _get("trader_investment_plan"),
                risk_judge,
                json.dumps(_serialize(final_state)),
                analysis_id,
            ),
        )


def fail_analysis(analysis_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE analyses SET status = 'failed', error = ? WHERE id = ?",
            (error, analysis_id),
        )


def list_analyses(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, ticker, trade_date, status,
                   provider, deep_model, quick_model, final_decision, processed_signal
            FROM analyses ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_analysis(analysis_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM analyses WHERE id = ?", (analysis_id,)
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    for key in ("analysts", "full_state"):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except (TypeError, ValueError):
                pass
    return data


def _serialize(obj: Any) -> Any:
    """Make LangChain messages and other non-JSON-native objects serializable."""
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(v) for v in obj]
    if hasattr(obj, "model_dump"):
        try:
            return _serialize(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return _serialize(obj.dict())
        except Exception:
            pass
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)
