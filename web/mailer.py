"""Generic SMTP transport, shared by the newsletter and by out-of-band alerts.

Split out of web/newsletter.py so the T1 (analysis-only) failure-alert path
(web/alerts.py) doesn't need to pull in the T2 portfolio-newsletter renderer
to send a plain email. All configuration is env vars (SMTP_*, NEWSLETTER_*),
read at call time so dashboard-saved settings apply without a restart.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

log = logging.getLogger(__name__)


def send_html(subject: str, html: str) -> str | None:
    """Build an HTML email and SMTP-send it. Returns Message-ID, or None.

    Shared transport for both the newsletter (newsletter.send) and failure
    alerts (send_alert). Returns None (logged) when SMTP isn't configured,
    so callers degrade gracefully on deployments with no mail set up.
    """
    host = os.environ.get("SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")  # pragma: allowlist secret
    sender = os.environ.get("NEWSLETTER_FROM") or user
    recipient = os.environ.get("NEWSLETTER_TO")
    if not (host and user and password and recipient):
        log.warning("[mailer] SMTP env missing — skipping send")
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
        log.info("[mailer] sent: %s", subject)
        return msg["Message-ID"]
    except Exception as exc:
        # log.exception records the traceback but NOT local variables, so
        # SMTP_PASS can't leak into the logs. Keep it that way.
        log.exception("[mailer] SMTP send failed: %s", exc)
        return None


def send_alert(subject: str, html: str) -> str | None:
    """SMTP-send a pre-rendered alert email (e.g. a run-failure notice).

    Thin wrapper over send_html so web/alerts.py can reach email without
    knowing anything about SMTP. Returns None when mail isn't configured.
    """
    return send_html(subject, html)
