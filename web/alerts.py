"""Out-of-band failure alerts: tell the user when a run dies.

A failed run (single-ticker analysis, portfolio scan, or S&P 500 scan) already
writes status='failed' + an error to the DB, but that's invisible unless someone
is watching the dashboard. notify_run_failed() pushes the failure over BOTH
configured channels — the webhook (FRED_NOTIFY_URL, via web/notifier.py) and
email (SMTP, via web/newsletter.py).

Design:
- Best-effort and non-blocking: the sends run on a daemon thread and each channel
  is independently guarded, so an alert can never raise into — or stall — a
  failing run's teardown (SMTP carries a 30s timeout).
- Each channel no-ops cleanly when unconfigured (NoOp notifier / SMTP env unset),
  so this is safe on deployments with neither set up.

Callers: the three failure paths (web/runner.py, web/portfolio_main.py) and the
stuck-run reaper (web/scheduler.py). All three service containers apply
credentials/settings to env at startup, so both channels resolve everywhere.
"""
from __future__ import annotations

import html
import logging
import os
import threading

from . import newsletter
from .notifier import default_notifier

log = logging.getLogger(__name__)

_ERROR_MAX = 1500  # cap error text in the alert; the full detail stays in the DB row


def _dashboard_url() -> str:
    return os.environ.get("DASHBOARD_URL", "https://trading.txferguson.net").rstrip("/")


def _send_webhook(summary: str, detail: str, link: str) -> None:
    try:
        text = f"{summary}\n{detail}" if detail else summary
        default_notifier().send(text, link=link)
    except Exception:
        log.exception("[alerts] webhook channel failed")


def _send_email(summary: str, detail: str, link: str) -> None:
    try:
        # Everything is escaped — error text is attacker-influenceable (e.g. a
        # ticker or upstream message embedded in the exception).
        body = (
            f'<p style="font-size:15px;">{html.escape(summary)}</p>'
            f'<pre style="white-space:pre-wrap;font-size:12px;color:#444;'
            f'border-left:3px solid #ff7c7c;padding-left:10px;">{html.escape(detail)}</pre>'
            f'<p><a href="{html.escape(link)}">Open the dashboard</a></p>'
        )
        newsletter.send_alert(summary, body)
    except Exception:
        log.exception("[alerts] email channel failed")


def _deliver(summary: str, detail: str, link: str) -> None:
    """Synchronous best-effort delivery to every channel. Never raises."""
    _send_webhook(summary, detail, link)
    _send_email(summary, detail, link)


def notify_run_failed(
    *, kind: str, run_id: int | str, label: str, error: str, link: str | None = None
) -> threading.Thread:
    """Alert the user that a run failed, over webhook + email. Never raises.

    kind   human label for the run type, e.g. "Portfolio scan".
    run_id the DB id of the failed run.
    label  what the run was about (ticker, trade date, ...).
    error  the failure message (truncated for the alert; the DB keeps full text).
    link   deep link; defaults to DASHBOARD_URL.

    Returns the delivery thread (already started) so callers/tests can join it;
    normal callers ignore it.
    """
    summary = f"⚠️ {kind} #{run_id} failed: {label}".strip()
    detail = (error or "").strip()[:_ERROR_MAX]
    target = link or _dashboard_url()
    t = threading.Thread(
        target=_deliver, args=(summary, detail, target), name="run-failure-alert", daemon=True
    )
    t.start()
    return t
