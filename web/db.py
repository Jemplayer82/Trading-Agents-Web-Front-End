"""SQLite persistence for the web service.

Tables:
  preferences          - single row of user form defaults
  provider_credentials - per-LLM-provider API key + optional base URL
  app_settings         - env-style key/value config managed from the UI
                         (Schwab app key/secret/callback, Ollama base URL, etc.)
  users                - dashboard login accounts (username + pbkdf2 hash)
  sessions             - active login sessions (cookie token -> username)
  analyses             - one row per single-ticker analysis (existing)
  portfolio_scans      - one row per nightly portfolio sweep (Schwab)
  portfolio_tickers    - join row connecting a scan to the analyses it generated
  spy_scans            - one row per weekly S&P 500 scanner run
  spy_quick_results    - one row per ticker per SPY scan (quick + deep results)
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

_HOME = Path(os.path.expanduser("~")) / ".tradingagents"
DB_PATH = Path(os.environ.get("TRADINGAGENTS_WEB_DB", str(_HOME / "web.db")))


SCHEMA = """
CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_credentials (
    provider TEXT PRIMARY KEY,
    api_key TEXT NOT NULL,
    base_url TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions (expires_at);

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
    cancel_requested INTEGER DEFAULT 0,
    previous_scan_id INTEGER,
    starting_value REAL,
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

-- Cross-container LLM concurrency registry. The api container inserts a row
-- per in-flight single-ticker analysis; the portfolio scanner reads the live
-- count to dynamically size its worker pool (single-ticker gets priority).
CREATE TABLE IF NOT EXISTS llm_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    label TEXT,
    started_at TEXT NOT NULL,
    heartbeat TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_activity_kind ON llm_activity (kind, heartbeat);

-- Cached company name + website per ticker (resolved via yfinance on demand).
CREATE TABLE IF NOT EXISTS ticker_info (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    website TEXT,
    fetched_at TEXT NOT NULL
);
"""


# Lightweight column migrations for tables that predate a new column.
# (init_db only runs CREATE TABLE IF NOT EXISTS, which never alters an
# existing table — so additive columns need an explicit guarded ALTER.)
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("spy_scans", "cancel_requested", "INTEGER DEFAULT 0"),
    ("spy_scans", "previous_scan_id", "INTEGER"),
    ("spy_scans", "starting_value", "REAL"),
]


def _run_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, decl in _COLUMN_MIGRATIONS:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        _run_column_migrations(conn)


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


# ---------- provider credentials (LLM API keys) ----------

def set_credential(provider: str, api_key: str, base_url: str | None = None) -> None:
    """Insert-or-update an API key (and optional base URL) for a provider."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO provider_credentials (provider, api_key, base_url, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(provider) DO UPDATE SET "
            "api_key = excluded.api_key, base_url = excluded.base_url, updated_at = excluded.updated_at",
            (provider, api_key, base_url, datetime.utcnow().isoformat()),
        )


def get_credential(provider: str) -> dict[str, Any] | None:
    """Return the full credential row for `provider`, or None if not set."""
    with connect() as conn:
        row = conn.execute(
            "SELECT provider, api_key, base_url, updated_at FROM provider_credentials WHERE provider = ?",
            (provider,),
        ).fetchone()
    return dict(row) if row else None


def list_credentials() -> list[dict[str, Any]]:
    """Return all stored credential rows (api_key included — caller must mask before returning to client)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT provider, api_key, base_url, updated_at FROM provider_credentials"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_credential(provider: str) -> bool:
    """Remove a provider's credential. Returns True if a row was deleted."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM provider_credentials WHERE provider = ?", (provider,))
    return cur.rowcount > 0


# ---------- app settings (Schwab, Ollama, etc. — env-style config) ----------

def set_app_setting(key: str, value: str) -> None:
    """Insert-or-update a UI-managed env-style setting."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, datetime.utcnow().isoformat()),
        )


def get_app_setting(key: str) -> str | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def list_app_settings() -> list[dict[str, Any]]:
    """All stored settings — caller must mask before returning to client."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT key, value, updated_at FROM app_settings"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_app_setting(key: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    return cur.rowcount > 0


# ---------- auth: users + sessions ----------

def count_users() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return int(row["n"])


def create_user(username: str, password_hash: str) -> None:
    """Insert a new user. Raises sqlite3.IntegrityError if username exists."""
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, datetime.utcnow().isoformat()),
        )


def get_user(username: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT username, password_hash, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT username, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def set_user_password(username: str, password_hash: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (password_hash, username),
        )
    return cur.rowcount > 0


def delete_user(username: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
    return cur.rowcount > 0


def create_session(token: str, username: str, expires_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, username, datetime.utcnow().isoformat(), expires_at),
        )


def get_session(token: str) -> dict[str, Any] | None:
    """Return a non-expired session row, or None. Expired rows are ignored."""
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT token, username, created_at, expires_at FROM sessions "
            "WHERE token = ? AND expires_at > ?",
            (token, now),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    return cur.rowcount > 0


def purge_expired_sessions() -> int:
    now = datetime.utcnow().isoformat()
    with connect() as conn:
        cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
    return cur.rowcount


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
            "INSERT INTO spy_scans (created_at, trade_date, status, cancel_requested) VALUES (?, ?, 'pending', 0)",
            (datetime.utcnow().isoformat(timespec="seconds") + "Z", trade_date),
        )
        return int(cur.lastrowid)


def request_spy_scan_cancel(scan_id: int) -> None:
    """Flag a running scan for cooperative cancellation."""
    with connect() as conn:
        conn.execute("UPDATE spy_scans SET cancel_requested = 1 WHERE id = ?", (scan_id,))


def is_spy_scan_cancelled(scan_id: int) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT cancel_requested FROM spy_scans WHERE id = ?", (scan_id,)
        ).fetchone()
    return bool(row and row["cancel_requested"])


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
    previous_scan_id: int | None = None,
    starting_value: float | None = None,
) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE spy_scans
               SET status = 'completed', allocator_report = ?, portfolio_json = ?,
                   previous_scan_id = ?, starting_value = ?
               WHERE id = ?""",
            (allocator_report, json.dumps(_serialize(portfolio_json)),
             previous_scan_id, starting_value, scan_id),
        )


def get_latest_completed_spy_scan(exclude_id: int | None = None) -> dict[str, Any] | None:
    """Return the most recent completed scan, optionally excluding one scan ID."""
    with connect() as conn:
        if exclude_id is not None:
            row = conn.execute(
                "SELECT id FROM spy_scans WHERE status = 'completed' AND id != ? ORDER BY id DESC LIMIT 1",
                (exclude_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM spy_scans WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
    if not row:
        return None
    return get_spy_scan(int(row["id"]))


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


def delete_spy_scan(scan_id: int) -> bool:
    """Delete a scan and its quick results (FK ON DELETE CASCADE).

    Deep-dive `analyses` rows the scan created are intentionally kept — they
    remain accessible from the Run Analysis history.
    """
    with connect() as conn:
        cur = conn.execute("DELETE FROM spy_scans WHERE id = ?", (scan_id,))
        return cur.rowcount > 0


# ---------- cross-container LLM concurrency registry ----------

def register_activity(kind: str, label: str | None = None) -> int:
    """Record an in-flight LLM consumer (e.g. a single-ticker analysis).

    Returns the row id; pass it to heartbeat_activity()/clear_activity().
    """
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO llm_activity (kind, label, started_at, heartbeat) VALUES (?, ?, ?, ?)",
            (kind, label, now, now),
        )
        return int(cur.lastrowid)


def heartbeat_activity(activity_id: int) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        conn.execute("UPDATE llm_activity SET heartbeat = ? WHERE id = ?", (now, activity_id))


def clear_activity(activity_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM llm_activity WHERE id = ?", (activity_id,))


def count_active_single(stale_seconds: int = 120) -> int:
    """Count fresh single-ticker analyses currently consuming LLM slots.

    Rows whose heartbeat is older than stale_seconds are ignored (and pruned)
    so a crashed api container doesn't permanently starve the scanner.
    """
    cutoff = (
        datetime.utcnow() - timedelta(seconds=stale_seconds)
    ).isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_activity WHERE kind = 'single' AND heartbeat >= ?",
            (cutoff,),
        ).fetchone()
    return int(row["n"]) if row else 0


def purge_stale_activity(stale_seconds: int = 120) -> None:
    cutoff = (
        datetime.utcnow() - timedelta(seconds=stale_seconds)
    ).isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        conn.execute("DELETE FROM llm_activity WHERE heartbeat < ?", (cutoff,))


# ---------- ticker company-info cache ----------

def get_ticker_info(ticker: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT ticker, name, website FROM ticker_info WHERE ticker = ?", (ticker,)
        ).fetchone()
    return dict(row) if row else None


def set_ticker_info(ticker: str, name: str | None, website: str | None) -> None:
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO ticker_info (ticker, name, website, fetched_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                name = excluded.name,
                website = excluded.website,
                fetched_at = excluded.fetched_at
            """,
            (ticker, name, website, now),
        )


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
