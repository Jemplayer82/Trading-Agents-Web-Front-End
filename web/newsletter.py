"""Render the overnight portfolio scan as an HTML email and SMTP-send it.

Called by the scheduler's 5am job (web/scheduler.py:job_morning_newsletter),
which records the returned Message-ID on the scan row for audit. Rendering
config (NEWSLETTER_*) is editable live from the dashboard Settings UI via
web/credentials.py SETTINGS_REGISTRY, read at call time so changes apply
without a restart. The body template is web/templates/newsletter.html;
report markdown is converted to HTML through the `mdhtml` Jinja filter
registered below. The actual SMTP transport lives in web/mailer.py — this
module only renders and is a T2 (Schwab-portfolio) concern.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import markdown as md_lib
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import mailer

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


def send(scan: dict[str, Any]) -> str | None:
    """Render the overnight scan and SMTP-send it. Returns Message-ID or None."""
    subject, html = render(scan)
    return mailer.send_html(subject, html)
