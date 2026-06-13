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
    05:00 daily          morning newsletter  (in-process)
    hourly               Schwab token health GET /api/auth/schwab/status
    Sat 00:00            S&P 500 scan        POST /api/spy-scan
    Mon-Fri 09:00-16:00  SPY price refresh   POST /api/spy-scans/latest/refresh-prices

Each job also has a --run-*-now CLI flag for one-shot manual runs (handy for
testing inside the container without waiting for cron).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from typing import Any

import httpx
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from . import credentials as creds
from . import db, newsletter
from ._logging import configure_logging
from .notifier import default_notifier

configure_logging()
log = logging.getLogger("scheduler")

API_URL = (os.environ.get("TRADINGAGENTS_API_URL") or "http://tradingagents-api:8000").rstrip("/")
PORTFOLIO_URL = (os.environ.get("TRADINGAGENTS_PORTFOLIO_URL") or "http://tradingagents-portfolio:8000").rstrip("/")
TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "America/New_York")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://trading.txferguson.net").rstrip("/")


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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-newsletter-now", action="store_true", help="Run newsletter once and exit")
    parser.add_argument("--run-scan-now", action="store_true", help="Trigger Schwab scan once and exit")
    parser.add_argument("--run-spy-scan-now", action="store_true", help="Trigger SPY scan once and exit")
    parser.add_argument("--refresh-spy-prices", action="store_true", help="Refresh SPY prices once and exit")
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
    log.info("Scheduler starting (tz=%s)", TIMEZONE)
    log.info(" - nightly_scan       cron 22:00 Mon-Fri %s", TIMEZONE)
    log.info(" - morning_newsletter cron 05:00 %s", TIMEZONE)
    log.info(" - token_health       every 1h")
    log.info(" - spy_scan           cron Sat 00:00 %s", TIMEZONE)
    log.info(" - spy_price_refresh  cron hourly Mon-Fri 09:00-16:00 %s", TIMEZONE)
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
