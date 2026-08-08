"""Unit tests for web/spy_allocator.py's LLM role selection."""

from unittest.mock import MagicMock

import pytest

from web import spy_allocator

pytestmark = pytest.mark.unit


def test_llm_uses_quick_model(monkeypatch):
    captured: dict = {}

    def _fake_llm_for(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(spy_allocator, "llm_for", _fake_llm_for)

    spy_allocator._llm({})

    assert captured.get("deep") is False
