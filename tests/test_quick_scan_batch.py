"""Pure unit tests for the batched quick-scan building blocks in
`web.spy_scanner`. These functions have no DB or LLM dependencies.

The ``TestRunQuickScanBatching`` class exercises ``run_quick_scan`` end-to-end
with a fake LLM and synthetic price data.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from web import db, spy_scanner

pytestmark = pytest.mark.unit


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "web.db")
    db.init_db()
    yield db


@pytest.fixture(autouse=True)
def clear_price_cache():
    spy_scanner._PRICE_DATA_CACHE.clear()
    yield


@pytest.fixture
def base_config():
    return {
        "llm_provider": "openai",
        "quick_llm_provider": "openai",
        "quick_think_llm": "gpt-4o-mini",
        "quick_backend_url": "http://fake",
    }


def _make_price_map(tickers, closes_count=25, short_tickers=None):
    short_tickers = set(short_tickers or [])
    out = {}
    for i, t in enumerate(tickers):
        if t in short_tickers:
            out[t] = {"close": [100.0, 101.0], "volume": [1000, 1001]}
        else:
            out[t] = {
                "close": [100.0 + i + j * 0.1 for j in range(closes_count)],
                "volume": [1000 + j for j in range(closes_count)],
            }
    return out


def _run_scan(monkeypatch, tmp_db, tickers, fake_llm, batch_size=20, price_map=None,
              config=None, trade_date="2026-08-03", reuse_map=None):
    scan_id = tmp_db.create_spy_scan(trade_date)
    if price_map is None:
        price_map = _make_price_map(tickers)
    if reuse_map is not None:
        monkeypatch.setattr(
            spy_scanner.db,
            "find_reusable_quick_results",
            lambda trade_date, fingerprint, exclude_scan_id, max_age_hours: reuse_map,
        )
    monkeypatch.setattr(spy_scanner, "_fetch_price_data_map", lambda sid, ts, td: price_map)
    monkeypatch.setattr(spy_scanner, "_llm_quick", lambda cfg: fake_llm)
    monkeypatch.setattr(spy_scanner, "_total_budget", lambda: 5)
    monkeypatch.setattr(spy_scanner.time, "sleep", lambda s: None)
    if batch_size != 20:
        monkeypatch.setenv("QUICK_SCAN_BATCH_SIZE", str(batch_size))
    cfg = config or {
        "llm_provider": "openai",
        "quick_llm_provider": "openai",
        "quick_think_llm": "gpt-4o-mini",
        "quick_backend_url": "http://fake",
    }
    return scan_id, spy_scanner.run_quick_scan(scan_id, tickers, trade_date, cfg), price_map


class _FakeBatchLLM:
    """Deterministic batch-format LLM for quick-scan integration tests."""

    def __init__(
        self,
        signal_by_ticker=None,
        missing_tickers=None,
        fail_tickers=None,
        fail_message="boom",
        raise_429_n_times=0,
        prose=False,
    ):
        self.calls = []
        self.signal_by_ticker = signal_by_ticker or {}
        self.missing_tickers = set(missing_tickers or [])
        self.fail_tickers = set(fail_tickers or [])
        self.fail_message = fail_message
        self.raise_429_n_times = raise_429_n_times
        self.prose = prose
        self._429_count = 0

    @staticmethod
    def _tickers_in_prompt(prompt: str) -> list[str]:
        tickers = []
        for line in prompt.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0].rstrip(".").isdigit():
                tickers.append(parts[1])
        return tickers

    def _default_for(self, ticker: str) -> tuple[str, int, str]:
        if ticker in self.signal_by_ticker:
            return self.signal_by_ticker[ticker]
        c = ticker[0].upper()
        if c < "I":
            return ("BUY", 8, f"momentum {ticker}")
        if c < "Q":
            return ("HOLD", 5, f"steady {ticker}")
        return ("SELL", 3, f"fade {ticker}")

    def invoke(self, messages):
        prompt = messages[-1]["content"]
        self.calls.append(prompt)
        tickers = self._tickers_in_prompt(prompt)

        for t in tickers:
            if t in self.fail_tickers:
                raise RuntimeError(self.fail_message)

        if self._429_count < self.raise_429_n_times:
            self._429_count += 1
            raise Exception("429 Too Many Requests")

        if self.prose:
            return MagicMock(content="No valid ticker lines here, just prose.")

        lines = []
        for t in tickers:
            if t in self.missing_tickers:
                continue
            sig, conv, reason = self._default_for(t)
            lines.append(f"{t}|{sig}|{conv}|{reason}")
        return MagicMock(content="\n".join(lines))


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


class TestRunQuickScanBatching:
    def test_round_trip_count_collapses(self, tmp_db, monkeypatch, base_config):
        tickers = [f"T{i:02d}" for i in range(45)]
        fake = _FakeBatchLLM()
        scan_id, results, price_map = _run_scan(
            monkeypatch, tmp_db, tickers, fake, config=base_config
        )
        assert len(fake.calls) == 3
        assert len(results) == 45
        assert {r["ticker"] for r in results} == set(tickers)
        assert tmp_db.get_spy_scan(scan_id)["quick_count"] == 45

    def test_signals_land_on_the_right_tickers(self, tmp_db, monkeypatch, base_config):
        tickers = ["AAPL", "MSFT", "TSLA", "NVDA", "AMZN"]
        signals = {
            "AAPL": ("BUY", 8, "apple strong"),
            "MSFT": ("HOLD", 5, "msft steady"),
            "TSLA": ("SELL", 3, "tsla fade"),
            "NVDA": ("BUY", 9, "nvda ai"),
            "AMZN": ("HOLD", 4, "amzn flat"),
        }
        fake = _FakeBatchLLM(signal_by_ticker=signals)
        scan_id, results, price_map = _run_scan(
            monkeypatch, tmp_db, tickers, fake, config=base_config
        )
        assert len(results) == 5
        for r in results:
            assert (r["signal"], r["conviction"], r["reasoning"]) == signals[r["ticker"]]
            assert r["entry_price"] == price_map[r["ticker"]]["close"][-1]

    def test_row_shape_matches_single_path(self, tmp_db, monkeypatch, base_config):
        tickers = ["AAPL", "MSFT", "TSLA"]
        fake = _FakeBatchLLM()
        scan_id, results, price_map = _run_scan(
            monkeypatch, tmp_db, tickers, fake, config=base_config
        )
        allowed_keys = {"ticker", "signal", "conviction", "reasoning", "entry_price",
                        "error", "reused_quick", "skipped"}
        for r in results:
            assert set(r.keys()) <= allowed_keys
            assert r["signal"] in {"BUY", "HOLD", "SELL"}
            assert isinstance(r["conviction"], int)
            assert r["entry_price"] == price_map[r["ticker"]]["close"][-1]

    def test_reuse_hits_never_enter_a_batch(self, tmp_db, monkeypatch, base_config):
        tickers = [f"T{i:02d}" for i in range(45)]
        reused = set(tickers[:40])
        reuse_map = {
            t: {"signal": "BUY", "conviction": 9, "reasoning": f"cached {t}"}
            for t in reused
        }
        fake = _FakeBatchLLM()
        scan_id, results, price_map = _run_scan(
            monkeypatch, tmp_db, tickers, fake, config=base_config, reuse_map=reuse_map
        )
        assert len(fake.calls) == 1
        for prompt in fake.calls:
            for t in reused:
                assert t not in prompt
        reused_rows = [r for r in results if r.get("reused_quick")]
        assert len(reused_rows) == 40
        for r in reused_rows:
            assert r["signal"] == "BUY"
            assert r["conviction"] == 9
            assert r["reasoning"] == f"cached {r['ticker']}"
            assert r["entry_price"] == price_map[r["ticker"]]["close"][-1]
        assert len(results) == 45

    def test_insufficient_price_data_never_batched(self, tmp_db, monkeypatch, base_config):
        tickers = [f"T{i:02d}" for i in range(25)]
        short = set(tickers[:5])
        fake = _FakeBatchLLM()
        scan_id, results, price_map = _run_scan(
            monkeypatch,
            tmp_db,
            tickers,
            fake,
            config=base_config,
            price_map=_make_price_map(tickers, short_tickers=short),
        )
        insufficient = [r for r in results if r["reasoning"] == "Insufficient price data."]
        assert len(insufficient) == 5
        for r in insufficient:
            assert r["signal"] == "HOLD"
            assert r["conviction"] == 1
            assert r["entry_price"] == 0.0
            assert "error" not in r
            assert r["ticker"] in short
            for prompt in fake.calls:
                assert r["ticker"] not in prompt
        batchable = [r for r in results if r["ticker"] not in short]
        for r in batchable:
            assert r["signal"] in {"BUY", "HOLD", "SELL"}
            assert r["entry_price"] == price_map[r["ticker"]]["close"][-1]
        assert len(fake.calls) == 1

    def test_malformed_line_degrades_one_ticker_only(self, tmp_db, monkeypatch, base_config, caplog):
        tickers = ["A1", "B1", "C1", "D1", "E1"]
        fake = _FakeBatchLLM(missing_tickers={"B1", "D1"})
        with caplog.at_level(logging.WARNING, logger="web.spy_scanner"):
            scan_id, results, price_map = _run_scan(
                monkeypatch, tmp_db, tickers, fake, config=base_config
            )
        missing_rows = [r for r in results if r.get("error") == "batch line unparsed"]
        assert len(missing_rows) == 2
        for r in missing_rows:
            assert r["signal"] == "HOLD"
            assert r["conviction"] == 5
            assert r["reasoning"] == "batch response line missing or unparsable"
            assert r["entry_price"] == price_map[r["ticker"]]["close"][-1]
        good_rows = [r for r in results if r["ticker"] in {"A1", "C1", "E1"}]
        for r in good_rows:
            assert r["signal"] in {"BUY", "HOLD", "SELL"}
            assert isinstance(r["conviction"], int)
            assert not r.get("error")
        assert "batch: 2/5 tickers had no parsable line" in caplog.text
        assert "B1" in caplog.text and "D1" in caplog.text

    def test_wholesale_format_failure_trips_the_health_guard(self, tmp_db, monkeypatch, base_config):
        tickers = [f"T{i:02d}" for i in range(20)]
        fake = _FakeBatchLLM(prose=True)
        scan_id, results, price_map = _run_scan(
            monkeypatch, tmp_db, tickers, fake, config=base_config
        )
        assert len(results) == 20
        assert all(r.get("error") for r in results)
        with pytest.raises(spy_scanner.ScanInfrastructureError):
            spy_scanner.assert_quick_scan_healthy(results)

        insufficient = [
            {"ticker": "X", "signal": "HOLD", "conviction": 1,
             "reasoning": "Insufficient price data.", "entry_price": 0.0}
        ]
        spy_scanner.assert_quick_scan_healthy(insufficient)

    def test_batch_exception_does_not_storm(self, tmp_db, monkeypatch, base_config):
        tickers = [f"T{i:02d}" for i in range(45)]
        fake = _FakeBatchLLM(fail_tickers={"T00"})
        scan_id, results, price_map = _run_scan(
            monkeypatch, tmp_db, tickers, fake, config=base_config
        )
        failing_tickers = {f"T{i:02d}" for i in range(20)}
        failing_rows = [r for r in results if r["ticker"] in failing_tickers]
        assert len(failing_rows) == 20
        for r in failing_rows:
            assert r["signal"] == "HOLD"
            assert r["conviction"] == 1
            assert r["reasoning"].startswith("scan error:")
            assert r.get("error")
        ok_rows = [r for r in results if r["ticker"] not in failing_tickers]
        for r in ok_rows:
            assert not r.get("error")
            assert r["signal"] in {"BUY", "HOLD", "SELL"}
        assert len(fake.calls) == 3

    def test_batch_level_429_retry(self, tmp_db, monkeypatch, base_config):
        tickers = [f"T{i:02d}" for i in range(20)]
        fake = _FakeBatchLLM(raise_429_n_times=2)
        scan_id, results, price_map = _run_scan(
            monkeypatch, tmp_db, tickers, fake, config=base_config
        )
        assert len(fake.calls) == 3
        for r in results:
            assert r["signal"] in {"BUY", "HOLD", "SELL"}
            assert not r.get("error")

    def test_env_override_batch_size(self, tmp_db, monkeypatch, base_config):
        tickers = [f"T{i:02d}" for i in range(25)]
        fake = _FakeBatchLLM()
        scan_id, results, price_map = _run_scan(
            monkeypatch, tmp_db, tickers, fake, batch_size=5, config=base_config
        )
        assert len(fake.calls) == 5

    def test_tail_batch_of_one_uses_quick_scan_one(self, tmp_db, monkeypatch, base_config):
        tickers = [f"T{i:02d}" for i in range(21)]
        spy_calls = []

        def spy_quick_scan_one(ticker, price_data, sector, llm, gate=None):
            spy_calls.append(ticker)
            return {
                "ticker": ticker,
                "signal": "BUY",
                "conviction": 7,
                "reasoning": "single path",
                "entry_price": float(price_data["close"][-1]),
            }

        monkeypatch.setattr(spy_scanner, "_quick_scan_one", spy_quick_scan_one)
        fake = _FakeBatchLLM()
        scan_id, results, price_map = _run_scan(
            monkeypatch, tmp_db, tickers, fake, batch_size=20, config=base_config
        )
        assert len(spy_calls) == 1
        assert spy_calls[0] == tickers[-1]
        assert len(results) == 21

    def test_cancel_before_start_raises(self, tmp_db, monkeypatch, base_config):
        scan_id = tmp_db.create_spy_scan("2026-08-03")
        tmp_db.request_spy_scan_cancel(scan_id)
        tickers = [f"T{i:02d}" for i in range(20)]
        fake = _FakeBatchLLM()
        price_map = _make_price_map(tickers)
        monkeypatch.setattr(spy_scanner, "_fetch_price_data_map", lambda sid, ts, td: price_map)
        monkeypatch.setattr(spy_scanner, "_llm_quick", lambda cfg: fake)
        monkeypatch.setattr(spy_scanner, "_total_budget", lambda: 5)
        monkeypatch.setattr(spy_scanner.time, "sleep", lambda s: None)
        with pytest.raises(spy_scanner.ScanCancelled):
            spy_scanner.run_quick_scan(scan_id, tickers, "2026-08-03", base_config)
        assert len(fake.calls) == 0

    def test_progress_flush_per_batch(self, tmp_db, monkeypatch, base_config):
        tickers = [f"T{i:02d}" for i in range(40)]
        fake = _FakeBatchLLM()
        scan_id = tmp_db.create_spy_scan("2026-08-03")
        price_map = _make_price_map(tickers)
        monkeypatch.setattr(spy_scanner, "_fetch_price_data_map", lambda sid, ts, td: price_map)
        monkeypatch.setattr(spy_scanner, "_llm_quick", lambda cfg: fake)
        monkeypatch.setattr(spy_scanner, "_total_budget", lambda: 5)
        monkeypatch.setattr(spy_scanner.time, "sleep", lambda s: None)

        original_update = spy_scanner.db.update_spy_scan
        update_calls = []

        def tracking_update(sid, **kwargs):
            update_calls.append(kwargs)
            return original_update(sid, **kwargs)

        monkeypatch.setattr(spy_scanner.db, "update_spy_scan", tracking_update)

        results = spy_scanner.run_quick_scan(scan_id, tickers, "2026-08-03", base_config)
        quick_count_calls = [c for c in update_calls if "quick_count" in c]

        batch_size = spy_scanner._quick_batch_size()
        number_of_batches = (len(tickers) + batch_size - 1) // batch_size
        # One flush before batching, one flush per completed batch, and a final
        # flush after the ThreadPoolExecutor loop.
        assert len(quick_count_calls) >= number_of_batches + 2

        assert tmp_db.get_spy_scan(scan_id)["quick_count"] == 40
        assert len(results) == 40
