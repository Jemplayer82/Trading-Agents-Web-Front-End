"""Unit tests for web/brokerages.py — OCC parser, SchwabProvider, registry.

No network access. All tests run with `uv run pytest tests/test_brokerages.py -v`.
"""
from __future__ import annotations

import pytest

from web import brokerages
from web.brokerages import SchwabProvider, parse_occ_symbol

# ---------------------------------------------------------------------------
# 1. parse_occ_symbol
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseOccSymbol:
    def test_standard_call(self):
        r = parse_occ_symbol("AAPL  250117C00150000")
        assert r == {
            "underlying": "AAPL",
            "expiration_date": "2025-01-17",
            "put_call": "CALL",
            "strike": 150.0,
        }

    def test_put(self):
        r = parse_occ_symbol("TSLA  260620P00200000")
        assert r["put_call"] == "PUT"
        assert r["underlying"] == "TSLA"
        assert r["expiration_date"] == "2026-06-20"

    def test_fractional_strike(self):
        r = parse_occ_symbol("F     250117C00012500")
        assert r["strike"] == 12.5
        assert r["underlying"] == "F"

    def test_root_with_digits(self):
        r = parse_occ_symbol("BRK B 250117C00400000")
        assert r["underlying"] == "BRK B"
        assert r["strike"] == 400.0

    def test_trailing_whitespace_tolerated(self):
        r = parse_occ_symbol("AAPL  250117C00150000  ")
        assert r is not None
        assert r["underlying"] == "AAPL"

    def test_plain_equity_rejected(self):
        assert parse_occ_symbol("AAPL") is None

    def test_too_short_rejected(self):
        assert parse_occ_symbol("250117C00150000") is None

    def test_bad_month_rejected(self):
        assert parse_occ_symbol("AAPL  251317C00150000") is None

    def test_bad_tail_rejected(self):
        assert parse_occ_symbol("AAPL  250117X00150000") is None

    def test_empty_and_none_safe(self):
        assert parse_occ_symbol("") is None


# ---------------------------------------------------------------------------
# 2. SchwabProvider normalization
# ---------------------------------------------------------------------------

EQUITY_POS = {
    "instrument": {"symbol": "NVDA", "assetType": "EQUITY"},
    "longQuantity": 20,
    "shortQuantity": 0,
    "averagePrice": 116.68,
    "marketValue": 2693.60,
}

OPTION_POS = {
    "instrument": {
        "symbol": "NVDA  260117C00140000",
        "assetType": "OPTION",
        "putCall": "CALL",
        "underlyingSymbol": "NVDA",
    },
    "longQuantity": 2,
    "shortQuantity": 0,
    "averagePrice": 6.40,
    "marketValue": 1690.00,
}

FIXTURE = [
    {
        "securitiesAccount": {
            "accountNumber": "83061234",
            "type": "CASH",
            "positions": [EQUITY_POS, OPTION_POS],
            "currentBalances": {"liquidationValue": 5000.0, "cashBalance": 616.40},
        }
    }
]


@pytest.mark.unit
class TestSchwabProvider:
    def _accounts(self, monkeypatch, fixture):
        monkeypatch.setattr(brokerages.schwab_mcp, "get_accounts", lambda **kw: fixture)
        return SchwabProvider().fetch_accounts()

    def test_account_identity(self, monkeypatch):
        acct = self._accounts(monkeypatch, FIXTURE)[0]
        assert acct["id"] == "schwab:83061234"
        assert acct["brokerage"] == "schwab"
        assert acct["label"] == "SCHWAB ••1234"

    def test_equity_math(self, monkeypatch):
        acct = self._accounts(monkeypatch, FIXTURE)[0]
        pos = next(p for p in acct["positions"] if p["symbol"] == "NVDA")
        assert pos["asset_type"] == "EQUITY"
        assert pos["multiplier"] == 1
        assert pos["shares"] == 20
        assert pos["cost_basis"] == round(116.68 * 20, 2)
        assert pos["current_price"] == round(2693.60 / 20, 4)
        assert pos["gain_dollars"] == round(2693.60 - 116.68 * 20, 2)
        assert pos["expiration_date"] is None
        assert pos["display_symbol"] == "NVDA"

    def test_option_math_uses_multiplier(self, monkeypatch):
        acct = self._accounts(monkeypatch, FIXTURE)[0]
        pos = next(p for p in acct["positions"] if p["asset_type"] == "OPTION")
        assert pos["multiplier"] == 100
        # cost = premium * contracts * 100
        assert pos["cost_basis"] == round(6.40 * 2 * 100, 2)
        # per-share price = market value / (contracts * 100)
        assert pos["current_price"] == round(1690.00 / 200, 4)
        assert pos["gain_dollars"] == round(1690.00 - 1280.00, 2)

    def test_option_fields_parsed(self, monkeypatch):
        acct = self._accounts(monkeypatch, FIXTURE)[0]
        pos = next(p for p in acct["positions"] if p["asset_type"] == "OPTION")
        assert pos["expiration_date"] == "2026-01-17"
        assert pos["strike"] == 140.0
        assert pos["put_call"] == "CALL"
        assert pos["underlying"] == "NVDA"
        assert pos["display_symbol"] == "NVDA 140C"

    def test_option_instrument_fallback_when_occ_unparseable(self, monkeypatch):
        weird = {
            "securitiesAccount": {
                "accountNumber": "83061234",
                "positions": [{
                    "instrument": {
                        "symbol": "WEIRD-OPT",
                        "assetType": "OPTION",
                        "putCall": "PUT",
                        "underlyingSymbol": "WEIRD",
                    },
                    "longQuantity": 1,
                    "averagePrice": 1.0,
                    "marketValue": 120.0,
                }],
            }
        }
        acct = self._accounts(monkeypatch, [weird])[0]
        pos = acct["positions"][0]
        assert pos["expiration_date"] is None
        assert pos["put_call"] == "PUT"
        assert pos["underlying"] == "WEIRD"
        assert pos["multiplier"] == 100

    def test_balance_fallback_initial(self, monkeypatch):
        fixture = [{
            "securitiesAccount": {
                "accountNumber": "555512349999",
                "positions": [EQUITY_POS],
                "initialBalances": {"accountValue": 3000.0, "totalCash": 100.0},
            }
        }]
        acct = self._accounts(monkeypatch, fixture)[0]
        assert acct["total_value"] == 3000.0
        assert acct["cash"] == 100.0
        assert acct["label"] == "SCHWAB ••9999"

    def test_none_returns_empty(self, monkeypatch):
        assert self._accounts(monkeypatch, None) == []

    def test_empty_list_returns_empty(self, monkeypatch):
        assert self._accounts(monkeypatch, []) == []

    def test_zero_qty_positions_skipped(self, monkeypatch):
        fixture = [{
            "securitiesAccount": {
                "accountNumber": "83061234",
                "positions": [{**EQUITY_POS, "longQuantity": 0}],
            }
        }]
        acct = self._accounts(monkeypatch, fixture)[0]
        assert acct["positions"] == []


# ---------------------------------------------------------------------------
# 3. Registry / fetch_all_accounts
# ---------------------------------------------------------------------------


class _BoomProvider(brokerages.BrokerageProvider):
    name = "boom"

    def enabled(self) -> bool:
        return True

    def fetch_accounts(self):
        raise RuntimeError("kaput")


class _OkProvider(brokerages.BrokerageProvider):
    name = "ok"

    def enabled(self) -> bool:
        return True

    def fetch_accounts(self):
        return [{"id": "ok:1", "brokerage": "ok", "label": "OK ••0001", "positions": [],
                 "total_value": 0.0, "cash": 0.0, "cost_basis": 0.0,
                 "gain_dollars": 0.0, "gain_percent": 0.0}]


@pytest.mark.unit
class TestFetchAllAccounts:
    def test_provider_error_isolated(self, monkeypatch):
        monkeypatch.setattr(brokerages, "_PROVIDERS", [_BoomProvider(), _OkProvider()])
        accounts = brokerages.fetch_all_accounts()
        assert [a["id"] for a in accounts] == ["ok:1"]

    def test_disabled_provider_skipped(self, monkeypatch):
        class _Off(_OkProvider):
            def enabled(self) -> bool:
                return False

        monkeypatch.setattr(brokerages, "_PROVIDERS", [_Off()])
        assert brokerages.fetch_all_accounts() == []
        assert brokerages.any_enabled() is False
