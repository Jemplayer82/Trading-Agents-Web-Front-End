"""Shared LLM factory.

Centralises the duplicated `_llm_for()` pattern previously copied across
`web/portfolio/aggregator.py` and `web/spy_allocator.py`. Use this helper
anywhere the web layer needs a quick LLM client driven by the user's saved
preferences (provider, deep_model, quick_model, backend_url).

API keys are resolved in this order:
  1. config["api_key"]            (explicit override from caller)
  2. provider_credentials row     (set via the dashboard's API Keys tab)
  3. process env var              (PROVIDER_API_KEY_ENV mapping)
  4. provider-specific fallback   (e.g. Ollama uses "ollama" as a dummy key)

For OpenAI-compatible providers this builds `langchain_openai.ChatOpenAI`.
For the "switchboard" provider it delegates to `create_llm_client` so the
bus is used consistently with the main orchestrator pipeline.
"""
from __future__ import annotations

import os
from typing import Any

from langchain_openai import ChatOpenAI

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.llm_clients.defaults import resolve_ollama_base_url


def _resolve_api_key(provider: str, config: dict[str, Any]) -> str | None:
    """Find the best available API key for `provider`."""
    if config.get("api_key"):
        return str(config["api_key"])

    # DB-stored credential — imported lazily to avoid an import cycle
    # when `db.py` ever needs to import anything from this module.
    try:
        from . import db as _db
        row = _db.get_credential(provider)
        if row and row.get("api_key"):
            return row["api_key"]
    except Exception:
        pass

    env_var = PROVIDER_API_KEY_ENV.get(provider)
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val:
            return env_val

    if provider == "ollama":
        return os.environ.get("OLLAMA_API_KEY", "ollama")
    return None


def _resolve_base_url(provider: str, config: dict[str, Any], *, deep: bool = True) -> str | None:
    """Find the best base URL for `provider`, preferring the role-specific override."""
    role_key = "deep_backend_url" if deep else "quick_backend_url"
    explicit = config.get(role_key) or config.get("backend_url") or config.get("base_url")
    if explicit:
        return str(explicit)

    try:
        from . import db as _db
        row = _db.get_credential(provider)
        if row and row.get("base_url"):
            return row["base_url"]
    except Exception:
        pass

    if provider == "ollama":
        return resolve_ollama_base_url()
    return None


def llm_for(config: dict[str, Any], *, deep: bool = True, temperature: float = 0.2):
    """Build an LLM client from the user's prefs dict.

    Args:
        config: prefs dict (e.g. from `db.get_preferences()`). Recognised keys:
            llm_provider, deep_think_llm, quick_think_llm, backend_url/base_url, api_key.
        deep: if True (default), use `deep_think_llm`; else use `quick_think_llm`.
        temperature: model temperature; callers override per use-case.

    Returns:
        A LangChain BaseChatModel instance (ChatOpenAI for OpenAI-compatible
        providers, SwitchboardChatModel when provider is "switchboard").
    """
    role_key = "deep_llm_provider" if deep else "quick_llm_provider"
    provider = (config.get(role_key) or config.get("llm_provider") or "ollama").lower()
    if deep:
        model = config.get("deep_think_llm") or "gpt-oss:120b-cloud"
    else:
        model = config.get("quick_think_llm") or "gpt-oss:20b-cloud"

    if provider == "switchboard":
        from tradingagents.llm_clients import create_llm_client
        return create_llm_client(provider="switchboard", model=model).get_llm()

    kwargs: dict[str, Any] = {"model": model, "temperature": temperature}
    base_url = _resolve_base_url(provider, config, deep=deep)
    if base_url:
        kwargs["base_url"] = base_url
    api_key = _resolve_api_key(provider, config)
    if api_key:
        kwargs["api_key"] = api_key
    return ChatOpenAI(**kwargs)
