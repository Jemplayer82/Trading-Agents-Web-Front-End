"""Tier-gating parity for the Schwab OAuth routes extracted to schwab_routes.py.

Locks in: web.main.app registers the 3 Schwab paths iff the "schwab" feature
is on for the active DEFAULT_TIER (4 on master, rewritten to 1/2/3 by
scripts/make_tier.py on a stripped tier tree); explicit TIER=1/TIER=2 always
behave as documented regardless of DEFAULT_TIER.
"""
from __future__ import annotations

import importlib

_SCHWAB_PATHS = {
    "/api/auth/schwab",
    "/api/auth/schwab/callback",
    "/api/auth/schwab/status",
}


def _app_paths(monkeypatch, **env):
    monkeypatch.delenv("TIER", raising=False)
    monkeypatch.delenv("FEATURES", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import web.features as features_module
    importlib.reload(features_module)
    import web.main as main_module
    importlib.reload(main_module)
    return {r.path for r in main_module.app.routes if hasattr(r, "path")}


class TestSchwabRouteGating:
    def test_default_tier_registers_schwab_paths_iff_enabled(self, monkeypatch):
        paths = _app_paths(monkeypatch)
        import web.features as features_module  # reloaded by _app_paths above
        if features_module.enabled("schwab"):
            assert _SCHWAB_PATHS <= paths
        else:
            assert not (paths & _SCHWAB_PATHS)

    def test_tier_1_registers_no_schwab_paths(self, monkeypatch):
        paths = _app_paths(monkeypatch, TIER="1")
        assert not (paths & _SCHWAB_PATHS)

    def test_tier_2_registers_schwab_paths(self, monkeypatch):
        paths = _app_paths(monkeypatch, TIER="2")
        assert _SCHWAB_PATHS <= paths
