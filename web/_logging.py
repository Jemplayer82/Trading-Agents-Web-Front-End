"""Shared logging configuration for the standalone web processes.

The portfolio API (``web.portfolio_main``) and the scheduler daemon
(``web.scheduler``) are launched directly (not behind uvicorn's logging
config), so each needs to initialise the root logger once at startup. This
helper gives them an identical, timestamped format instead of two slightly
different ``logging.basicConfig`` calls.

The main API (``web.main``) runs under uvicorn, which configures logging
itself, so it does not call this.
"""

from __future__ import annotations

import logging

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Initialise root logging with the shared format.

    A thin wrapper over ``logging.basicConfig`` (so it is a no-op if the root
    logger already has handlers, e.g. when something configured logging first).
    """
    logging.basicConfig(level=level, format=_LOG_FORMAT)
