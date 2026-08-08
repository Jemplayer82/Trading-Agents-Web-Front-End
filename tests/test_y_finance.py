import pandas as pd
import pytest

from tradingagents.dataflows import y_finance


class _FakeTicker:
    def __init__(self, df):
        self._df = df

    def history(self, start=None, end=None):
        return self._df


def _make_df(n_days):
    idx = pd.bdate_range("2025-01-01", periods=n_days)
    return pd.DataFrame(
        {
            "Open": range(n_days),
            "High": range(n_days),
            "Low": range(n_days),
            "Close": range(n_days),
            "Volume": range(n_days),
        },
        index=idx,
    )


def _csv_row_count(result_text):
    body_lines = [
        line for line in result_text.splitlines()
        if line.strip() and not line.startswith("#")
    ]
    # first non-comment line is the CSV header row
    return len(body_lines) - 1


@pytest.mark.unit
class TestYFinDownsampling:
    def test_short_range_no_downsampling(self, monkeypatch):
        df = _make_df(40)
        monkeypatch.setattr(y_finance.yf, "Ticker", lambda symbol: _FakeTicker(df))
        result = y_finance.get_YFin_data_online("TEST", "2025-01-01", "2025-03-01")
        assert _csv_row_count(result) == 40
        assert "downsampled" not in result.lower()

    def test_long_range_downsamples_older_rows_without_losing_recent_ones(self, monkeypatch):
        df = _make_df(250)
        monkeypatch.setattr(y_finance.yf, "Ticker", lambda symbol: _FakeTicker(df))
        result = y_finance.get_YFin_data_online("TEST", "2025-01-01", "2025-12-01")
        row_count = _csv_row_count(result)
        assert row_count < 250
        assert row_count >= 60
        assert "downsampled" in result.lower()

    def test_exactly_at_threshold_no_downsampling(self, monkeypatch):
        df = _make_df(60)
        monkeypatch.setattr(y_finance.yf, "Ticker", lambda symbol: _FakeTicker(df))
        result = y_finance.get_YFin_data_online("TEST", "2025-01-01", "2025-04-01")
        assert _csv_row_count(result) == 60
        assert "downsampled" not in result.lower()
