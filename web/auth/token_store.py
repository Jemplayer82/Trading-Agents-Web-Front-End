"""Fernet-encrypted Schwab token storage in the shared volume.

Both the api container (which exchanges the OAuth code and writes the bundle)
and the portfolio container (which reads + refreshes) hit the same file via
the tradingagents_data volume. WAL is irrelevant here — it's a single file
rewritten atomically.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_HOME = Path(os.path.expanduser("~")) / ".tradingagents"
TOKEN_PATH = Path(os.environ.get("SCHWAB_TOKEN_PATH", str(_HOME / "schwab_tokens.enc")))


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: str          # ISO timestamp — when the access token dies
    refresh_issued_at: str   # ISO timestamp — when the *refresh* token was minted


def _key() -> bytes:
    raw = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not raw:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY env var is required")
    return raw.encode()


def load() -> Optional[TokenBundle]:
    if not TOKEN_PATH.exists():
        return None
    try:
        encrypted = TOKEN_PATH.read_bytes()
        plain = Fernet(_key()).decrypt(encrypted)
        data = json.loads(plain.decode())
        return TokenBundle(**data)
    except (InvalidToken, json.JSONDecodeError, TypeError, ValueError):
        return None


def save(bundle: TokenBundle) -> None:
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(bundle)).encode()
    encrypted = Fernet(_key()).encrypt(payload)
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_bytes(encrypted)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(TOKEN_PATH)


def clear() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
