import inspect
import re

import pytest

from tradingagents.orchestrator.switchboard_orchestrator import SwitchboardOrchestrator

pytestmark = pytest.mark.unit


def test_news_analyst_factory_passes_macro_brief_from_config():
    src = inspect.getsource(SwitchboardOrchestrator.run)
    assert re.search(
        r'create_news_analyst\(\s*self\._quick_llm\s*,\s*macro_brief\s*=\s*'
        r'self\.config\.get\(\s*["\']macro_brief["\']\s*\)\s*\)',
        src,
    ), "analyst_factories['news'] must pass macro_brief=self.config.get('macro_brief') through to create_news_analyst"


def test_other_analyst_factories_unchanged():
    src = inspect.getsource(SwitchboardOrchestrator.run)
    assert "lambda: create_market_analyst(self._quick_llm)" in src
    assert "lambda: create_sentiment_analyst(self._quick_llm)" in src
    assert "lambda: create_fundamentals_analyst(self._quick_llm)" in src
