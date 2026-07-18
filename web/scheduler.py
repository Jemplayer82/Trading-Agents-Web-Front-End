"""APScheduler daemon — cron triggers for scans, newsletter, and health checks.

Runs inside the tradingagents-scheduler container (`python -m web.scheduler`).
It does no analysis itself: scan jobs are fire-and-forget HTTP POSTs to the
portfolio/api containers over the Docker network, authenticated with the
X-Internal-Token header (INTERNAL_API_TOKEN, verified server-side in
web/auth_app.py with hmac.compare_digest). The newsletter is the exception —
it sends from this process directly, which is why _apply_db_config() must
pull SMTP/notifier settings from the shared DB first.

Schedule (all times SCHEDULER_TIMEZONE, default America/New_York):
    22:00 Mon-Fri        portfolio scan      POST /api/portfolio-scan
    23:30 Mon-Fri        outcome sweep       (in-process — resolves pending memory-log entries)
    05:00 daily          morning newsletter  (in-process)
    hourly               Schwab token health GET /api/auth/schwab/status
    Sat 00:00            S&P 500 scan        POST /api/spy-scan
    Mon-Fri 09:00-16:00  SPY price refresh   POST /api/spy-scans/latest/refresh-prices
    Mon-Fri 07:30        options scan        POST /api/options-scan (all options accounts)
    Mon-Fri 10:00-16:00  options mark        POST /api/options-positions/refresh (+16:45 close pass)
    Mon-Fri 20:00        options settlement  POST /api/options-positions/settle

Each job also has a --run-*-now CLI flag for one-shot manual runs (handy for
testing inside the container without waiting for cron).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Any

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import alerts, db, newsletter
from . import credentials as creds
from ._logging import configure_logging
from .notifier import default_notifier

configure_logging()
log = logging.getLogger("scheduler")

API_URL = (os.environ.get("TRADINGAGENTS_API_URL") or "http://tradingagents-api:8000").rstrip("/")
PORTFOLIO_URL = (os.environ.get("TRADINGAGENTS_PORTFOLIO_URL") or "http://tradingagents-portfolio:8000").rstrip("/")
TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "America/New_York")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://trading.txferguson.net").rstrip("/")

# Stuck-run reaper thresholds (minutes). A run quiet past its limit is treated as a
# crashed worker. Generous defaults so a slow-but-healthy deep dive isn't reaped.
STUCK_SCAN_STALL_MIN = int(os.environ.get("STUCK_SCAN_STALL_MIN", "60"))
STUCK_ANALYSIS_MIN = int(os.environ.get("STUCK_ANALYSIS_MIN", "90"))


def _internal_headers() -> dict[str, str]:
    """Auth bypass header so cron calls pass the api/portfolio login gate."""
    tok = os.environ.get("INTERNAL_API_TOKEN")
    return {"X-Internal-Token": tok} if tok else {}


def _apply_db_config() -> None:
    """Pull UI-saved credentials + settings (SMTP, notifier, etc.) onto env.

    The scheduler sends the newsletter and notifications directly, so it
    needs SMTP_* / FRED_NOTIFY_URL from the shared DB — it has no other
    way to see values the user saved in the dashboard.
    """
    try:
        db.init_db()
        creds.apply_to_env()
        creds.apply_settings_to_env()
    except Exception:
        log.exception("[scheduler] applying DB config failed")


def job_nightly_scan() -> None:
    """Kick off the portfolio container's nightly scan.

    The endpoint queues a background task and returns immediately, so the
    60s timeout covers request startup only — never the multi-hour scan.
    The endpoint is idempotent per trade date, so a retry can't double-scan.
    """
    log.info("[nightly_scan] firing portfolio scan at %s", PORTFOLIO_URL)
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/portfolio-scan", timeout=60, headers=_internal_headers())
        log.info("[nightly_scan] response %s: %s", r.status_code, r.text[:400])
    except Exception as exc:
        log.exception("[nightly_scan] failed: %s", exc)
        default_notifier().send(
            "⚠️ Nightly portfolio scan failed to start.",
            link=DASHBOARD_URL,
        )


def _latest_scan_for_today() -> dict[str, Any] | None:
    """Latest portfolio scan, with a warning (not a skip) when it isn't from today."""
    today_iso = datetime.utcnow().date().isoformat()
    try:
        scan = db.latest_portfolio_scan()
    except Exception:
        log.exception("[newsletter] db read failed")
        return None
    if not scan:
        return None
    trade_date = (scan.get("trade_date") or "")[:10]
    if trade_date != today_iso:
        log.warning("[newsletter] latest scan trade_date=%s != today=%s — sending anyway", trade_date, today_iso)
    return scan


def job_morning_newsletter() -> None:
    """Email the overnight scan as the morning newsletter.

    Deliberately permissive: a stale or still-running scan logs a warning but
    sends anyway — a slightly old briefing beats no briefing.
    """
    log.info("[newsletter] morning newsletter job firing")
    _apply_db_config()  # ensure latest SMTP_* / notifier settings from the UI
    scan = _latest_scan_for_today()
    if not scan:
        log.warning("[newsletter] no scan to send")
        default_notifier().send("Morning newsletter skipped: no overnight scan found.")
        return
    if scan.get("status") != "completed":
        log.warning("[newsletter] latest scan status=%s — sending anyway", scan.get("status"))
    try:
        msg_id = newsletter.send(scan)
        if msg_id:
            db.mark_newsletter_sent(int(scan["id"]), msg_id)
            log.info("[newsletter] sent scan id=%s msg_id=%s", scan["id"], msg_id)
        else:
            log.warning("[newsletter] send returned None")
    except Exception:
        log.exception("[newsletter] crash during send")


def job_token_health() -> None:
    """Hourly Schwab MCP session check; notifies the user when re-auth is needed."""
    try:
        r = httpx.get(f"{API_URL}/api/auth/schwab/status", timeout=15, headers=_internal_headers())
        if r.status_code != 200:
            log.warning("[token_health] status %s: %s", r.status_code, r.text[:200])
            return
        data = r.json()
    except Exception:
        log.exception("[token_health] failed to reach api")
        return
    # Master switch off → user has no Schwab; nothing to nag about.
    if not data.get("enabled", True):
        log.info("[token_health] Schwab disabled — skipping")
        return
    if not data.get("connected"):
        log.warning("[token_health] Schwab MCP not authorized")
        default_notifier().send(
            "⚠️ Schwab MCP session is not authorized — nightly/portfolio scans and live "
            "quotes are down. Re-authorize at https://schwab.txferguson.net/auth",
            link="https://schwab.txferguson.net/auth",
        )
        return
    log.info("[token_health] Schwab MCP connected")


def job_spy_scan() -> None:
    log.info("[spy_scan] firing S&P 500 scan at %s", PORTFOLIO_URL)
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/spy-scan", timeout=60, headers=_internal_headers())
        log.info("[spy_scan] response %s: %s", r.status_code, r.text[:400])
    except Exception as exc:
        log.exception("[spy_scan] failed: %s", exc)


def job_spy_price_refresh() -> None:
    """Mark the latest SPY paper portfolio to market (also records signal flips)."""
    log.info("[spy_price_refresh] refreshing latest SPY scan prices")
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/spy-scans/latest/refresh-prices", timeout=120, headers=_internal_headers())
        log.info("[spy_price_refresh] response %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.exception("[spy_price_refresh] failed: %s", exc)


def job_options_scan() -> None:
    """Kick off the daily options build for every options paper account.

    The endpoint is idempotent per (account, day) and queues background
    workers, so the 60s timeout covers request startup only. A 409 means no
    options accounts exist yet — informational, not a failure.
    """
    log.info("[options_scan] firing daily options scan at %s", PORTFOLIO_URL)
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/options-scan", timeout=60, headers=_internal_headers())
        if r.status_code == 409:
            log.info("[options_scan] skipped: %s", r.text[:200])
        else:
            log.info("[options_scan] response %s: %s", r.status_code, r.text[:400])
    except Exception as exc:
        log.exception("[options_scan] failed: %s", exc)


def job_options_refresh() -> None:
    """Settle due expiries + mark all open option positions to market."""
    log.info("[options_refresh] refreshing option marks")
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/options-positions/refresh", timeout=300,
                       headers=_internal_headers())
        log.info("[options_refresh] response %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.exception("[options_refresh] failed: %s", exc)


def job_options_settle() -> None:
    """Nightly expiry-settlement sweep (idempotent; daily bars final after close)."""
    log.info("[options_settle] running expiry settlement sweep")
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/options-positions/settle", timeout=300,
                       headers=_internal_headers())
        log.info("[options_settle] response %s: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.exception("[options_settle] failed: %s", exc)


def job_outcome_sweep() -> None:
    """Resolve ALL matured pending memory-log decisions (every ticker).

    In-process like the newsletter: this container mounts the same
    ``tradingagents_data`` volume that holds the memory log, so no HTTP hop is
    needed. Without this sweep, entries only resolve when the same ticker is
    re-run through the CLI graph — in the deployed topology that almost never
    happens, so decisions sit pending forever and the agents learn from a
    sparse, survivor-biased subset of their own track record.

    Runs after US close so day-0 bars are final; the maturity guard inside
    ``resolve_all_pending`` ensures each entry waits for its full holding
    window. LLM cost is bounded by ``sweep_max_reflections_per_run``; canned
    NOISE/CENSORED reflections are free.
    """
    log.info("[outcome_sweep] starting")
    _apply_db_config()  # LLM provider keys may live in the shared DB
    try:
        from tradingagents.agents.utils.memory import TradingMemoryLog
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.outcome_resolution import resolve_all_pending
        from tradingagents.graph.reflection import Reflector
        from tradingagents.llm_clients import create_llm_client

        config = dict(DEFAULT_CONFIG)
        memory_log = TradingMemoryLog(config)
        # Missing/misconfigured LLM keys degrade the sweep (NOISE/CENSORED
        # entries still resolve; LLM-graded ones defer) instead of killing it.
        reflector = None
        try:
            llm = create_llm_client(
                provider=config["llm_provider"],
                model=config["quick_think_llm"],
                base_url=config.get("backend_url"),
            ).get_llm()
            reflector = Reflector(llm)
        except Exception:
            log.exception("[outcome_sweep] LLM client unavailable — resolving canned entries only")
        summary = resolve_all_pending(
            memory_log,
            reflector,
            config,
            max_reflections=config.get("sweep_max_reflections_per_run", 50),
        )
        log.info("[outcome_sweep] done: %s", summary)
        if summary["errors"]:
            log.warning("[outcome_sweep] %d entries errored — see log above", summary["errors"])
    except Exception:
        log.exception("[outcome_sweep] crashed")


def _cutoff_iso(minutes: int) -> str:
    """UTC cutoff `minutes` in the past, in the same ISO+Z format rows are stored."""
    return (datetime.utcnow() - timedelta(minutes=minutes)).isoformat(timespec="seconds") + "Z"


def job_reap_stuck_runs() -> None:
    """Fail + alert runs whose worker died silently (status stuck in 'running').

    A crash/OOM never runs the in-process `except` that would mark a run failed, so
    these rows would otherwise sit 'running' forever — invisible to the user and
    showing as phantom progress on the Analysis tab. We detect them by a stalled
    liveness stamp (scans) or age (analyses), close them out, and notify once each.
    """
    scan_cutoff = _cutoff_iso(STUCK_SCAN_STALL_MIN)
    analysis_cutoff = _cutoff_iso(STUCK_ANALYSIS_MIN)
    scan_err = f"abandoned — no progress for {STUCK_SCAN_STALL_MIN} min, worker likely crashed"
    analysis_err = f"abandoned — still running after {STUCK_ANALYSIS_MIN} min, worker likely crashed"
    try:
        for scan in db.find_stuck_portfolio_scans(scan_cutoff):
            db.fail_portfolio_scan(scan["id"], scan_err)
            log.warning("[reaper] failed stuck portfolio scan %s", scan["id"])
            alerts.notify_run_failed(kind="Portfolio scan", run_id=scan["id"],
                                     label=scan.get("trade_date") or "", error=scan_err)
        for scan in db.find_stuck_spy_scans(scan_cutoff):
            db.fail_spy_scan(scan["id"], scan_err)
            kind_label = "Options scan" if scan.get("kind") == "options" else "S&P 500 scan"
            log.warning("[reaper] failed stuck %s %s", kind_label, scan["id"])
            alerts.notify_run_failed(kind=kind_label, run_id=scan["id"],
                                     label=scan.get("trade_date") or "", error=scan_err)
        for a in db.find_stuck_analyses(analysis_cutoff):
            db.fail_analysis(a["id"], analysis_err)
            log.warning("[reaper] failed stuck analysis %s", a["id"])
            alerts.notify_run_failed(kind="Analysis", run_id=a["id"],
                                     label=a.get("ticker") or "", error=analysis_err)
    except Exception:
        log.exception("[reaper] sweep failed")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-newsletter-now", action="store_true", help="Run newsletter once and exit")
    parser.add_argument("--run-scan-now", action="store_true", help="Trigger Schwab scan once and exit")
    parser.add_argument("--run-spy-scan-now", action="store_true", help="Trigger SPY scan once and exit")
    parser.add_argument("--refresh-spy-prices", action="store_true", help="Refresh SPY prices once and exit")
    parser.add_argument("--run-sweep-now", action="store_true", help="Run outcome-resolution sweep once and exit")
    parser.add_argument("--run-options-scan-now", action="store_true", help="Trigger daily options scan once and exit")
    parser.add_argument("--refresh-options-now", action="store_true", help="Refresh option marks once and exit")
    parser.add_argument("--settle-options-now", action="store_true", help="Run options expiry settlement once and exit")
    args = parser.parse_args()

    _apply_db_config()  # pull UI-saved SMTP/notifier/credentials onto env

    if args.send_newsletter_now:
        job_morning_newsletter()
        return
    if args.run_scan_now:
        job_nightly_scan()
        return
    if args.run_spy_scan_now:
        job_spy_scan()
        return
    if args.refresh_spy_prices:
        job_spy_price_refresh()
        return
    if args.run_sweep_now:
        job_outcome_sweep()
        return
    if args.run_options_scan_now:
        job_options_scan()
        return
    if args.refresh_options_now:
        job_options_refresh()
        return
    if args.settle_options_now:
        job_options_settle()
        return

    sched = BlockingScheduler(timezone=TIMEZONE)
    sched.add_job(
        job_nightly_scan,
        # Mon-Fri only: holdings don't move over the closed-market weekend, so a
        # Sat/Sun scan would burn a full multi-agent LLM pass on stale Friday-close
        # data. Mirrors the spy_price_refresh weekday restriction below.
        CronTrigger(day_of_week="mon-fri", hour=22, minute=0, timezone=TIMEZONE),
        id="nightly_scan",
        replace_existing=True,
    )
    sched.add_job(
        job_morning_newsletter,
        CronTrigger(hour=5, minute=0, timezone=TIMEZONE),
        id="morning_newsletter",
        replace_existing=True,
    )
    sched.add_job(
        job_token_health,
        IntervalTrigger(hours=1),
        id="token_health",
        replace_existing=True,
    )
    sched.add_job(
        job_spy_scan,
        CronTrigger(day_of_week="sat", hour=0, minute=0, timezone=TIMEZONE),
        id="spy_scan",
        replace_existing=True,
    )
    sched.add_job(
        job_spy_price_refresh,
        CronTrigger(day_of_week="mon-fri", hour="9-16", minute=0, timezone=TIMEZONE),
        id="spy_price_refresh",
        replace_existing=True,
    )
    sched.add_job(
        job_reap_stuck_runs,
        IntervalTrigger(minutes=20),
        id="reap_stuck_runs",
        replace_existing=True,
    )
    sched.add_job(
        job_outcome_sweep,
        # Mon-Fri 23:30 ET: after US close (day-0 bars final) and after the
        # 22:00 nightly scan has queued, so the two don't contend.
        CronTrigger(day_of_week="mon-fri", hour=23, minute=30, timezone=TIMEZONE),
        id="outcome_sweep",
        replace_existing=True,
    )
    sched.add_job(
        job_options_scan,
        # 07:30 ET: quick scan + 25 deep dives run pre-market; the build's own
        # market-open gate holds allocation until 09:35 so entries fill at
        # live mids.
        CronTrigger(day_of_week="mon-fri", hour=7, minute=30, timezone=TIMEZONE),
        id="options_scan",
        replace_existing=True,
    )
    sched.add_job(
        job_options_refresh,
        CronTrigger(day_of_week="mon-fri", hour="10-16", minute=0, timezone=TIMEZONE),
        id="options_refresh",
        replace_existing=True,
    )
    sched.add_job(
        job_options_refresh,
        # Extra pass just after the close so the day ends on settled marks.
        CronTrigger(day_of_week="mon-fri", hour=16, minute=45, timezone=TIMEZONE),
        id="options_refresh_close",
        replace_existing=True,
    )
    sched.add_job(
        job_options_settle,
        # 20:00 ET: daily bars final; catches Friday expiries same evening and
        # holiday-shifted Thursday expiries without a market calendar.
        CronTrigger(day_of_week="mon-fri", hour=20, minute=0, timezone=TIMEZONE),
        id="options_settle",
        replace_existing=True,
    )
    # Sweep once at startup too — a crash that happened while the scheduler was
    # down should be caught and alerted immediately, not up to 20 min later.
    job_reap_stuck_runs()
    log.info("Scheduler starting (tz=%s)", TIMEZONE)
    log.info(" - nightly_scan       cron 22:00 Mon-Fri %s", TIMEZONE)
    log.info(" - morning_newsletter cron 05:00 %s", TIMEZONE)
    log.info(" - token_health       every 1h")
    log.info(" - spy_scan           cron Sat 00:00 %s", TIMEZONE)
    log.info(" - spy_price_refresh  cron hourly Mon-Fri 09:00-16:00 %s", TIMEZONE)
    log.info(" - reap_stuck_runs    every 20m (stall>%dm scans / %dm analyses)",
             STUCK_SCAN_STALL_MIN, STUCK_ANALYSIS_MIN)
    log.info(" - outcome_sweep      cron 23:30 Mon-Fri %s", TIMEZONE)
    log.info(" - options_scan       cron 07:30 Mon-Fri %s", TIMEZONE)
    log.info(" - options_refresh    cron hourly Mon-Fri 10:00-16:00 + 16:45 %s", TIMEZONE)
    log.info(" - options_settle     cron 20:00 Mon-Fri %s", TIMEZONE)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
