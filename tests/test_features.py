"""Tests for web/features.py — the tier feature-gate module."""
from __future__ import annotations

import importlib

from web import features


def _reload(monkeypatch, **env):
    monkeypatch.delenv("TIER", raising=False)
    monkeypatch.delenv("FEATURES", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(features)


class TestTierDefault:
    def test_no_env_uses_default_tier_4(self, monkeypatch):
        f = _reload(monkeypatch)
        assert f.DEFAULT_TIER == 4
        assert f.enabled("schwab")
        assert f.enabled("sp500")
        assert f.enabled("options")


class TestTierEnv:
    def test_tier_1_has_no_features(self, monkeypatch):
        f = _reload(monkeypatch, TIER="1")
        assert not f.enabled("schwab")
        assert not f.enabled("sp500")
        assert not f.enabled("options")

    def test_tier_2_has_schwab_only(self, monkeypatch):
        f = _reload(monkeypatch, TIER="2")
        assert f.enabled("schwab")
        assert not f.enabled("sp500")
        assert not f.enabled("options")

    def test_tier_3_has_schwab_and_sp500(self, monkeypatch):
        f = _reload(monkeypatch, TIER="3")
        assert f.enabled("schwab")
        assert f.enabled("sp500")
        assert not f.enabled("options")

    def test_tier_4_has_everything(self, monkeypatch):
        f = _reload(monkeypatch, TIER="4")
        assert f.enabled("schwab")
        assert f.enabled("sp500")
        assert f.enabled("options")

    def test_unknown_tier_falls_back_to_default(self, monkeypatch):
        f = _reload(monkeypatch, TIER="99")
        assert f.enabled("options")  # DEFAULT_TIER == 4

    def test_non_numeric_tier_falls_back_to_default(self, monkeypatch):
        f = _reload(monkeypatch, TIER="bogus")
        assert f.enabled("options")


class TestFeaturesEnvOverride:
    def test_features_env_wins_over_tier(self, monkeypatch):
        f = _reload(monkeypatch, TIER="4", FEATURES="schwab")
        assert f.enabled("schwab")
        assert not f.enabled("sp500")
        assert not f.enabled("options")

    def test_features_env_empty_string_means_no_features(self, monkeypatch):
        f = _reload(monkeypatch, TIER="4", FEATURES="")
        assert not f.enabled("schwab")

    def test_features_env_whitespace_tolerant(self, monkeypatch):
        f = _reload(monkeypatch, FEATURES=" schwab , sp500 ")
        assert f.enabled("schwab")
        assert f.enabled("sp500")
        assert not f.enabled("options")
