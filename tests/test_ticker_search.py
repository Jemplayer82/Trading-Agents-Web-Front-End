"""Tests for the GET /api/ticker-search endpoint (company-name -> symbol).

Auth is bypassed with the X-Internal-Token header (INTERNAL_API_TOKEN set in the
env), matching the pattern in test_bus_bridge.py. The upstream Yahoo Finance call
is monkeypatched via httpx.get so the suite stays offline.
"""
from __future__ import annotations

import pytest


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "test-secret-token")
    from fastapi.testclient import TestClient

    from web.main import app
    with TestClient(app) as c:
        yield c


_HEADERS = {"x-internal-token": "test-secret-token"}


def _yahoo(quotes):
    """Build a fake Yahoo search payload and a patched httpx.get returning it."""
    calls = []

    def fake_get(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return FakeResp({"quotes": quotes})

    return fake_get, calls


@pytest.mark.unit
def test_maps_equity_and_etf_only(client, monkeypatch):
    fake_get, _ = _yahoo([
        {"symbol": "AAPL", "quoteType": "EQUITY", "longname": "Apple Inc.", "exchDisp": "NASDAQ"},
        {"symbol": "SPY", "quoteType": "ETF", "shortname": "SPDR S&P 500", "exchDisp": "NYSEArca"},
        {"symbol": "^GSPC", "quoteType": "INDEX", "shortname": "S&P 500", "exchDisp": "SNP"},
    ])
    monkeypatch.setattr("httpx.get", fake_get)

    resp = client.get("/api/ticker-search?q=apple", headers=_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    syms = [d["symbol"] for d in data]
    assert syms == ["AAPL", "SPY"]  # INDEX dropped
    assert data[0] == {"symbol": "AAPL", "name": "Apple Inc.", "exchange": "NASDAQ"}


@pytest.mark.unit
def test_drops_symbols_failing_validation(client, monkeypatch):
    # "EVIL!" contains a char outside the safe_ticker_component charset -> dropped,
    # even though Yahoo tagged it EQUITY.
    fake_get, _ = _yahoo([
        {"symbol": "EVIL!", "quoteType": "EQUITY", "longname": "Bad Co", "exchDisp": "X"},
        {"symbol": "MSFT", "quoteType": "EQUITY", "longname": "Microsoft", "exchDisp": "NASDAQ"},
    ])
    monkeypatch.setattr("httpx.get", fake_get)

    data = client.get("/api/ticker-search?q=micro", headers=_HEADERS).json()
    assert [d["symbol"] for d in data] == ["MSFT"]


@pytest.mark.unit
def test_short_query_returns_empty_without_calling_yahoo(client, monkeypatch):
    fake_get, calls = _yahoo([{"symbol": "A", "quoteType": "EQUITY"}])
    monkeypatch.setattr("httpx.get", fake_get)

    data = client.get("/api/ticker-search?q=a", headers=_HEADERS).json()
    assert data == []
    assert calls == []  # never hit the network for a 1-char query


@pytest.mark.unit
def test_upstream_error_returns_empty(client, monkeypatch):
    def boom(url, **kwargs):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr("httpx.get", boom)

    resp = client.get("/api/ticker-search?q=apple", headers=_HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []
