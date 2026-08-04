"""Unit tests for the /status polling fix (the confirmed ~38 MiB/hr leak).

GET /api/spy-scans/{id} (and the options equivalent) return the entire scan —
up to ~500 spy_quick_results rows — and were polled every 5s for a running
scan's full duration. get_spy_scan_status is the O(1) replacement the 5s
poller now hits instead; the full endpoint is only re-fetched when something
in the cheap row actually changes.

Covers:
  - get_spy_scan_status: payload stays small regardless of row count, returns
    None for a missing scan, includes kind (needed by the options 404 guard)
  - upsert_spy_quick_result: reasoning/error get truncated at write time
  - _is_any_scan_running: running_wait_market counts as busy (a scan sitting
    in the daily market-open wait must not let a second scan start concurrently)
  - options_engine's running_wait_market / running_alloc status split around
    _wait_for_market_open

No network access. Run with: uv run pytest tests/test_spy_scan_status_endpoint.py -v
"""
from __future__ import annotations

import json

import pytest

from web import db


@pytest.mark.unit
class TestGetSpyScanStatus:
    def test_payload_size_independent_of_row_count(self, monkeypatch, tmp_path):
        """The whole point of this endpoint: O(1) regardless of quick_results size."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")

        for i in range(500):
            db.upsert_spy_quick_result(
                scan_id=scan_id,
                ticker=f"T{i}",
                signal="BUY",
                conviction=7,
                reasoning="x" * 500,  # near the truncation cap
            )
        db.update_spy_scan(scan_id, quick_count=500, quick_total=500)

        status = db.get_spy_scan_status(scan_id)
        payload_bytes = len(json.dumps(status).encode("utf-8"))

        # The full endpoint with 500 such rows would be hundreds of KB; this
        # must stay tiny no matter how large quick_results grows.
        assert payload_bytes < 1024, (
            f"status payload was {payload_bytes} bytes — get_spy_scan_status "
            "must not scale with row count"
        )
        assert "quick_results" not in status, "status must not include the heavy join"
        assert status["quick_count"] == 500

    def test_missing_scan_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        assert db.get_spy_scan_status(999) is None

    def test_includes_kind_for_options_guard(self, monkeypatch, tmp_path):
        """The /api/options-scans/{id}/status route 404s when kind != 'options' —
        it needs kind in this row to enforce that, same as get_options_scan does."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        equity_id = db.create_spy_scan("2026-01-01", kind="equity")
        options_id = db.create_spy_scan("2026-01-01", kind="options")

        assert db.get_spy_scan_status(equity_id)["kind"] == "equity"
        assert db.get_spy_scan_status(options_id)["kind"] == "options"

    def test_reflects_live_progress_fields(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")
        db.update_spy_scan(scan_id, status="running_deep", quick_count=150,
                           quick_total=150, deep_count=3, deep_total=50)

        status = db.get_spy_scan_status(scan_id)
        assert status["status"] == "running_deep"
        assert status["quick_count"] == 150
        assert status["deep_count"] == 3
        assert status["cancel_requested"] == 0


@pytest.mark.unit
class TestQuickResultTruncation:
    def test_reasoning_and_error_truncated_at_write(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")

        db.upsert_spy_quick_result(
            scan_id=scan_id, ticker="AAPL",
            reasoning="x" * 5000, error="y" * 5000,
        )

        row = db.get_spy_quick_result(scan_id, "AAPL")
        assert len(row["reasoning"]) == db._QUICK_RESULT_TEXT_MAX
        assert len(row["error"]) == db._QUICK_RESULT_TEXT_MAX

    def test_short_text_unaffected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")

        db.upsert_spy_quick_result(scan_id=scan_id, ticker="AAPL", reasoning="short")
        row = db.get_spy_quick_result(scan_id, "AAPL")
        assert row["reasoning"] == "short"

    def test_none_stays_none(self, monkeypatch, tmp_path):
        """A partial upsert (e.g. the deep pass adding only analysis_id) must not
        crash trying to slice None."""
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01")

        db.upsert_spy_quick_result(scan_id=scan_id, ticker="AAPL", signal="BUY")
        row = db.get_spy_quick_result(scan_id, "AAPL")
        assert row["reasoning"] is None
        assert row["error"] is None


@pytest.mark.unit
class TestIsAnyScanRunningCoversWaitMarket:
    """A scan sitting in running_wait_market (up to ~2h/day, doing nothing but
    waiting for 09:35 ET) must still count as 'busy' — otherwise the queue-busy
    check would think nothing is running and let a second scan start
    concurrently during the wait."""

    def test_wait_market_status_counts_as_running(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01", kind="options")
        db.update_spy_scan(scan_id, status="running_wait_market")

        from web import scan_queue
        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)
        assert busy is not None, "running_wait_market must be treated as busy"
        assert busy["id"] == scan_id

    def test_terminal_statuses_do_not_count_as_running(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01", kind="options", status="completed")

        from web import scan_queue
        with db.connect() as conn:
            busy = scan_queue._is_any_scan_running(conn)
        assert busy is None
        assert scan_id  # keep the id referenced; the assertion is on `busy`


@pytest.mark.unit
class TestMarketWaitStatusSplit:
    """_wait_for_market_open should label itself running_wait_market — distinct
    from running_alloc, which now means real vetting/allocation work — and
    run_options_build should flip back to running_alloc the moment the wait
    actually ends."""

    def test_wait_for_market_open_sets_wait_status_before_open(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01", kind="options")

        from datetime import datetime
        from web import options_engine

        # A Monday well before market open — the loop must write the wait
        # status at least once, then we cut it short via cancellation.
        calls = {"n": 0}

        def fake_now_et():
            calls["n"] += 1
            if calls["n"] > 2:
                # Let the loop exit after a couple of iterations by reporting
                # a cancelled scan next tick.
                db.request_spy_scan_cancel(scan_id)
            return datetime(2026, 6, 1, 7, 30)  # Monday, before MARKET_OPEN_ET

        monkeypatch.setattr(options_engine.options_data, "now_et", fake_now_et)
        monkeypatch.setattr(options_engine.time_mod, "sleep", lambda _s: None)

        with pytest.raises(options_engine.spy_scanner.ScanCancelled):
            options_engine._wait_for_market_open(scan_id)

        assert db.get_spy_scan_status(scan_id)["status"] == "running_wait_market"

    def test_wait_for_market_open_returns_immediately_after_open(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-01-01", kind="options")

        from datetime import datetime
        from web import options_engine

        monkeypatch.setattr(
            options_engine.options_data, "now_et",
            lambda: datetime(2026, 6, 1, 10, 0),  # Monday, after MARKET_OPEN_ET
        )

        # Must return without ever sleeping or touching the DB status.
        monkeypatch.setattr(
            options_engine.time_mod, "sleep",
            lambda _s: (_ for _ in ()).throw(AssertionError("should not sleep")),
        )
        options_engine._wait_for_market_open(scan_id)  # no exception = pass
