"""Unit tests for web/portfolio/aggregator.py's LLM role selection."""

from unittest.mock import MagicMock

import pytest

from web.portfolio import aggregator

pytestmark = pytest.mark.unit


def test_llm_uses_quick_model(monkeypatch):
    captured: dict = {}
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="## Concentration Risk\ntest\n")

    def _fake_llm_for(*args, **kwargs):
        captured.update(kwargs)
        return llm

    monkeypatch.setattr(aggregator, "llm_for", _fake_llm_for)

    per_ticker = [{
        "ticker": "AAPL", "signal": "BUY", "quantity": 10, "market_value": 1000.0,
        "trader_plan": "buy plan", "final_decision": "hold verdict",
    }]

    result = aggregator.run(per_ticker, "2026-07-17", {})

    assert captured.get("deep") is False
    assert "Concentration Risk" in result
