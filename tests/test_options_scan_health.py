"""Options-scan health guards that depend on web.options_engine (tier 4 only).

Split out of test_scan_health_guard.py, which needs spy_scanner (tier 3+) and
is itself deleted below tier 3 by the tier strip — this file additionally
needs options_engine, so it's deleted below tier 4.
"""

import pytest

from web import options_engine

pytestmark = pytest.mark.unit

MODEL_404 = 'Error code: 404 - {"error": {"message": "model \\"gpt-5.4-mini\\" not found"}}'


def _row(ticker, signal="BUY", conviction=8, error=None):
    row = {"ticker": ticker, "signal": signal, "conviction": conviction}
    if error:
        # Mirrors _quick_scan_one's catch-all: degraded scores + an error key.
        row.update({"signal": "HOLD", "conviction": 1, "error": error})
    return row


# ── never trade on a crashed analysis ────────────────────────────────────────

def test_failed_dives_are_not_vetted(monkeypatch):
    """A failed dive keeps its BUY/conviction from the quick scan, so without
    filtering it would reach the allocator and open real positions."""
    seen = {}

    def fake_fetch(signals, **kw):
        seen["rows"] = list(signals)
        return [], []

    monkeypatch.setattr(options_engine.options_data, "fetch_candidates", fake_fetch)
    enriched = [_row("AAPL", error="deep model 410"),
                _row("MSFT", error="deep model 410"),
                _row("NVDA", error="deep model 410")]
    # The filter as applied in run_options_build.
    usable = [e for e in enriched if not e.get("error")]
    options_engine.options_data.fetch_candidates(usable)
    assert seen["rows"] == [], "crashed analyses must never be vetted into contracts"


# ── zero-candidate explanations ──────────────────────────────────────────────

def test_reason_none_when_candidates_exist():
    assert options_engine._zero_candidate_reason([], [], [], [], [{"occ_symbol": "X"}]) is None


def test_reason_all_hold():
    quick = [_row(f"T{i}", signal="HOLD") for i in range(10)]
    msg = options_engine._zero_candidate_reason(quick, [], [], [], [])
    assert "BUY or SELL" in msg


def test_reason_all_dives_failed_is_not_blamed_on_vetting():
    """The failure mode the feature exists to surface must not be reported as
    'nothing passed vetting'."""
    quick = [_row("AAPL")]
    enriched = [_row("AAPL", error="boom")]
    msg = options_engine._zero_candidate_reason(quick, quick, enriched, [], [])
    assert "All 1 deep dives failed" in msg
    assert "vetting" not in msg.lower()


def test_reason_vetting_when_dives_usable():
    quick = [_row("AAPL")]
    msg = options_engine._zero_candidate_reason(quick, quick, quick, quick, [])
    assert "vetting" in msg.lower()
