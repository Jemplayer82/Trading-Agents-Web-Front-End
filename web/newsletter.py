"""Render the overnight portfolio scan as an HTML email and SMTP-send it.

Called by the scheduler's 5am job (web/scheduler.py:job_morning_newsletter),
which records the returned Message-ID on the scan row for audit. All
configuration is env vars (SMTP_*, NEWSLETTER_*) — editable
live from the dashboard Settings UI via web/credentials.py SETTINGS_REGISTRY,
and read at call time here so changes apply without a restart. The body
template is web/templates/newsletter.html; report markdown is converted to
HTML through the `mdhtml` Jinja filter registered below.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Any

import markdown as md_lib
from jinja2 import Environment, FileSystemLoader, select_autoescape

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _badge_color(signal: str) -> str:
    return {"BUY": "#2ecc71", "SELL": "#ff7c7c", "HOLD": "#f4c95d"}.get(
        (signal or "").upper(), "#6b7d8f"
    )


def _markdown_to_html(text: str) -> str:
    if not text:
        return ""
    try:
        return md_lib.markdown(text, extensions=["fenced_code", "tables"])
    except Exception:
        return f"<pre>{text}</pre>"


def _excerpt(text: str, n: int = 280) -> str:
    if not text:
        return ""
    text = text.replace("\n\n", " ").strip()
    if len(text) <= n:
        return text
    return text[:n].rsplit(" ", 1)[0] + "…"


_env.filters["mdhtml"] = _markdown_to_html
_env.filters["badgecolor"] = _badge_color
_env.filters["excerpt"] = _excerpt


def render(scan: dict[str, Any]) -> tuple[str, str]:
    """Returns (subject, html_body)."""
    dashboard_url = os.environ.get("DASHBOARD_URL", "https://trading.txferguson.net").rstrip("/")
    counts = scan.get("signal_counts") or {}
    n = scan.get("num_tickers") or 0
    date_str = (scan.get("trade_date") or scan.get("created_at") or "")[:10]
    subject = (
        f"Portfolio Briefing · {date_str} · {n} positions · "
        f"{counts.get('BUY', 0)} BUY / {counts.get('HOLD', 0)} HOLD / {counts.get('SELL', 0)} SELL"
    )
    html = _env.get_template("newsletter.html").render(
        scan=scan,
        counts=counts,
        date_str=date_str,
        dashboard_url=dashboard_url,
        tickers=scan.get("tickers") or [],
    )
    return subject, html


def _smtp_send(subject: str, html: str) -> str | None:
    """Build an HTML email and SMTP-send it. Returns Message-ID, or None.

    Shared transport for both the newsletter (send) and failure alerts
    (send_alert). Reads SMTP_*/NEWSLETTER_* at call time so dashboard-saved
    config applies without a restart; returns None (logged) when SMTP isn't
    configured, so callers degrade gracefully on deployments with no mail set up.
    """
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    sender = os.environ.get("NEWSLETTER_FROM") or user
    recipient = os.environ.get("NEWSLETTER_TO")
    if not (host and user and password and recipient):
        log.warning("[newsletter] SMTP env missing — skipping send")
        return None
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="tradingagents")
    msg.set_content("This email is HTML — please use an HTML-capable client.")
    msg.add_alternative(html, subtype="html")
    try:
        # Port 465 means implicit TLS from the first byte; anything else
        # (587/25) is assumed to be a plaintext connection upgraded via STARTTLS.
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
                s.login(user, password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                s.login(user, password)
                s.send_message(msg)
        log.info("[newsletter] sent: %s", subject)
        return msg["Message-ID"]
    except Exception as exc:
        # log.exception records the traceback but NOT local variables, so
        # SMTP_PASS can't leak into the logs. Keep it that way.
        log.exception("[newsletter] SMTP send failed: %s", exc)
        return None


def send(scan: dict[str, Any]) -> str | None:
    """Render the overnight scan and SMTP-send it. Returns Message-ID or None."""
    subject, html = render(scan)
    return _smtp_send(subject, html)


def send_alert(subject: str, html: str) -> str | None:
    """SMTP-send a pre-rendered alert email (e.g. a run-failure notice).

    Thin wrapper over the shared transport so web/alerts.py can reach email
    without knowing anything about SMTP. Returns None when mail isn't configured.
    """
    return _smtp_send(subject, html)
