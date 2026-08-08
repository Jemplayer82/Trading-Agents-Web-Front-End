"""options_engine's running_wait_market / running_alloc status split around
_wait_for_market_open. Moved out of test_spy_scan_status_endpoint.py because
options_engine.py is a tier-4-only module — that file survives down to tier
3 (where options_engine doesn't exist), this one is tier-4-only alongside it.

No network access. Run with: uv run pytest tests/test_options_market_wait.py -v
"""
from __future__ import annotations

import threading
import time as time_mod
from typing import Any

import pytest

from web import db, options_engine, spy_scanner

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


class TestAllocationSlot:
    """_allocation_slot serializes the post-wait phase and remains safe when
    cancelled or timed out while waiting for the global lock."""

    def _release_lock(self):
        if options_engine._ALLOC_LOCK.locked():
            options_engine._ALLOC_LOCK.release()

    def test_mutual_exclusion(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(options_engine, "_ALLOC_POLL_SECONDS", 0.05)

        seq: list[str] = []
        seq_lock = threading.Lock()

        def worker(n: int):
            with options_engine._allocation_slot(scan_id):
                with seq_lock:
                    seq.append(f"enter-{n}")
                time_mod.sleep(0.05)
                with seq_lock:
                    seq.append(f"exit-{n}")

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        try:
            t1.join(timeout=5)
            t2.join(timeout=5)
            assert not t1.is_alive()
            assert not t2.is_alive()
        finally:
            self._release_lock()

        assert len(seq) == 4
        for i in range(0, len(seq), 2):
            enter = seq[i]
            exit = seq[i + 1]
            n = enter.split("-")[1]
            assert exit == f"exit-{n}"

    def test_released_on_exception(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")

        with pytest.raises(RuntimeError, match="boom"):
            with options_engine._allocation_slot(scan_id):
                raise RuntimeError("boom")

        acquired = options_engine._ALLOC_LOCK.acquire(timeout=0.1)
        assert acquired
        try:
            options_engine._ALLOC_LOCK.release()
        finally:
            self._release_lock()

    def test_heartbeats_while_blocked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(options_engine, "_ALLOC_POLL_SECONDS", 0.05)

        held = options_engine._ALLOC_LOCK.acquire()
        assert held
        t = threading.Thread(
            target=lambda: options_engine._allocation_slot(scan_id).__enter__().__exit__(None, None, None)
        )
        t.start()
        try:
            time_mod.sleep(0.3)
            status = db.get_spy_scan_status(scan_id)["status"]
            assert status == "running_wait_market"
        finally:
            self._release_lock()
            t.join(timeout=5)
            assert not t.is_alive()

    def test_cancel_while_blocked_raises_scan_cancelled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(options_engine, "_ALLOC_POLL_SECONDS", 0.05)

        held = options_engine._ALLOC_LOCK.acquire()
        assert held
        db.request_spy_scan_cancel(scan_id)
        caught: dict[str, Any] = {}

        def worker():
            try:
                with options_engine._allocation_slot(scan_id):
                    caught["entered"] = True
            except Exception as exc:
                caught["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        try:
            t.join(timeout=5)
            assert not t.is_alive()
            assert isinstance(caught.get("exc"), spy_scanner.ScanCancelled)
            assert "entered" not in caught
        finally:
            self._release_lock()

    def test_cancel_requested_before_entry_does_not_yield(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        db.request_spy_scan_cancel(scan_id)

        body_ran = False
        with pytest.raises(spy_scanner.ScanCancelled):
            with options_engine._allocation_slot(scan_id):
                body_ran = True

        assert body_ran is False
        assert not options_engine._ALLOC_LOCK.locked()

    def test_timeout_raises_instead_of_hanging(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(options_engine, "_ALLOC_POLL_SECONDS", 0.05)
        monkeypatch.setattr(options_engine, "_ALLOC_TIMEOUT_SECONDS", 0.1)

        held = options_engine._ALLOC_LOCK.acquire()
        assert held
        caught: dict[str, Any] = {}

        def worker():
            try:
                with options_engine._allocation_slot(scan_id):
                    caught["entered"] = True
            except Exception as exc:
                caught["exc"] = exc

        t = threading.Thread(target=worker)
        start = time_mod.monotonic()
        t.start()
        try:
            t.join(timeout=5)
            elapsed = time_mod.monotonic() - start
            assert not t.is_alive()
            exc = caught.get("exc")
            assert isinstance(exc, RuntimeError)
            assert "allocation slot" in str(exc)
            assert elapsed < 2
        finally:
            self._release_lock()