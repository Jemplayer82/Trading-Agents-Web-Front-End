"""Shared LLM factory.

Centralises the duplicated `_llm_for()` pattern previously copied across
`web/portfolio/aggregator.py` and `web/spy_allocator.py`. Use this helper
anywhere the web layer needs a quick ChatOpenAI client driven by the
user's saved preferences (provider, deep_model, quick_model, backend_url).
"""
from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI


def llm_for(config: dict[str, Any], *, deep: bool = True, temperature: float = 0.2) -> ChatOpenAI:
    """Build a ChatOpenAI client from the user's prefs dict.

    Args:
        config: prefs dict (e.g. from `db.get_preferences()`). Recognised keys:
            llm_provider, deep_think_llm, quick_think_llm, backend_url/base_url.
        deep: if True (default), use `deep_think_llm`; else use `quick_think_llm`.
        temperature: model temperature; callers override per use-case.

    Returns:
        A configured `langchain_openai.ChatOpenAI` instance.
    """
    provider = (config.get("llm_provider") or "ollama").lower()
    if deep:
        model = config.get("deep_think_llm") or "gpt-oss:120b-cloud"
    else:
        model = config.get("quick_think_llm") or "gpt-oss:20b-cloud"
    base_url = config.get("backend_url") or config.get("base_url")

    kwargs: dict[str, Any] = {"model": model, "temperature": temperature}
    if provider == "ollama":
        kwargs["base_url"] = base_url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
        kwargs["api_key"] = os.environ.get("OLLAMA_API_KEY", "ollama")
    elif base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(**kwargs)
