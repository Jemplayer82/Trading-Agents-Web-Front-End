"""Bulk-delete endpoints served by web.portfolio_main (T2+ — the portfolio
container doesn't exist below tier 2, so this file is dropped by the tier
strip at T1). See test_clear_history.py for the web.main-served (T1)
counterparts and the shared db.py-level tests.
"""
from __future__ import annotations

import importlib

import pytest

from web import db, features, portfolio_main


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()


@pytest.mark.unit
class TestBulkDeletePortfolioEndpoints:
    @pytest.fixture()
    def portfolio_client(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "test-secret-token")  # gitleaks:allow pragma: allowlist secret
        # Force every feature on and reload: web.portfolio_main may already be
        # cached (imported by an earlier test) with whatever DEFAULT_TIER was
        # active at that point, which scripts/make_tier.py rewrites to 1/2/3
        # on a stripped tree — these endpoints (incl. the T3 /api/spy-scans
        # route below) must exist regardless of DEFAULT_TIER.
        monkeypatch.setenv("FEATURES", "schwab,sp500,options")
        importlib.reload(features)
        importlib.reload(portfolio_main)
        from fastapi.testclient import TestClient

        with TestClient(portfolio_main.app) as c:
            yield c
        monkeypatch.delenv("FEATURES", raising=False)
        importlib.reload(features)
        importlib.reload(portfolio_main)

    _HEADERS = {"x-internal-token": "test-secret-token"}  # pragma: allowlist secret

    def test_delete_all_portfolio_scans_endpoint(self, portfolio_client):
        db.create_portfolio_scan("2026-07-08")
        db.create_portfolio_scan("2026-07-07")

        resp = portfolio_client.delete("/api/portfolio-scans", headers=self._HEADERS)

        assert resp.status_code == 200
        assert resp.json()["status"] == "deleted"
        assert resp.json()["count"] == 2
        assert db.list_portfolio_scans() == []

    def test_delete_all_spy_scans_endpoint(self, portfolio_client):
        db.create_spy_scan("2026-07-08")

        resp = portfolio_client.delete("/api/spy-scans", headers=self._HEADERS)

        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted", "count": 1}
        assert db.list_spy_scans() == []
