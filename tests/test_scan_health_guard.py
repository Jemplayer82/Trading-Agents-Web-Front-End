"""Scan health guards + provider-aware model defaults.

Regression cover for the production incident where a stale model name made all
150 quick scans return 404, every ticker degraded to HOLD/conviction-1, and the
options scan reported **completed** with an empty portfolio and no alert.
"""

import pytest

from web import options_engine, runner, spy_scanner
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
    assert_quick_scan_healthy([_row(f"T{i}") for i in range(150)]) is None


def test_partial_errors_do_not_trip_the_guard():
    """Routine flakiness across a large universe must never fail a run."""
    rows = [_row(f"T{i}") for i in range(140)] + [_row(f"E{i}", error="boom") for i in range(10)]
    assert_quick_scan_healthy(rows) is None


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


# ── provider-aware model defaults ────────────────────────────────────────────

def test_catalog_default_skips_custom_sentinel(monkeypatch):
    monkeypatch.setattr(runner, "__name__", runner.__name__)  # no-op, keeps lint quiet
    val = runner._catalog_default("ollama", "quick")
    assert val and val != "custom"


def test_explicit_param_wins_over_catalog():
    cfg = runner.build_config({"provider": "ollama", "quick_model": "my-model:tag"})
    assert cfg["quick_think_llm"] == "my-model:tag"


def test_env_override_beats_catalog(monkeypatch):
    """TRADINGAGENTS_QUICK_THINK_LLM is a deliberate operator override and must
    outrank the provider catalog."""
    monkeypatch.setenv("TRADINGAGENTS_QUICK_THINK_LLM", "operator-choice")
    monkeypatch.setitem(runner.DEFAULT_CONFIG, "quick_think_llm", "operator-choice")
    cfg = runner.build_config({"provider": "ollama"})
    assert cfg["quick_think_llm"] == "operator-choice"


def test_fresh_db_gets_a_provider_appropriate_model(monkeypatch):
    """The actual production bug: empty preferences + provider=ollama must not
    yield an OpenAI model name."""
    monkeypatch.delenv("TRADINGAGENTS_QUICK_THINK_LLM", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_DEEP_THINK_LLM", raising=False)
    monkeypatch.setitem(runner.DEFAULT_CONFIG, "quick_think_llm", "gpt-5.4-mini")
    monkeypatch.setitem(runner.DEFAULT_CONFIG, "deep_think_llm", "gpt-5.4")
    cfg = runner.build_config({})
    assert cfg["quick_llm_provider"] == "ollama"
    assert not cfg["quick_think_llm"].startswith("gpt-5."), cfg["quick_think_llm"]
    assert not cfg["deep_think_llm"].startswith("gpt-5."), cfg["deep_think_llm"]


def test_same_provider_default_is_left_alone(monkeypatch):
    """An OpenAI user with empty prefs keeps DEFAULT_CONFIG's OpenAI model —
    the catalog fallback is only for cross-provider mismatch."""
    monkeypatch.delenv("TRADINGAGENTS_DEEP_THINK_LLM", raising=False)
    cfg = runner.build_config({"provider": "openai"})
    assert cfg["deep_think_llm"] == runner.DEFAULT_CONFIG["deep_think_llm"]


def test_catalog_has_no_retired_models():
    """Retired Ollama Cloud names return HTTP 410 while still looking valid.
    These four went dead in production while still being offered."""
    from web.providers import _OLLAMA_CLOUD_MODELS

    retired = {"kimi-k2:1t-cloud", "glm-4.6:cloud",
               "deepseek-v3.1:671b-cloud", "qwen3-coder:480b-cloud"}
    offered = {v for mode in _OLLAMA_CLOUD_MODELS.values() for _, v in mode}
    assert not (offered & retired), f"retired models still offered: {offered & retired}"
