"""S&P 500 ticker list, fetched from Wikipedia and cached for 24h."""
from __future__ import annotations

import logging
import time
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)

_CACHE: Optional[list[str]] = None
_CACHE_TS: float = 0.0
_TTL = 86400  # 24 hours


def get_sp500_tickers() -> list[str]:
    global _CACHE, _CACHE_TS
    if _CACHE and (time.time() - _CACHE_TS) < _TTL:
        return _CACHE
    try:
        df = pd.read_html("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")[0]
        # yfinance uses hyphens; Wikipedia uses dots (BRK.B → BRK-B)
        tickers = df["Symbol"].str.replace(".", "-", regex=False).tolist()
        _CACHE = tickers
        _CACHE_TS = time.time()
        log.info("Fetched %d S&P 500 tickers from Wikipedia", len(tickers))
        return tickers
    except Exception as exc:
        log.exception("Failed to fetch S&P 500 tickers: %s", exc)
        if _CACHE:
            log.warning("Using stale ticker cache (%d tickers)", len(_CACHE))
            return _CACHE
        raise
