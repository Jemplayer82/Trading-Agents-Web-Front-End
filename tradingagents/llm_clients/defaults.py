"""Shared default endpoints for LLM providers.

A single source of truth for values that were previously copy-pasted across
the web layer, the CLI and the OpenAI-compatible client. Right now that is the
Ollama base URL and its ``OLLAMA_BASE_URL`` env-var override.

Keeping the override logic here (rather than re-deriving it at each call site)
means there is exactly one place to change if the default port moves or a new
override variable is introduced.
"""

from __future__ import annotations

import os

# Local Ollama's OpenAI-compatible endpoint. The ``/v1`` suffix is required so
# the OpenAI client speaks the compatible API rather than Ollama's native one.
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434/v1"


def resolve_ollama_base_url() -> str:
    """Return the Ollama base URL, honouring the ``OLLAMA_BASE_URL`` override.

    Pointing this at a remote ollama-serve (or Ollama Cloud) is the convention
    in the broader Ollama ecosystem, so we read it at call time — not import
    time — to keep tests that monkeypatch the env after import correct.
    """
    return os.environ.get("OLLAMA_BASE_URL") or DEFAULT_OLLAMA_BASE_URL
