import pytest

from tradingagents.default_config import DEFAULT_CONFIG, _ENV_OVERRIDES

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
