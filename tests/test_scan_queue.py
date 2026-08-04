"""Tests for web/scan_queue.py — the runner registry and its tier degrade path.

The queue itself ships at every tier, but the workers live in tier-gated route
modules (portfolio_routes / spy_routes / options_routes). A lower tier simply
never imports the module for a scan kind, so _dequeue_next_scan can meet a
queued row it has no runner for. That must fail the row and move on, not raise
and not wedge the queue.

Ordering/locking behavior for the normal dispatch paths lives in
tests/test_options_lifecycle.py.
"""
from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from web import db, features, portfolio_main, scan_queue

pytestmark = pytest.mark.unit


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    return tmp_path / "web.db"


@pytest.fixture()
def thread_spy(monkeypatch):
    """Capture threading.Thread(...) calls made by _dequeue_next_scan.

    Patches only scan_queue's reference to the threading module, so the real
    threading module (and the already-constructed _SCAN_LOCK) are untouched.
    """
    calls: list[dict] = []

    def _thread(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(start=lambda: None)

    monkeypatch.setattr(scan_queue, "threading", SimpleNamespace(Thread=_thread))
    return calls


class TestMissingRunnerDegrades:
    """No runner registered for a scan kind => fail the row, start nothing."""

    def test_queued_spy_row_is_failed(self, tmp_db, monkeypatch, thread_spy):
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        scan_id = db.create_spy_scan("2026-08-03", status="queued", kind="equity")

        scan_queue._dequeue_next_scan()  # must not raise

        assert db.get_spy_scan(scan_id)["status"] == "failed"
        assert thread_spy == [], "no worker thread may be started"

    def test_queued_options_row_is_failed(self, tmp_db, monkeypatch, thread_spy):
        """Options rows live in spy_scans too — they must take the spy fail path."""
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        acct = db.create_paper_account("Tier Test", kind="options")
        scan_id = db.create_spy_scan("2026-08-03", paper_account_id=acct,
                                     status="queued", kind="options")

        scan_queue._dequeue_next_scan()

        assert db.get_spy_scan(scan_id)["status"] == "failed"
        assert thread_spy == []

    def test_queued_portfolio_row_is_failed(self, tmp_db, monkeypatch, thread_spy):
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        scan_id = db.create_portfolio_scan("2026-08-03", status="queued")

        scan_queue._dequeue_next_scan()

        assert db.get_portfolio_scan(scan_id)["status"] == "failed"
        assert thread_spy == []

    def test_failed_row_leaves_the_queue(self, tmp_db, monkeypatch, thread_spy):
        """The row must not be re-selected forever: failing it drops it out of
        the 'queued' set so the next dequeue moves on to the following row."""
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        first = db.create_spy_scan("2026-08-03", status="queued", kind="equity")
        second = db.create_spy_scan("2026-08-03", status="queued", kind="equity")
        with db.connect() as conn:
            conn.execute("UPDATE spy_scans SET created_at = '2026-08-03T00:00:00Z' WHERE id = ?", (first,))
            conn.execute("UPDATE spy_scans SET created_at = '2026-08-03T00:00:01Z' WHERE id = ?", (second,))

        scan_queue._dequeue_next_scan()
        scan_queue._dequeue_next_scan()

        assert db.get_spy_scan(first)["status"] == "failed"
        assert db.get_spy_scan(second)["status"] == "failed"


class TestRunnerRegistry:
    def test_dispatch_resolves_the_runner_live(self, tmp_db, monkeypatch, thread_spy):
        """register_runner stores (module, name), not the function object, so a
        later monkeypatch of that attribute is what actually gets dispatched.
        Capturing the function at registration time would silently run the real
        worker under test — see the monkeypatch-based tests in
        tests/test_options_lifecycle.py."""
        holder = SimpleNamespace(_worker=lambda scan_id, trade_date: None)
        monkeypatch.setattr(scan_queue, "_RUNNERS", {})
        scan_queue.register_runner("spy", holder, "_worker")

        def _patched(scan_id, trade_date):
            return None

        holder._worker = _patched  # rebind AFTER registration
        scan_id = db.create_spy_scan("2026-08-03", status="queued", kind="equity")

        scan_queue._dequeue_next_scan()

        assert len(thread_spy) == 1
        assert thread_spy[0]["target"] is _patched
        assert thread_spy[0]["args"] == (scan_id, "2026-08-03")
        assert db.get_spy_scan(scan_id)["status"] == "running_quick"


class TestPortfolioAppTierGating:
    """The shell mounts route modules per tier; the default tier must expose the
    exact pre-split path set."""

    @pytest.fixture()
    def app_paths(self, monkeypatch):
        def _load(**env):
            monkeypatch.delenv("TIER", raising=False)
            monkeypatch.delenv("FEATURES", raising=False)
            for k, v in env.items():
                monkeypatch.setenv(k, v)
            importlib.reload(features)
            importlib.reload(portfolio_main)
            return {r.path for r in portfolio_main.app.routes if hasattr(r, "path")}

        yield _load
        # Leave web.portfolio_main.app back at the default tier for any test
        # that imports it later (e.g. tests/test_clear_history.py's TestClient).
        monkeypatch.delenv("TIER", raising=False)
        monkeypatch.delenv("FEATURES", raising=False)
        importlib.reload(features)
        importlib.reload(portfolio_main)

    def test_default_tier_has_every_path(self, app_paths):
        paths = app_paths()
        assert {
            "/api/health", "/api/auth/schwab/status", "/api/portfolio-scan",
            "/api/portfolio-scans", "/api/portfolio-scans/{scan_id}",
            "/api/portfolio/status", "/api/portfolio/advance-queue",
            "/api/accounts", "/api/paper-accounts", "/api/spy-scan",
            "/api/spy-scans", "/api/spy-account", "/api/spy-account/compare",
            "/api/options-scan", "/api/options-positions", "/api/options-summary",
        } <= paths

    def test_tier_2_drops_spy_and_options(self, app_paths):
        paths = app_paths(TIER="2")
        assert "/api/portfolio-scan" in paths
        assert "/api/accounts" in paths
        assert not [p for p in paths if p.startswith(("/api/spy", "/api/options", "/api/paper-accounts"))]

    def test_tier_3_drops_options_only(self, app_paths):
        paths = app_paths(TIER="3")
        assert "/api/spy-scan" in paths
        assert "/api/paper-accounts" in paths
        assert not [p for p in paths if p.startswith("/api/options")]

    def test_queue_introspection_survives_tier_1(self, app_paths):
        """The queue endpoints are tier-agnostic and stay in the shell."""
        paths = app_paths(TIER="1")
        assert {"/api/portfolio/status", "/api/portfolio/advance-queue"} <= paths
        assert not [p for p in paths if p.startswith(("/api/spy", "/api/options", "/api/portfolio-"))]
