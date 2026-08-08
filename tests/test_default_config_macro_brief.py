import importlib

import pytest

import tradingagents.default_config as default_config_module
from tradingagents.default_config import _ENV_OVERRIDES, DEFAULT_CONFIG

pytestmark = pytest.mark.unit


def test_macro_brief_enabled_defaults_true():
    assert DEFAULT_CONFIG["macro_brief_enabled"] is True


def test_macro_brief_enabled_has_env_override_row():
    assert _ENV_OVERRIDES["TRADINGAGENTS_MACRO_BRIEF_ENABLED"] == "macro_brief_enabled"


def test_macro_brief_key_absent_from_default_config():
    """config.get('macro_brief') must default to None for every caller that
    doesn't explicitly set it — this is what keeps interactive single-ticker
    Run Analysis unaffected by the scan-only feature."""
    assert "macro_brief" not in DEFAULT_CONFIG
    assert DEFAULT_CONFIG.get("macro_brief") is None


def test_macro_brief_enabled_env_var_kill_switch(monkeypatch):
    """TRADINGAGENTS_MACRO_BRIEF_ENABLED must actually coerce to False from
    the environment — the env-only rollback path is the flag's core purpose."""
    try:
        monkeypatch.setenv("TRADINGAGENTS_MACRO_BRIEF_ENABLED", "false")
        reloaded = importlib.reload(default_config_module)
        assert reloaded.DEFAULT_CONFIG["macro_brief_enabled"] is False
    finally:
        # Leave the module in its default state for other tests in this process.
        monkeypatch.delenv("TRADINGAGENTS_MACRO_BRIEF_ENABLED", raising=False)
        importlib.reload(default_config_module)
