import copy
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import tradingagents.default_config as default_config
from tradingagents.dataflows.alpha_vantage_news import get_global_news
from tradingagents.dataflows.config import set_config


@pytest.mark.unit
def test_get_global_news_uses_config_defaults_when_none():
    """None look_back_days/limit must use config defaults, not raise TypeError."""
    set_config(copy.deepcopy(default_config.DEFAULT_CONFIG))
    config = default_config.DEFAULT_CONFIG

    curr_date = "2024-01-10"
    expected_start = (
        datetime.strptime(curr_date, "%Y-%m-%d")
        - timedelta(days=config["global_news_lookback_days"])
    ).strftime("%Y-%m-%d").replace("-", "")

    with patch("tradingagents.dataflows.alpha_vantage_news._make_api_request") as mock_api:
        mock_api.return_value = {"mock": "news"}
        result = get_global_news(curr_date, None, None)

    mock_api.assert_called_once()
    _, params = mock_api.call_args.args

    assert result == {"mock": "news"}
    assert params["topics"] == "financial_markets,economy_macro,economy_monetary"
    assert params["limit"] == str(config["global_news_article_limit"])
    assert expected_start in params["time_from"]
    assert curr_date.replace("-", "") in params["time_to"]


@pytest.mark.unit
def test_get_global_news_honors_explicit_arguments():
    """Explicit look_back_days/limit values must override config defaults."""
    set_config(copy.deepcopy(default_config.DEFAULT_CONFIG))

    curr_date = "2024-01-10"
    expected_start = "20240107"  # 3 days before 2024-01-10

    with patch("tradingagents.dataflows.alpha_vantage_news._make_api_request") as mock_api:
        get_global_news(curr_date, look_back_days=3, limit=5)

    _, params = mock_api.call_args.args

    assert params["limit"] == "5"
    assert expected_start in params["time_from"]
    assert curr_date.replace("-", "") in params["time_to"]
