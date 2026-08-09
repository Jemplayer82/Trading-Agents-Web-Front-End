"""Scan-queue coordination for the portfolio app — tier-agnostic.

At most one scan (portfolio, S&P, or options) runs at a time across the whole
container. Concurrent requests create a 'queued' row and are started FIFO when
the active scan finishes. This module owns the lock, the busy-check, and the
dequeue/dispatch; the actual workers live in the per-tier route modules
(web/portfolio_routes.py, web/spy_routes.py, web/options_routes.py) and plug in
here via ``register_runner``.

The registry stores ``(module, function_name)`` rather than the function object
so ``_dequeue_next_scan`` resolves the target with a LIVE ``getattr`` at
dispatch time. That is load-bearing: tests monkeypatch e.g.
``portfolio_routes._run_scan_thread`` and expect the dequeue to pick up the
patched version. A captured reference would silently bypass the patch.

A lower tier simply never imports the route module for a scan kind, so no
runner is registered for it. Rather than crash (or wedge the queue), a queued
row of an unsupported kind is failed and skipped.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any

from . import credentials as creds
from . import db

log = logging.getLogger(__name__)

# Reentrant: _advance_queue_if_idle() holds it across a _dequeue_next_scan()
# call, and _dequeue_next_scan() acquires it on its own behalf for the callers
# that reach it directly (each scan thread's `finally`).
_SCAN_LOCK = threading.RLock()

# dispatch key ("portfolio" | "spy" | "options") -> (module, function name).
# Populated at import time by whichever route modules the current tier mounts.
_RUNNERS: dict[str, tuple[Any, str]] = {}


def register_runner(key: str, module: Any, func_name: str) -> None:
    """Declare which module/attribute runs scans of dispatch key ``key``.

    Stores the NAME, not the function — see the module docstring.
    """
    _RUNNERS[key] = (module, func_name)


def _wait_market_release_enabled() -> bool:
    """Whether a 'running_wait_market' scan should be treated as idle (env, read at call time).

    Mirrors options_engine._slot_release_enabled's kill switch
    byte-for-byte (same env var, same default, same falsy set) rather
    than importing it: options_engine.py is a tier-4-only module (see
    scripts/make_tier.py's TIER_ONLY_FILES), while this module ships at
    every tier >= 2, so an import here would break the tier 2/3 builds
    that delete options_engine.py entirely. Keep the two functions in
    sync if the env var name, default, or falsy set ever changes.
    """
    return os.environ.get("OPTIONS_RELEASE_SLOT_DURING_WAIT", "1").strip().lower() not in {
        "0", "false", "no", "off"
    }


def _is_any_scan_running(conn) -> dict | None:  # type: ignore[type-arg]
    """Return info dict if any scan is actively running, else None.

    'pending' counts as busy: it's the window between a scan row being created
    and its worker's first status write, and (for daily options runs) the
    multi-account loop creates several rows in one request. Without it two
    back-to-back requests would both see "not busy" and run concurrently. A
    pending row whose worker never started is closed out by the stuck-run
    reaper, so it can't wedge the queue.

    'running_wait_market' does NOT count as busy BY DEFAULT: a scan parked in
    ``options_engine._wait_for_market_open()`` sits from ~07:30 to 09:35 ET
    doing nothing but sleeping in 30s ticks, consuming no LLM budget and no CPU.
    Counting it busy meant the next queued options account could not begin its
    compute phase for 25-45 minutes. Serialization of the phase that actually
    matters — allocation and order placement — is now enforced by
    ``options_engine._ALLOC_LOCK`` (step 3), not by this busy check.

    That default is exactly what ``OPTIONS_RELEASE_SLOT_DURING_WAIT=0`` rolls
    back (see options_engine._slot_release_enabled): with the switch off,
    'running_wait_market' goes back into the busy set below, so a container
    with a parked build reads busy again for the whole ~2h daily wait window —
    the invariant the kill switch's own docstring names the 4GB-container OOM
    risk against. ``_wait_market_release_enabled()`` above reads the same env
    var so this predicate and the proactive dequeue it guards can't disagree.

    Stuck-waiter detection is unaffected: ``db.find_stuck_spy_scans``
    (web/db.py:1149) keys off ``status NOT IN ('completed','cancelled','failed','queued')``
    plus a heartbeat-staleness cutoff, entirely independent of this list, so a
    genuinely dead waiter is still reaped.

    The returned dict now also carries the running row's ``status`` and its
    live progress counters so ``/api/portfolio/status`` is a complete single-row
    poll target for the Run Analysis progress banner, replacing two ~50-row
    history-list fetches every 5s.
    """
    busy_statuses = ["pending", "running_quick", "running_deep", "running_alloc"]
    if not _wait_market_release_enabled():
        busy_statuses.append("running_wait_market")
    placeholders = ",".join("?" for _ in busy_statuses)
    row = conn.execute(
        "SELECT 'portfolio' AS scan_type, id, trade_date, 'equity' AS kind, created_at, status,"
        " scanned_count, scan_total, current_ticker,"
        " NULL AS quick_count, NULL AS quick_total, NULL AS deep_count, NULL AS deep_total"
        " FROM portfolio_scans WHERE status = 'running'"
        " UNION SELECT 'spy', id, trade_date, kind, created_at, status,"
        " NULL, NULL, NULL, quick_count, quick_total, deep_count, deep_total"
        " FROM spy_scans"
        f" WHERE status IN ({placeholders})"
        " LIMIT 1",
        busy_statuses,
    ).fetchone()
    return dict(row) if row else None


def _dequeue_next_scan() -> None:
    """If anything is queued, start the oldest one. Called at the end of every
    scan thread. spy_scans rows carry kind: 'options' rows run the daily
    options build, everything else the equity S&P pipeline.

    Holds _SCAN_LOCK across select-and-claim so two finishing scans (or a
    finishing scan racing the reaper's advance-queue kick) can't both claim
    the same queued row and start it twice."""
    with _SCAN_LOCK:
        with db.connect() as conn:
            # created_at must be IN the compound select — SQLite (correctly) refuses
            # ORDER BY on a column absent from a UNION's result set.
            row = conn.execute(
                "SELECT 'portfolio' AS scan_type, id, trade_date, 'equity' AS kind, created_at"
                " FROM portfolio_scans WHERE status = 'queued'"
                " UNION SELECT 'spy', id, trade_date, kind, created_at"
                " FROM spy_scans WHERE status = 'queued'"
                " ORDER BY created_at LIMIT 1"
            ).fetchone()
        if not row:
            return
        scan_type, scan_id, trade_date = row["scan_type"], row["id"], row["trade_date"]
        log.info("[queue] starting queued %s scan #%s", scan_type, scan_id)
        if scan_type == "portfolio":
            key = "portfolio"
        elif row["kind"] == "options":
            key = "options"
        else:
            key = "spy"

        module, func_name = _RUNNERS.get(key, (None, None))
        if module is None:
            # Lower tier: the route module owning this scan kind was never
            # imported. Fail the row so it leaves the queue instead of being
            # re-selected forever by the next dequeue.
            log.warning(
                "[queue] no runner registered for %s scans — failing queued scan #%s",
                key, scan_id,
            )
            reason = "scan kind not supported at this tier"
            if scan_type == "portfolio":
                db.fail_portfolio_scan(scan_id, reason)
            else:
                db.fail_spy_scan(scan_id, reason)
            return

        # The status write is the "claim" — it must happen under the same lock
        # hold as the select, or a second caller can read the same 'queued' row
        # before this one flips it to running.
        if scan_type == "portfolio":
            db.update_portfolio_scan(scan_id, status="running")
        else:
            db.update_spy_scan(scan_id, status="running_quick")
        target = getattr(module, func_name)
    threading.Thread(target=target, args=(scan_id, trade_date), daemon=True).start()


def _advance_queue_if_idle() -> dict[str, Any] | None:
    """Start the next queued scan iff nothing is currently running.

    Recovery path for the wedge where a worker dies SILENTLY (crash/OOM/host
    SIGKILL): its `finally: _dequeue_next_scan()` never runs, so any scan queued
    behind it sits 'queued' forever with nothing running. The stuck-run reaper
    (web/scheduler.py) calls this after failing abandoned scans. Guarded on
    _is_any_scan_running so it's a safe no-op while a scan is live and can't
    double-start one. Returns the now-running scan, or None if it stayed idle.
    """
    # Lock spans idle-check + dequeue: a scan finishing naturally runs
    # _dequeue_next_scan() from its own `finally` at the same time the reaper
    # gets here, and without this both could start the same queued row.
    with _SCAN_LOCK:
        with db.connect() as conn:
            if _is_any_scan_running(conn):
                return None
        _dequeue_next_scan()
        with db.connect() as conn:
            running = _is_any_scan_running(conn)
    return dict(running) if running else None


def refresh_creds_from_db() -> None:
    """Re-apply DB-stored API keys to env before a scan starts.

    The api container hosts the UI where the user saves keys; this
    container only sees them via the shared sqlite DB. Refreshing
    here means a credential or app setting (Schwab key, etc.) saved
    mid-day takes effect on the very next scan without a restart.

    Public (not _-prefixed) because every route module's worker calls it
    across the module boundary.
    """
    try:
        creds.apply_to_env()
        creds.apply_settings_to_env()
    except Exception:
        log.exception("[creds] refresh failed")
