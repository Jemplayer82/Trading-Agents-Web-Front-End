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
        t1.join(timeout=5)
        t2.join(timeout=5)
        assert not t1.is_alive()
        assert not t2.is_alive()

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
        lock_held = True
        try:
            options_engine._ALLOC_LOCK.release()
            lock_held = False
        finally:
            if lock_held:
                options_engine._ALLOC_LOCK.release()
                lock_held = False

    def test_heartbeats_while_blocked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(options_engine, "_ALLOC_POLL_SECONDS", 0.05)

        lock_held = options_engine._ALLOC_LOCK.acquire()
        assert lock_held
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
            time_mod.sleep(0.3)
            status = db.get_spy_scan_status(scan_id)["status"]
            assert status == "running_wait_alloc"
            options_engine._ALLOC_LOCK.release()
            lock_held = False
            t.join(timeout=5)
            assert not t.is_alive()
            assert "entered" in caught
            assert "exc" not in caught
        finally:
            if lock_held:
                options_engine._ALLOC_LOCK.release()
                lock_held = False
            t.join(timeout=5)

    def test_cancel_while_blocked_raises_scan_cancelled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        monkeypatch.setattr(options_engine, "_ALLOC_POLL_SECONDS", 0.05)

        lock_held = options_engine._ALLOC_LOCK.acquire()
        assert lock_held
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
            if lock_held:
                options_engine._ALLOC_LOCK.release()
                lock_held = False

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

        lock_held = options_engine._ALLOC_LOCK.acquire()
        assert lock_held
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
            if lock_held:
                options_engine._ALLOC_LOCK.release()
                lock_held = False


class TestWaitReleasesTheQueueSlot:
    """_wait_for_market_open should hand its scan-queue slot to the next queued
    options account the moment it actually starts blocking, so the next account
    can begin its own compute phase immediately. Allocation is still serialized
    by _allocation_slot()."""

    def _make_blocking_clock(self, monkeypatch, scan_id: int):
        """Monday 07:30 ET; requests cancel after a couple of ticks."""
        from datetime import datetime

        calls = {"n": 0}

        def fake_now_et():
            calls["n"] += 1
            if calls["n"] > 2:
                db.request_spy_scan_cancel(scan_id)
            return datetime(2026, 6, 1, 7, 30)  # Monday, before MARKET_OPEN_ET

        monkeypatch.setattr(options_engine.options_data, "now_et", fake_now_et)
        monkeypatch.setattr(options_engine.time_mod, "sleep", lambda _s: None)

    def test_dequeue_called_once_on_entering_the_wait(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        self._make_blocking_clock(monkeypatch, scan_id)

        dequeues = {"n": 0}

        def fake_dequeue():
            dequeues["n"] += 1

        monkeypatch.setattr(options_engine.scan_queue, "_dequeue_next_scan", fake_dequeue)

        with pytest.raises(options_engine.spy_scanner.ScanCancelled):
            options_engine._wait_for_market_open(scan_id)

        assert dequeues["n"] == 1

    def test_no_dequeue_when_the_wait_returns_immediately(self, monkeypatch, tmp_path):
        from datetime import datetime

        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")

        monkeypatch.setattr(
            options_engine.options_data, "now_et",
            lambda: datetime(2026, 6, 1, 10, 0),  # Monday, after MARKET_OPEN_ET
        )
        monkeypatch.setattr(
            options_engine.time_mod, "sleep",
            lambda _s: (_ for _ in ()).throw(AssertionError("should not sleep")),
        )

        dequeues = {"n": 0}

        def fake_dequeue():
            dequeues["n"] += 1

        monkeypatch.setattr(options_engine.scan_queue, "_dequeue_next_scan", fake_dequeue)

        options_engine._wait_for_market_open(scan_id)

        assert dequeues["n"] == 0

    def test_dequeue_failure_does_not_break_the_wait(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        self._make_blocking_clock(monkeypatch, scan_id)

        dequeues = {"n": 0}

        def boom_dequeue():
            dequeues["n"] += 1
            raise RuntimeError("boom")

        monkeypatch.setattr(options_engine.scan_queue, "_dequeue_next_scan", boom_dequeue)

        with pytest.raises(options_engine.spy_scanner.ScanCancelled):
            options_engine._wait_for_market_open(scan_id)

        assert dequeues["n"] == 1

    def test_kill_switch_disables_the_handoff(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OPTIONS_RELEASE_SLOT_DURING_WAIT", "0")
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()
        scan_id = db.create_spy_scan("2026-06-01", kind="options")
        self._make_blocking_clock(monkeypatch, scan_id)

        dequeues = {"n": 0}

        def fake_dequeue():
            dequeues["n"] += 1

        monkeypatch.setattr(options_engine.scan_queue, "_dequeue_next_scan", fake_dequeue)

        with pytest.raises(options_engine.spy_scanner.ScanCancelled):
            options_engine._wait_for_market_open(scan_id)

        assert dequeues["n"] == 0

    def test_wait_entry_actually_starts_the_next_queued_scan(self, monkeypatch, tmp_path):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        # Importing options_routes registers the 'options' runner with scan_queue.
        from web import options_routes, scan_queue

        account_id = db.create_paper_account(
            "wait-release-e2e", 100_000.0, "options"
        )
        scan1 = db.create_spy_scan(
            "2026-06-01", kind="options", paper_account_id=account_id
        )
        scan2 = db.create_spy_scan(
            "2026-06-01", kind="options", paper_account_id=account_id
        )
        db.update_spy_scan(scan2, status="queued")

        started = threading.Event()
        recorded: dict[str, Any] = {}

        def recorder(scan_id: int, *args: Any, **kwargs: Any) -> None:
            recorded["scan_id"] = scan_id
            # Mirror what the real thread wrapper does on entry so the test can
            # observe the queued row has genuinely been handed off to a runner.
            db.update_spy_scan(scan_id, status="running_quick")
            started.set()

        # The registry dispatches via live getattr(module, func_name), so patch
        # the function on options_routes and point the registry back at it.
        monkeypatch.setattr(options_routes, "_run_options_scan_thread", recorder)
        scan_queue.register_runner("options", options_routes, "_run_options_scan_thread")

        self._make_blocking_clock(monkeypatch, scan1)

        wait_thread = threading.Thread(
            target=options_engine._wait_for_market_open, args=(scan1,)
        )
        wait_thread.start()
        try:
            assert started.wait(timeout=5), "queued scan runner did not start"
            assert recorded["scan_id"] == scan2
            assert db.get_spy_scan_status(scan2)["status"] == "running_quick"
        finally:
            wait_thread.join(timeout=5)
            assert not wait_thread.is_alive()


class TestRunOptionsBuildHoldsAllocationLock:
    """run_options_build end-to-end: the global allocation lock is held during
    the post-wait phase and released afterward. Also guards that prescreen
    is invoked with the keyword-only trade_date argument."""

    def test_run_options_build_holds_allocation_lock_during_refresh_positions(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
        db.init_db()

        from datetime import datetime

        trade_date = "2026-06-06"
        account_id = db.create_paper_account("lock-e2e", 100_000.0, "options")
        scan_id = db.create_spy_scan(
            trade_date, kind="options", paper_account_id=account_id
        )

        captured: dict[str, Any] = {}

        # Phase 1: universe + movers pre-screen.
        monkeypatch.setattr(options_engine, "get_sp500_tickers", lambda: ["AAPL"])

        def fake_prescreen(tickers, top_n=None, *, trade_date=None):
            captured["prescreen_trade_date"] = trade_date
            assert trade_date == trade_date  # keyword-only, asserted below too
            return ["AAPL"]

        monkeypatch.setattr(options_engine, "prescreen", fake_prescreen)

        # Phase 2: quick scan returns only a HOLD name + no SPY row, so
        # select_deep_dive_targets() is empty and the real run_deep_dives path
        # is never reached.
        def fake_run_quick_scan(scan_id_arg, movers, trade_date_arg, config):
            return [{"ticker": "AAPL", "signal": "HOLD", "conviction": 1}]

        monkeypatch.setattr(
            options_engine.spy_scanner, "run_quick_scan", fake_run_quick_scan
        )

        # Skip the market-open wait: Saturday after open returns immediately.
        monkeypatch.setattr(
            options_engine.options_data,
            "now_et",
            lambda: datetime(2026, 6, 6, 10, 0),
        )

        # The load-bearing assertion: refresh_positions runs inside _allocation_slot.
        def fake_refresh_positions(paper_account_id=None):
            captured["lock_during_refresh"] = options_engine._ALLOC_LOCK.locked()
            assert captured["lock_during_refresh"] is True

        monkeypatch.setattr(options_engine, "refresh_positions", fake_refresh_positions)

        # Avoid the real LLM allocator while keeping the contract shape intact.
        def fake_allocator_run(*args, **kwargs):
            return {
                "closes": [],
                "holds": [],
                "opens": [],
                "report_md": "test allocation report",
            }

        monkeypatch.setattr(options_engine.options_allocator, "run", fake_allocator_run)

        options_engine.run_options_build(scan_id, trade_date)

        assert captured["prescreen_trade_date"] == trade_date
        assert captured["lock_during_refresh"] is True
        assert not options_engine._ALLOC_LOCK.locked()
        assert db.get_spy_scan_status(scan_id)["status"] == "completed"
