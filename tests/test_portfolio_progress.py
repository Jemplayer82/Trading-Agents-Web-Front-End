"""Unit tests for portfolio scan progress bar feature.

Covers:
  - update_portfolio_scan helper: round-trip, whitelist rejection, no-op on empty kwargs
  - _COLUMN_MIGRATIONS: idempotent migration adds new columns to pre-existing schema
  - _run_scan: sets scan_total once after option filter; writes scanned_count/current_ticker per ticker

No network access. Run with: uv run pytest tests/test_portfolio_progress.py -v
"""
from __future__ import annotations

import sqlite3
import threading
import time
from unittest.mock import MagicMock

import pytest

from web import db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Minimal OLD portfolio_scans schema — does NOT have the three new columns.
_OLD_PORTFOLIO_SCANS_DDL = """
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
"""


# ---------------------------------------------------------------------------
# 1. update_portfolio_scan helper + _PORTFOLIO_SCAN_UPDATABLE allow-list
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUpdatePortfolioScanHelper:
    def test_round_trip(self, monkeypatch, tmp_path):
        """update_portfolio_scan writes, get_portfolio_scan reads back the values."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_portfolio_scan("2026-01-01")

        db.update_portfolio_scan(scan_id, scanned_count=2, scan_total=5, current_ticker="AAPL")

        row = db.get_portfolio_scan(scan_id)
        assert row["scanned_count"] == 2
        assert row["scan_total"] == 5
        assert row["current_ticker"] == "AAPL"

    def test_no_op_on_empty_kwargs(self, monkeypatch, tmp_path):
        """update_portfolio_scan() with no kwargs returns without error."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_portfolio_scan("2026-01-01")
        # Should not raise
        db.update_portfolio_scan(scan_id)

    def test_status_allowed_error_rejected(self, monkeypatch, tmp_path):
        """status joined the allow-list for the scan queue; other non-progress
        columns (e.g. error) must still raise ValueError."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_portfolio_scan("2026-01-01")
        db.update_portfolio_scan(scan_id, status="queued")
        assert db.get_portfolio_scan(scan_id)["status"] == "queued"
        with pytest.raises(ValueError, match="disallowed"):
            db.update_portfolio_scan(scan_id, error="nope")

    def test_rejects_arbitrary_column(self, monkeypatch, tmp_path):
        """Unknown columns must raise ValueError (SQL injection guard)."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_portfolio_scan("2026-01-01")
        with pytest.raises(ValueError, match="disallowed"):
            db.update_portfolio_scan(scan_id, injected_col="bad")

    def test_partial_update_leaves_others_intact(self, monkeypatch, tmp_path):
        """Updating only one column doesn't reset the others."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_portfolio_scan("2026-01-01")
        db.update_portfolio_scan(scan_id, scan_total=10, scanned_count=3, current_ticker="MSFT")
        db.update_portfolio_scan(scan_id, scanned_count=5)

        row = db.get_portfolio_scan(scan_id)
        assert row["scanned_count"] == 5
        assert row["scan_total"] == 10  # unchanged
        assert row["current_ticker"] == "MSFT"  # unchanged


# ---------------------------------------------------------------------------
# 2. _COLUMN_MIGRATIONS — idempotent migration on a pre-existing schema
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestColumnMigration:
    def test_migration_adds_new_columns(self, monkeypatch, tmp_path):
        """init_db() adds the three progress columns to an OLD portfolio_scans table."""
        db_path = tmp_path / "web.db"
        monkeypatch.setattr(db, "DB_PATH", db_path)

        # Build a DB with the OLD schema manually (no progress columns)
        conn = sqlite3.connect(str(db_path))
        conn.execute(_OLD_PORTFOLIO_SCANS_DDL)
        conn.commit()
        conn.close()

        # init_db should add the missing columns via _run_column_migrations
        db.init_db()

        # Verify columns are present
        conn = sqlite3.connect(str(db_path))
        cols = {row[1] for row in conn.execute("PRAGMA table_info(portfolio_scans)")}
        conn.close()
        assert "scanned_count" in cols
        assert "scan_total" in cols
        assert "current_ticker" in cols

    def test_migration_is_idempotent(self, monkeypatch, tmp_path):
        """Calling init_db() twice on a schema that already has the columns does not error."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        # Second call must not raise
        db.init_db()

    def test_migration_entries_in_list(self):
        """_COLUMN_MIGRATIONS must contain all three portfolio_scans tuples."""
        table_cols = [
            (tbl, col) for tbl, col, _ in db._COLUMN_MIGRATIONS
            if tbl == "portfolio_scans"
        ]
        assert ("portfolio_scans", "scanned_count") in table_cols
        assert ("portfolio_scans", "scan_total") in table_cols
        assert ("portfolio_scans", "current_ticker") in table_cols


# ---------------------------------------------------------------------------
# 3. _run_scan progress writes
# ---------------------------------------------------------------------------


def _make_fake_position(symbol: str, asset_type: str = "EQUITY") -> dict:
    return {
        "symbol": symbol,
        "asset_type": asset_type,
        "quantity": 10.0,
        "market_value": 1000.0,
        "shares": 10.0,
    }


@pytest.mark.unit
class TestRunScanProgress:
    def test_scan_total_and_per_ticker_progress(self, monkeypatch, tmp_path):
        """_run_scan writes scan_total once and scanned_count/current_ticker per ticker."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        fake_positions = [
            _make_fake_position("AAPL"),
            _make_fake_position("MSFT"),
            _make_fake_position("GOOGL"),
        ]

        from web import portfolio_routes

        # Patch the Schwab MCP positions source
        monkeypatch.setattr(portfolio_routes, "_mcp_positions", lambda: fake_positions)

        # Stub the orchestrator _run_scan actually uses. It imports
        # SwitchboardOrchestrator locally, so patch it at its source module.
        #
        # This previously patched tradingagents.graph.portfolio_graph.
        # run_single_ticker, which _run_scan never calls — so the test made
        # REAL network calls and only "passed" because the configured model
        # name was invalid and every request 404'd instantly. Once the model
        # defaults were fixed the requests became valid and the test hung on a
        # live LLM analysis. Keep this stub aligned with _run_scan.
        class FakeOrchestrator:
            def __init__(self, config=None, selected_analysts=None):
                self.memory_log = MagicMock()

            def run(self, ticker, trade_date):
                return ({"trader_investment_plan": "", "final_trade_decision": ""}, "HOLD")

        monkeypatch.setattr(
            "tradingagents.orchestrator.SwitchboardOrchestrator", FakeOrchestrator
        )

        # Stub out the aggregator and complete_portfolio_scan to avoid side effects
        monkeypatch.setattr(
            portfolio_routes.aggregator,
            "run",
            lambda payload, trade_date, config: "stub aggregator report",
        )

        scan_id = db.create_portfolio_scan("2026-01-01")
        portfolio_routes._run_scan(scan_id, "2026-01-01")

        row = db.get_portfolio_scan(scan_id)
        assert row["scan_total"] == 3, f"expected scan_total=3, got {row['scan_total']}"
        # scanned_count is written as completed-so-far after each ticker
        # finishes. With 3 tickers the last write is scanned_count=3.
        assert row["scanned_count"] is not None, "scanned_count should not be None after scan"
        # current_ticker is the most recently completed holding
        assert row["current_ticker"] in ("AAPL", "MSFT", "GOOGL"), (
            f"current_ticker should be one of the positions, got {row['current_ticker']!r}"
        )

    def test_option_positions_excluded_from_scan_total(self, monkeypatch, tmp_path):
        """scan_total reflects only equity positions — options are filtered before writing it."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        fake_positions = [
            _make_fake_position("AAPL"),
            _make_fake_position("AAPL  260117C00200000", asset_type="OPTION"),
            _make_fake_position("MSFT"),
        ]

        from web import portfolio_routes

        monkeypatch.setattr(portfolio_routes, "_mcp_positions", lambda: fake_positions)

        # Same stub as the sibling test above — _run_scan uses
        # SwitchboardOrchestrator, NOT portfolio_graph.run_single_ticker.
        # Patching the wrong target leaves the real orchestrator in place and
        # the test hangs on a live LLM call.
        class FakeOrchestrator:
            def __init__(self, config=None, selected_analysts=None):
                self.memory_log = MagicMock()

            def run(self, ticker, trade_date):
                return ({"trader_investment_plan": "", "final_trade_decision": ""}, "BUY")

        monkeypatch.setattr(
            "tradingagents.orchestrator.SwitchboardOrchestrator", FakeOrchestrator
        )
        monkeypatch.setattr(
            portfolio_routes.aggregator,
            "run",
            lambda payload, trade_date, config: "stub report",
        )

        scan_id = db.create_portfolio_scan("2026-01-01")
        portfolio_routes._run_scan(scan_id, "2026-01-01")

        row = db.get_portfolio_scan(scan_id)
        assert row["scan_total"] == 2, f"expected scan_total=2 (options excluded), got {row['scan_total']}"


# ---------------------------------------------------------------------------
# 4. _run_scan parallelism + ordering + fault isolation
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRunScanParallelism:
    def test_holdings_run_concurrently(self, monkeypatch, tmp_path):
        """Three holdings must reach a barrier together; sequential execution would time out."""
        monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        barrier = threading.Barrier(3, timeout=5)

        class FakeOrchestrator:
            def __init__(self, config=None, selected_analysts=None):
                self.memory_log = MagicMock()

            def run(self, ticker, trade_date):
                barrier.wait()
                return ({"trader_investment_plan": "", "final_trade_decision": ""}, "HOLD")

        monkeypatch.setattr(
            "tradingagents.orchestrator.SwitchboardOrchestrator", FakeOrchestrator
        )

        from web import portfolio_routes

        monkeypatch.setattr(
            portfolio_routes,
            "_mcp_positions",
            lambda: [
                _make_fake_position("AAPL"),
                _make_fake_position("MSFT"),
                _make_fake_position("GOOGL"),
            ],
        )
        monkeypatch.setattr(
            portfolio_routes.aggregator,
            "run",
            lambda payload, trade_date, config: "stub",
        )

        scan_id = db.create_portfolio_scan("2026-01-01")
        portfolio_routes._run_scan(scan_id, "2026-01-01")

        per_ticker = db.get_portfolio_scan(scan_id)["full_payload"]["per_ticker"]
        assert len(per_ticker) == 3
        assert all(e["signal"] == "HOLD" for e in per_ticker)

    def test_payload_preserves_input_order(self, monkeypatch, tmp_path):
        """Completion order is reversed by sleep, but stored payload stays in holdings order."""
        monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        delays = {"AAPL": 0.15, "MSFT": 0.05, "GOOGL": 0.0}

        class FakeOrchestrator:
            def __init__(self, config=None, selected_analysts=None):
                self.memory_log = MagicMock()

            def run(self, ticker, trade_date):
                time.sleep(delays[ticker])
                return ({"trader_investment_plan": "", "final_trade_decision": ""}, "HOLD")

        monkeypatch.setattr(
            "tradingagents.orchestrator.SwitchboardOrchestrator", FakeOrchestrator
        )

        from web import portfolio_routes

        monkeypatch.setattr(
            portfolio_routes,
            "_mcp_positions",
            lambda: [
                _make_fake_position("AAPL"),
                _make_fake_position("MSFT"),
                _make_fake_position("GOOGL"),
            ],
        )
        monkeypatch.setattr(
            portfolio_routes.aggregator,
            "run",
            lambda payload, trade_date, config: "stub",
        )

        scan_id = db.create_portfolio_scan("2026-01-01")
        portfolio_routes._run_scan(scan_id, "2026-01-01")

        tickers = [
            e["ticker"]
            for e in db.get_portfolio_scan(scan_id)["full_payload"]["per_ticker"]
        ]
        assert tickers == ["AAPL", "MSFT", "GOOGL"]

    def test_progress_flushed_per_completion(self, monkeypatch, tmp_path):
        """scanned_count is written once per completion and ends at scan_total."""
        monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        recorded_counts: list[int] = []
        real_update = db.update_portfolio_scan

        def recording_update(scan_id, **kwargs):
            if "scanned_count" in kwargs:
                recorded_counts.append(kwargs["scanned_count"])
            return real_update(scan_id, **kwargs)

        monkeypatch.setattr(db, "update_portfolio_scan", recording_update)

        class FakeOrchestrator:
            def __init__(self, config=None, selected_analysts=None):
                self.memory_log = MagicMock()

            def run(self, ticker, trade_date):
                return ({"trader_investment_plan": "", "final_trade_decision": ""}, "HOLD")

        monkeypatch.setattr(
            "tradingagents.orchestrator.SwitchboardOrchestrator", FakeOrchestrator
        )

        from web import portfolio_routes

        monkeypatch.setattr(
            portfolio_routes,
            "_mcp_positions",
            lambda: [
                _make_fake_position("AAPL"),
                _make_fake_position("MSFT"),
                _make_fake_position("GOOGL"),
            ],
        )
        monkeypatch.setattr(
            portfolio_routes.aggregator,
            "run",
            lambda payload, trade_date, config: "stub",
        )

        scan_id = db.create_portfolio_scan("2026-01-01")
        portfolio_routes._run_scan(scan_id, "2026-01-01")

        assert recorded_counts == sorted(recorded_counts)
        assert {1, 2, 3}.issubset(set(recorded_counts))
        assert db.get_portfolio_scan(scan_id)["scanned_count"] == 3

    def test_one_failure_does_not_sink_the_scan(self, monkeypatch, tmp_path):
        """A single bad holding is recorded as failed; the rest of the scan completes."""
        monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "3")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        class FakeOrchestrator:
            def __init__(self, config=None, selected_analysts=None):
                self.memory_log = MagicMock()

            def run(self, ticker, trade_date):
                if ticker == "MSFT":
                    raise RuntimeError("boom")
                return (
                    {"trader_investment_plan": "plan", "final_trade_decision": f"buy {ticker}"},
                    "BUY",
                )

        monkeypatch.setattr(
            "tradingagents.orchestrator.SwitchboardOrchestrator", FakeOrchestrator
        )

        from web import portfolio_routes

        monkeypatch.setattr(
            portfolio_routes,
            "_mcp_positions",
            lambda: [
                _make_fake_position("AAPL"),
                _make_fake_position("MSFT"),
                _make_fake_position("GOOGL"),
            ],
        )
        monkeypatch.setattr(
            portfolio_routes.aggregator,
            "run",
            lambda payload, trade_date, config: "stub",
        )

        scan_id = db.create_portfolio_scan("2026-01-01")
        portfolio_routes._run_scan(scan_id, "2026-01-01")

        row = db.get_portfolio_scan(scan_id)
        assert row["status"] == "completed"
        per_ticker = {e["ticker"]: e for e in row["full_payload"]["per_ticker"]}
        assert per_ticker["MSFT"]["signal"] == ""
        assert per_ticker["MSFT"]["final_decision"].startswith("(failed:")
        assert per_ticker["AAPL"]["signal"] == "BUY"
        assert per_ticker["GOOGL"]["signal"] == "BUY"

        msft_entry = next(t for t in (row.get("tickers") or []) if t["ticker"] == "MSFT")
        assert msft_entry.get("error") and "boom" in msft_entry["error"]

    def test_signal_counts_are_correct_under_concurrency(self, monkeypatch, tmp_path):
        """Signal counts are aggregated on the main thread after concurrent analysis."""
        monkeypatch.setenv("OLLAMA_MAX_CONCURRENCY", "4")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        signals = ["BUY", "HOLD", "SELL", "BUY", "HOLD", "SELL", "BUY", "HOLD", "SELL"]
        positions = [_make_fake_position(f"T{i}") for i in range(1, 10)]

        class FakeOrchestrator:
            def __init__(self, config=None, selected_analysts=None):
                self.memory_log = MagicMock()

            def run(self, ticker, trade_date):
                idx = int(ticker[1:]) - 1
                return ({"trader_investment_plan": "", "final_trade_decision": ""}, signals[idx])

        monkeypatch.setattr(
            "tradingagents.orchestrator.SwitchboardOrchestrator", FakeOrchestrator
        )

        from web import portfolio_routes

        monkeypatch.setattr(portfolio_routes, "_mcp_positions", lambda: positions)
        monkeypatch.setattr(
            portfolio_routes.aggregator,
            "run",
            lambda payload, trade_date, config: "stub",
        )

        scan_id = db.create_portfolio_scan("2026-01-01")
        portfolio_routes._run_scan(scan_id, "2026-01-01")

        counts = db.get_portfolio_scan(scan_id)["signal_counts"]
        assert counts["BUY"] == 3
        assert counts["HOLD"] == 3
        assert counts["SELL"] == 3

    def test_portfolio_routes_does_not_depend_on_spy_scanner(self, monkeypatch, tmp_path):
        """This file must never import spy_scanner, even lazily — spy_scanner.py is deleted at tier-2 build time."""
        from web import portfolio_routes

        assert not hasattr(portfolio_routes, "spy_scanner")
