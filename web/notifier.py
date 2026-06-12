"""Pluggable webhook notifier — Fred (OpenClaw runner on the WebServer)
is responsible for actually delivering the WhatsApp message.

Notifications must never crash the caller, so all errors are logged and
swallowed.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def send(self, text: str, link: str | None = None) -> None:
        raise NotImplementedError


class NoOpNotifier(Notifier):
    def send(self, text: str, link: str | None = None) -> None:
        log.info("[notifier:noop] %s | link=%s", text, link)


class HttpPostNotifier(Notifier):
    """POST JSON `{"text": ..., "link": ...}` to FRED_NOTIFY_URL."""

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout

    def send(self, text: str, link: str | None = None) -> None:
        payload: dict[str, str] = {"text": text}
        if link:
            payload["link"] = link
        try:
            r = httpx.post(self.url, json=payload, timeout=self.timeout)
            r.raise_for_status()
            log.info("[notifier] sent: %s", text[:80])
        except Exception as exc:
            log.warning("[notifier] failed POST %s: %s", self.url, exc)


def default_notifier() -> Notifier:
    """Construct the notifier from env. Returns NoOp if FRED_NOTIFY_URL is empty."""
    url = (os.environ.get("FRED_NOTIFY_URL") or "").strip()
    if not url:
        return NoOpNotifier()
    return HttpPostNotifier(url)
