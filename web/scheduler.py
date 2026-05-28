"""APScheduler daemon — nightly portfolio scan, 5am newsletter, hourly health.

Runs inside the tradingagents-scheduler container. All HTTP calls go to the
portfolio + api containers via the docker network.
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

from . import db, newsletter
from .notifier import default_notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("scheduler")

API_URL = (os.environ.get("TRADINGAGENTS_API_URL") or "http://tradingagents-api:8000").rstrip("/")
PORTFOLIO_URL = (os.environ.get("TRADINGAGENTS_PORTFOLIO_URL") or "http://tradingagents-portfolio:8000").rstrip("/")
TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "America/New_York")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://trading.txferguson.net").rstrip("/")


def job_nightly_scan() -> None:
    log.info("[nightly_scan] firing portfolio scan at %s", PORTFOLIO_URL)
    try:
        r = httpx.post(f"{PORTFOLIO_URL}/api/portfolio-scan", timeout=60)
        log.info("[nightly_scan] response %s: %s", r.status_code, r.text[:400])
    except Exception as exc:
        log.exception("[nightly_scan] failed: %s", exc)
        default_notifier().send(
            "⚠️ Nightly portfolio scan failed to start.",
            link=DASHBOARD_URL,
        )


def _latest_scan_for_today() -> dict[str, Any] | None:
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
    log.info("[newsletter] morning newsletter job firing")
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
    try:
        r = httpx.get(f"{API_URL}/api/auth/schwab/status", timeout=15)
        if r.status_code != 200:
            log.warning("[token_health] status %s: %s", r.status_code, r.text[:200])
            return
        data = r.json()
    except Exception:
        log.exception("[token_health] failed to reach api")
        return
    if not data.get("connected"):
        log.info("[token_health] Schwab not connected — skipping nag")
        return
    days = data.get("days_until_refresh_expires")
    log.info("[token_health] connected, %s days until refresh expires", days)
    if days is None:
        return
    if int(days) < 1:
        default_notifier().send(
            f"⚠️ Schwab refresh token expires within 24h ({days} days left). Re-auth now to keep nightly scans running.",
            link=f"{DASHBOARD_URL}/#schwab",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send-newsletter-now", action="store_true", help="Run newsletter once and exit")
    parser.add_argument("--run-scan-now", action="store_true", help="Trigger scan once and exit")
    args = parser.parse_args()

    if args.send_newsletter_now:
        job_morning_newsletter()
        return
    if args.run_scan_now:
        job_nightly_scan()
        return

    sched = BlockingScheduler(timezone=TIMEZONE)
    sched.add_job(
        job_nightly_scan,
        CronTrigger(hour=22, minute=0, timezone=TIMEZONE),
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
    log.info("Scheduler starting (tz=%s)", TIMEZONE)
    log.info(" - nightly_scan       cron 22:00 %s", TIMEZONE)
    log.info(" - morning_newsletter cron 05:00 %s", TIMEZONE)
    log.info(" - token_health       every 1h")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Scheduler shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
