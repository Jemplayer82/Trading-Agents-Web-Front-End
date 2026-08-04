"""options_engine's running_wait_market / running_alloc status split around
_wait_for_market_open. Moved out of test_spy_scan_status_endpoint.py because
options_engine.py is a tier-4-only module — that file survives down to tier
3 (where options_engine doesn't exist), this one is tier-4-only alongside it.

No network access. Run with: uv run pytest tests/test_options_market_wait.py -v
"""
from __future__ import annotations

import pytest

from web import db

pytestmark = pytest.mark.unit


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
