"""One-click "Clear history" — bulk delete for each of the three tabs.

Each tab already supported per-item delete (DELETE .../{id}); these add a
bulk variant (DELETE with no id) mirroring the same db.py pattern. No FK
references INTO the analyses table, and clearing spy_scans intentionally
leaves deep-dive `analyses` rows behind — same accepted behavior as the
existing single-scan delete (see db.delete_spy_scan's docstring).
"""
from __future__ import annotations

import pytest

from web import db


@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()


@pytest.mark.unit
class TestDeleteAllAnalyses:
    def test_deletes_all_rows_and_returns_count(self):
        db.create_analysis({"ticker": "AAPL", "trade_date": "2026-07-08"})
        db.create_analysis({"ticker": "MSFT", "trade_date": "2026-07-08"})

        deleted = db.delete_all_analyses()

        assert deleted == 2
        assert db.list_analyses() == []

    def test_empty_table_returns_zero(self):
        assert db.delete_all_analyses() == 0


@pytest.mark.unit
class TestDeleteAllSpyScans:
    def test_deletes_all_scans_and_returns_count(self):
        db.create_spy_scan("2026-07-08")
        db.create_spy_scan("2026-07-07")

        deleted = db.delete_all_spy_scans()

        assert deleted == 2
        assert db.list_spy_scans() == []

    def test_keeps_deep_dive_analyses(self):
        """Same accepted behavior as the single-scan delete: clearing scans
        must not touch the analyses rows they created."""
        analysis_id = db.create_analysis({"ticker": "AAPL", "trade_date": "2026-07-08"})
        db.create_spy_scan("2026-07-08")

        db.delete_all_spy_scans()

        assert db.list_spy_scans() == []
        assert len(db.list_analyses()) == 1
        assert db.list_analyses()[0]["id"] == analysis_id


@pytest.mark.unit
class TestBulkDeleteEndpoints:
    @pytest.fixture()
    def api_client(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "test-secret-token")  # gitleaks:allow
        from fastapi.testclient import TestClient

        from web.main import app
        with TestClient(app) as c:
            yield c

    @pytest.fixture()
    def portfolio_client(self, monkeypatch):
        monkeypatch.setenv("INTERNAL_API_TOKEN", "test-secret-token")  # gitleaks:allow
        from fastapi.testclient import TestClient

        from web.portfolio_main import app
        with TestClient(app) as c:
            yield c

    _HEADERS = {"x-internal-token": "test-secret-token"}

    def test_delete_all_analyses_endpoint(self, api_client):
        db.create_analysis({"ticker": "AAPL", "trade_date": "2026-07-08"})
        db.create_analysis({"ticker": "MSFT", "trade_date": "2026-07-08"})

        resp = api_client.delete("/api/analyses", headers=self._HEADERS)

        assert resp.status_code == 200
        assert resp.json() == {"status": "deleted", "count": 2}
        assert db.list_analyses() == []

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

    def test_single_item_delete_route_still_works(self, api_client):
        """Guard against the zero-vs-one-path-segment routes colliding."""
        analysis_id = db.create_analysis({"ticker": "AAPL", "trade_date": "2026-07-08"})
        other_id = db.create_analysis({"ticker": "MSFT", "trade_date": "2026-07-08"})

        resp = api_client.delete(f"/api/analyses/{analysis_id}", headers=self._HEADERS)

        assert resp.status_code == 200
        remaining = [a["id"] for a in db.list_analyses()]
        assert remaining == [other_id]
