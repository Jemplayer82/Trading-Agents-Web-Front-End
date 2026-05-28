"""Centralised LLM-provider credential management.

The core `tradingagents/` package reads provider API keys from
process-level env vars (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc. — see
`tradingagents/llm_clients/api_key_env.py`). To let the user enter
keys via the web UI without restarting containers, we persist them in
`provider_credentials` (sqlite, `web/db.py`) and copy them onto
`os.environ` on startup and on every save. Env vars set externally
(.env, docker-compose) remain visible if no DB row overrides them.

Never echo a raw key back to the client. All UI-facing endpoints go
through `list_meta()` which masks the secret.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

from . import db

log = logging.getLogger(__name__)


def mask_key(key: str | None) -> str:
    """Reveal at most the last 4 characters of a key."""
    if not key:
        return ""
    if len(key) <= 6:
        return "•" * len(key)
    return "•••• " + key[-4:]


def apply_to_env() -> None:
    """Populate os.environ from DB-stored credentials.

    Called at FastAPI startup and after every PUT/DELETE so changes
    take effect immediately within this process. Other containers
    (portfolio, scheduler) pick up the change at their next startup.
    """
    rows = db.list_credentials()
    by_provider = {r["provider"]: r for r in rows}
    applied = 0
    for prov, env_var in PROVIDER_API_KEY_ENV.items():
        if not env_var:
            continue  # ollama uses no key
        row = by_provider.get(prov)
        if row and row.get("api_key"):
            os.environ[env_var] = row["api_key"]
            applied += 1
        # else: leave any externally-set env var (.env, compose) alone
    if applied:
        log.info("[credentials] applied %d provider keys from DB to env", applied)


def list_meta() -> list[dict[str, Any]]:
    """Per-provider credential metadata — masked, safe to return to the client.

    For each known provider, reports whether a key exists, where it
    comes from (db / env / none), and a 4-char preview.
    """
    rows = db.list_credentials()
    by_provider = {r["provider"]: r for r in rows}

    out: list[dict[str, Any]] = []
    for prov, env_var in PROVIDER_API_KEY_ENV.items():
        row = by_provider.get(prov)
        db_key = (row or {}).get("api_key") or ""
        env_val = os.environ.get(env_var) if env_var else None
        effective = db_key or env_val or ""
        source = "db" if db_key else ("env" if env_val else None)
        out.append({
            "provider": prov,
            "env_var": env_var,
            "has_key": bool(effective),
            "masked": mask_key(effective),
            "source": source,
            "base_url": (row or {}).get("base_url"),
            "updated_at": (row or {}).get("updated_at"),
        })
    return out
