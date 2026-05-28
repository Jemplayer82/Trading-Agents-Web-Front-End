"""SQLite persistence for the web service.

Tables:
  preferences        - single row of user form defaults
  analyses           - one row per single-ticker analysis (existing)
  portfolio_scans    - one row per nightly portfolio sweep (Schwab)
  portfolio_tickers  - join row connecting a scan to the analyses it generated
  spy_scans          - one row per weekly S&P 500 scanner run
  spy_quick_results  - one row per ticker per SPY scan (quick + deep results)
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

CREATE TABLE IF NOT EXISTS portfolio_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    num_tickers INTEGER DEFAULT 0,
    signal_counts TEXT,
    aggregator_report TEXT,
    full_payload TEXT,
    error TEXT,
    newsletter_sent_at TEXT,
    newsletter_message_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_portfolio_scans_created_at ON portfolio_scans (created_at DESC);

CREATE TABLE IF NOT EXISTS spy_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    status TEXT NOT NULL,
    quick_count INTEGER DEFAULT 0,
    quick_total INTEGER DEFAULT 0,
    deep_count INTEGER DEFAULT 0,
    deep_total INTEGER DEFAULT 50,
    allocator_report TEXT,
    portfolio_json TEXT,
    current_value REAL,
    last_price_check TEXT,
    rebalance_notes TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_spy_scans_created_at ON spy_scans (created_at DESC);

CREATE TABLE IF NOT EXISTS spy_quick_results (
    scan_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    signal TEXT,
    conviction INTEGER,
    reasoning TEXT,
    analysis_id INTEGER,
    error TEXT,
    PRIMARY KEY (scan_id, ticker),
    FOREIGN KEY (scan_id) REFERENCES spy_scans (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS portfolio_tickers (
    scan_id INTEGER NOT NULL,
    ticker TEXT NOT NULL,
    analysis_id INTEGER,
    quantity REAL,
    market_value REAL,
    signal TEXT,
    error TEXT,
    PRIMARY KEY (scan_id, ticker),
    FOREIGN KEY (scan_id) REFERENCES portfolio_scans (id) ON DELETE CASCADE
);
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
    conn.execute("PRAGMA foreign_keys=ON")
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


# ---------- single-ticker analyses (unchanged behavior) ----------

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


def delete_analysis(analysis_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
        return cur.rowcount > 0


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
        row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
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


# ---------- portfolio scans (new) ----------

def create_portfolio_scan(trade_date: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO portfolio_scans (created_at, trade_date, status) VALUES (?, ?, 'running')",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z", trade_date),
        )
        return int(cur.lastrowid)


def add_scan_ticker(
    scan_id: int,
    ticker: str,
    analysis_id: int | None,
    quantity: float,
    market_value: float,
    signal: str | None,
    error: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_tickers (scan_id, ticker, analysis_id, quantity, market_value, signal, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id, ticker) DO UPDATE SET
                analysis_id = excluded.analysis_id,
                quantity = excluded.quantity,
                market_value = excluded.market_value,
                signal = excluded.signal,
                error = excluded.error
            """,
            (scan_id, ticker, analysis_id, quantity, market_value, signal, error),
        )


def complete_portfolio_scan(
    scan_id: int,
    aggregator_report: str,
    signal_counts: dict[str, int],
    num_tickers: int,
    full_payload: dict[str, Any] | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE portfolio_scans SET
                status = 'completed',
                num_tickers = ?,
                signal_counts = ?,
                aggregator_report = ?,
                full_payload = ?
            WHERE id = ?
            """,
            (
                num_tickers,
                json.dumps(signal_counts),
                aggregator_report,
                json.dumps(_serialize(full_payload or {})),
                scan_id,
            ),
        )


def fail_portfolio_scan(scan_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE portfolio_scans SET status = 'failed', error = ? WHERE id = ?",
            (error, scan_id),
        )


def mark_newsletter_sent(scan_id: int, message_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE portfolio_scans SET newsletter_sent_at = ?, newsletter_message_id = ? WHERE id = ?",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z", message_id, scan_id),
        )


def list_portfolio_scans(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, trade_date, status, num_tickers, signal_counts,
                   newsletter_sent_at
            FROM portfolio_scans ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("signal_counts"):
            try:
                d["signal_counts"] = json.loads(d["signal_counts"])
            except (TypeError, ValueError):
                pass
        out.append(d)
    return out


def get_portfolio_scan(scan_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM portfolio_scans WHERE id = ?", (scan_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        tickers = conn.execute(
            "SELECT * FROM portfolio_tickers WHERE scan_id = ? ORDER BY ticker",
            (scan_id,),
        ).fetchall()
    for key in ("signal_counts", "full_payload"):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except (TypeError, ValueError):
                pass
    data["tickers"] = [dict(t) for t in tickers]
    return data


def latest_portfolio_scan() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM portfolio_scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return get_portfolio_scan(int(row["id"]))


# ---------- S&P 500 scanner ----------

def create_spy_scan(trade_date: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO spy_scans (created_at, trade_date, status) VALUES (?, ?, 'pending')",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z", trade_date),
        )
        return int(cur.lastrowid)


def update_spy_scan(scan_id: int, **kwargs: Any) -> None:
    if not kwargs:
        return
    sets = ", ".join(k + " = ?" for k in kwargs)
    vals = list(kwargs.values()) + [scan_id]
    with connect() as conn:
        conn.execute("UPDATE spy_scans SET " + sets + " WHERE id = ?", vals)


def complete_spy_scan(
    scan_id: int,
    allocator_report: str,
    portfolio_json: list[dict[str, Any]],
) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE spy_scans SET status = 'completed', allocator_report = ?, portfolio_json = ? WHERE id = ?",
            (allocator_report, json.dumps(_serialize(portfolio_json)), scan_id),
        )


def fail_spy_scan(scan_id: int, error: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE spy_scans SET status = 'failed', error = ? WHERE id = ?",
            (error, scan_id),
        )


def update_spy_scan_prices(scan_id: int, current_value: float, rebalance_notes: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE spy_scans SET current_value = ?, last_price_check = ?, rebalance_notes = ? WHERE id = ?",
            (current_value, datetime.utcnow().isoformat(timespec="seconds") + "Z", rebalance_notes, scan_id),
        )


def upsert_spy_quick_result(
    scan_id: int,
    ticker: str,
    signal: str | None = None,
    conviction: int | None = None,
    reasoning: str | None = None,
    analysis_id: int | None = None,
    error: str | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO spy_quick_results (scan_id, ticker, signal, conviction, reasoning, analysis_id, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scan_id, ticker) DO UPDATE SET
                signal = COALESCE(excluded.signal, signal),
                conviction = COALESCE(excluded.conviction, conviction),
                reasoning = COALESCE(excluded.reasoning, reasoning),
                analysis_id = COALESCE(excluded.analysis_id, analysis_id),
                error = COALESCE(excluded.error, error)
            """,
            (scan_id, ticker, signal, conviction, reasoning, analysis_id, error),
        )


def list_spy_scans(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, trade_date, status, quick_count, quick_total,
                   deep_count, deep_total, current_value, last_price_check
            FROM spy_scans ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_spy_scan(scan_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM spy_scans WHERE id = ?", (scan_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        results = conn.execute(
            "SELECT * FROM spy_quick_results WHERE scan_id = ? ORDER BY conviction DESC, ticker",
            (scan_id,),
        ).fetchall()
    if data.get("portfolio_json"):
        try:
            data["portfolio_json"] = json.loads(data["portfolio_json"])
        except (TypeError, ValueError):
            pass
    data["quick_results"] = [dict(r) for r in results]
    return data


def get_spy_quick_result(scan_id: int, ticker: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM spy_quick_results WHERE scan_id = ? AND ticker = ?",
            (scan_id, ticker),
        ).fetchone()
    return dict(row) if row else None


def latest_spy_scan() -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM spy_scans ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return get_spy_scan(int(row["id"]))


def _serialize(obj: Any) -> Any:
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
