"""Guards against the two dependency regressions that broke the deployed image.

Both were silent because the Docker image installs via `pip install ".[web]"`
(Dockerfile), which ignores uv.lock and re-resolves pyproject's pins fresh at
build time. A rebuild then drifted from what CI tests:

1. pandas resolved past 2.x to 3.0 (only pin was `pandas>=2.3.0`, no ceiling).
   pandas 3.0 tightened tz-aware/naive datetime comparisons, breaking every
   stockstats indicator calc and the chart-data endpoint. stockstats 0.6.5 is
   not pandas-3 compatible.
2. lxml vanished — it was only present transitively via `parsel`, which was
   dropped in a dependency cleanup. `pd.read_html()` in web/spy_tickers.py
   (the S&P 500 constituents fetch) hard-fails without it.

These assert the installed environment, so they catch the drift wherever the
suite runs — including inside the built image.
"""
from __future__ import annotations

import importlib.util

import pandas as pd


def test_pandas_is_v2() -> None:
    """stockstats 0.6.5 + our tz-aware indicator code require pandas 2.x."""
    major = int(pd.__version__.split(".")[0])
    assert major == 2, (
        f"pandas {pd.__version__} — must stay on 2.x (pin `pandas>=2.3.0,<3`). "
        "pandas 3.0 breaks tz-aware datetime comparisons in stockstats indicators."
    )


def test_lxml_is_installed() -> None:
    """web/spy_tickers.py fetches the S&P 500 list via pandas.read_html, needs lxml."""
    assert importlib.util.find_spec("lxml") is not None, (
        "lxml missing — pandas.read_html() (S&P 500 ticker fetch) fails without it. "
        "Declare `lxml` as a direct dependency in pyproject.toml."
    )
