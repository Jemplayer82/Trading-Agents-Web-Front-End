"""Pure unit tests for the batched quick-scan building blocks in
`web.spy_scanner`. These functions have no DB or LLM dependencies.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from web import spy_scanner

pytestmark = pytest.mark.unit


class TestQuickFeatures:
    def test_none_when_fewer_than_five_closes(self):
        price_data = {"close": [1.0, 2.0, 3.0, 4.0]}
        assert spy_scanner._quick_features("TICK", price_data, "Tech") is None

    def test_known_25_close_series(self):
        # 25 closes rising from 100 to 124.
        closes = list(range(100, 125))  # [100, ..., 124], len 25
        # 24 trailing volumes at 10, then today's spike at 190.
        volumes = [10] * 24 + [190]
        price_data = {"close": closes, "volume": volumes}

        feats = spy_scanner._quick_features("RISE", price_data, "Tech")

        # price = last close = 124.0
        # ret5  = ((124 / 120) - 1) * 100 = 3.3333333333333335
        # ret20 = ((124 / 100) - 1) * 100 = 24.0
        # avg of prior 19 volumes in the trailing 20-day window:
        #   window = volumes[-20:-1] = [10]*19 -> avg = 190/19 = 10.0
        # vol_ratio = 190 / 10.0 = 19.0
        assert feats == {
            "ticker": "RISE",
            "price": 124.0,
            "ret5": pytest.approx(3.3333333333333335),
            "ret20": pytest.approx(24.0),
            "vol_ratio": pytest.approx(19.0),
            "sector": "Tech",
            "price_data": price_data,
        }

    def test_ret20_zero_with_only_ten_closes(self):
        closes = [float(i) for i in range(1, 11)]
        feats = spy_scanner._quick_features("TEN", {"close": closes}, "Tech")
        assert feats is not None
        assert feats["ret20"] == pytest.approx(0.0)

    def test_vol_ratio_one_when_volumes_missing(self):
        closes = [float(i) for i in range(1, 26)]
        feats = spy_scanner._quick_features("NOVOL", {"close": closes}, "Tech")
        assert feats is not None
        assert feats["vol_ratio"] == pytest.approx(1.0)

    def test_vol_ratio_one_when_volumes_empty(self):
        closes = [float(i) for i in range(1, 26)]
        feats = spy_scanner._quick_features("EMPTYVOL", {"close": closes, "volume": []}, "Tech")
        assert feats is not None
        assert feats["vol_ratio"] == pytest.approx(1.0)


def test_quick_scan_one_insufficient_data_does_not_call_llm():
    llm = MagicMock()
    price_data = {"close": [1.0, 2.0]}
    row = spy_scanner._quick_scan_one("TICK", price_data, "Sector", llm)
    assert row == {
        "ticker": "TICK",
        "signal": "HOLD",
        "conviction": 1,
        "reasoning": "Insufficient price data.",
        "entry_price": 0.0,
    }
    assert "error" not in row
    llm.invoke.assert_not_called()


class TestBuildBatchPrompt:
    def test_three_rows_numbered_and_no_price_data_leak(self):
        rows = [
            spy_scanner._quick_features("AAPL", {"close": list(range(100, 125)), "volume": [10] * 24 + [190]}, "Tech"),
            spy_scanner._quick_features("MSFT", {"close": list(range(200, 225)), "volume": [5] * 24 + [50]}, "Tech"),
            spy_scanner._quick_features("TSLA", {"close": list(range(300, 325)), "volume": [1] * 24 + [5]}, "Auto"),
        ]
        prompt = spy_scanner._build_quick_batch_prompt(rows)

        lines = prompt.splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("1. AAPL")
        assert lines[1].startswith("2. MSFT")
        assert lines[2].startswith("3. TSLA")

        assert "price_data" not in prompt
        assert "{" not in prompt or "dict(" not in prompt
        assert lines[0].count("AAPL") >= 1
        assert lines[1].count("MSFT") >= 1
        assert lines[2].count("TSLA") >= 1


class TestParseBatchResponse:
    def test_well_formed_three_lines(self):
        text = (
            "AAPL|BUY|8|strong momentum\n"
            "MSFT|HOLD|5|unclear direction\n"
            "NVDA|SELL|3|profit taking"
        )
        parsed = spy_scanner._parse_quick_batch_response(text, ["AAPL", "MSFT", "NVDA"])
        assert parsed == {
            "AAPL": ("BUY", 8, "strong momentum"),
            "MSFT": ("HOLD", 5, "unclear direction"),
            "NVDA": ("SELL", 3, "profit taking"),
        }

    def test_conviction_ten_not_one(self):
        parsed = spy_scanner._parse_quick_batch_response("T|BUY|10|x", ["T"])
        assert parsed["T"] == ("BUY", 10, "x")

    def test_numbered_prefix_parses(self):
        parsed = spy_scanner._parse_quick_batch_response("1. NVDA|BUY|8|x", ["NVDA"])
        assert parsed["NVDA"] == ("BUY", 8, "x")

    def test_markdown_bold_wrapper_parses(self):
        parsed = spy_scanner._parse_quick_batch_response("**AAPL|HOLD|5|y**", ["AAPL"])
        assert parsed["AAPL"] == ("HOLD", 5, "y")

    def test_lowercase_ticker_uses_original_key(self):
        parsed = spy_scanner._parse_quick_batch_response("nvda|buy|8|x", ["NVDA"])
        assert parsed["NVDA"] == ("BUY", 8, "x")

        parsed2 = spy_scanner._parse_quick_batch_response("nvda|buy|8|x", ["Nvda"])
        assert parsed2["Nvda"] == ("BUY", 8, "x")

    def test_missing_ticker_returns_only_present_keys_no_shift(self):
        text = "NVDA|BUY|9|ai fever\nMSFT|HOLD|4|steady"
        parsed = spy_scanner._parse_quick_batch_response(text, ["NVDA", "AAPL", "MSFT"])
        assert set(parsed.keys()) == {"NVDA", "MSFT"}
        assert parsed["MSFT"] == ("HOLD", 4, "steady")

    def test_reordered_response_maps_by_symbol(self):
        text = "MSFT|SELL|2|soft\nAAPL|BUY|9|breakout"
        parsed = spy_scanner._parse_quick_batch_response(text, ["AAPL", "MSFT"])
        assert parsed["AAPL"] == ("BUY", 9, "breakout")
        assert parsed["MSFT"] == ("SELL", 2, "soft")

    def test_unknown_ticker_line_dropped(self):
        parsed = spy_scanner._parse_quick_batch_response(
            "AAPL|BUY|8|ok\nZZZZ|SELL|1|no", ["AAPL"]
        )
        assert parsed == {"AAPL": ("BUY", 8, "ok")}

    def test_duplicate_line_keeps_first_match(self):
        text = "AAPL|BUY|8|first\nAAPL|SELL|1|second"
        parsed = spy_scanner._parse_quick_batch_response(text, ["AAPL"])
        assert parsed["AAPL"] == ("BUY", 8, "first")

    def test_invalid_conviction_values_rejected(self):
        for conv in ["0", "11", "-3", "high"]:
            text = f"A|BUY|{conv}|x"
            parsed = spy_scanner._parse_quick_batch_response(text, ["A"])
            assert parsed == {}

    def test_reason_with_extra_pipe_survives(self):
        text = "A|BUY|7|a|b|c"
        parsed = spy_scanner._parse_quick_batch_response(text, ["A"])
        assert parsed["A"] == ("BUY", 7, "a|b|c")

    def test_long_reason_truncated_to_500(self):
        reason = "x" * 900
        text = f"A|BUY|7|{reason}"
        parsed = spy_scanner._parse_quick_batch_response(text, ["A"])
        assert parsed["A"] == ("BUY", 7, "x" * 500)

    def test_prose_without_pipes_returns_empty(self):
        parsed = spy_scanner._parse_quick_batch_response("nothing useful here", ["A"])
        assert parsed == {}

    def test_empty_input_returns_empty(self):
        parsed = spy_scanner._parse_quick_batch_response("", ["A"])
        assert parsed == {}
