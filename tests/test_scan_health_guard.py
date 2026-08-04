"""Scan health guards (web.spy_scanner) — tier 3+ only.

Regression cover for the production incident where a stale model name made all
150 quick scans return 404, every ticker degraded to HOLD/conviction-1, and
the options scan reported **completed** with an empty portfolio and no alert.

Depends on web.spy_scanner (tier 3+), so this file is deleted below tier 3 by
the tier strip. See test_model_defaults.py for the (tier-1) provider-default
tests that used to live here, and test_options_scan_health.py for the
(tier-4) options_engine-dependent ones.
"""

import pytest

from web.spy_scanner import (
    ScanInfrastructureError,
    assert_deep_dives_healthy,
    assert_quick_scan_healthy,
)

pytestmark = pytest.mark.unit

MODEL_404 = 'Error code: 404 - {"error": {"message": "model \\"gpt-5.4-mini\\" not found"}}'


def _row(ticker, signal="BUY", conviction=8, error=None):
    row = {"ticker": ticker, "signal": signal, "conviction": conviction}
    if error:
        # Mirrors _quick_scan_one's catch-all: degraded scores + an error key.
        row.update({"signal": "HOLD", "conviction": 1, "error": error})
    return row


# ── quick-scan guard ─────────────────────────────────────────────────────────

def test_total_failure_raises_and_quotes_the_error():
    rows = [_row(f"T{i}", error=MODEL_404) for i in range(150)]
    with pytest.raises(ScanInfrastructureError) as exc:
        assert_quick_scan_healthy(rows)
    msg = str(exc.value)
    assert "150/150" in msg
    assert "gpt-5.4-mini" in msg, "alert must name the actual cause"


def test_healthy_scan_passes():
    assert assert_quick_scan_healthy([_row(f"T{i}") for i in range(150)]) is None


def test_partial_errors_do_not_trip_the_guard():
    """Routine flakiness across a large universe must never fail a run."""
    rows = [_row(f"T{i}") for i in range(140)] + [_row(f"E{i}", error="boom") for i in range(10)]
    assert assert_quick_scan_healthy(rows) is None


@pytest.mark.parametrize(
    "errored,total,should_raise",
    [(49, 100, False), (50, 100, True), (51, 100, True)],
)
def test_fifty_percent_boundary(errored, total, should_raise):
    rows = [_row(f"E{i}", error="boom") for i in range(errored)]
    rows += [_row(f"T{i}") for i in range(total - errored)]
    if should_raise:
        with pytest.raises(ScanInfrastructureError):
            assert_quick_scan_healthy(rows)
    else:
        assert assert_quick_scan_healthy(rows) is None


def test_missing_price_data_is_not_an_infrastructure_error():
    """_quick_scan_one returns HOLD/1 with NO error key when yfinance is short
    on data. Those must not count toward the failure rate."""
    rows = [{"ticker": f"T{i}", "signal": "HOLD", "conviction": 1,
             "reasoning": "Insufficient price data."} for i in range(150)]
    assert assert_quick_scan_healthy(rows) is None


def test_empty_results_pass():
    assert assert_quick_scan_healthy([]) is None


# ── deep-dive guard ──────────────────────────────────────────────────────────

def test_all_deep_dives_failed_raises():
    rows = [_row(f"T{i}", error=MODEL_404) for i in range(25)]
    with pytest.raises(ScanInfrastructureError) as exc:
        assert_deep_dives_healthy(rows)
    assert "all 25 deep dives failed" in str(exc.value)


def test_partial_deep_dive_failure_passes():
    """9-of-10 failing is survivable: these are full agent graphs and the
    caller already tolerates fewer candidates. Only 100% is infrastructure."""
    rows = [_row(f"E{i}", error="boom") for i in range(9)] + [_row("OK")]
    assert assert_deep_dives_healthy(rows) is None


def test_empty_enriched_passes():
    assert assert_deep_dives_healthy([]) is None
