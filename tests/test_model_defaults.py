"""Provider-aware model defaults (web.runner.build_config / _catalog_default).

Regression cover for the production bug where empty preferences + a non-OpenAI
provider still yielded an OpenAI model name. Pure T1 — touches only
web.runner and web.providers, no spy_scanner/options_engine dependency, so
this stays in every tier (unlike test_scan_health_guard.py, which needs
spy_scanner and is T3+ only).
"""

import pytest

from web import runner

pytestmark = pytest.mark.unit


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
