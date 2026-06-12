"""Fernet-encrypted Schwab token storage on the shared volume.

One small file (TOKEN_PATH, overridable via SCHWAB_TOKEN_PATH) holding the
JSON-serialized TokenBundle, encrypted with TOKEN_ENCRYPTION_KEY. Encryption
is mandatory here (require_fernet) — unlike the opt-in secret_box DB columns,
there is no plaintext fallback for OAuth tokens.

The api container writes the bundle at the end of the OAuth flow and deletes
it on disconnect (web/main.py); the direct REST client
(web/auth/schwab_client.py) reads it and re-saves on refresh. Every container
mounting the tradingagents_data volume sees the same file. Writes are atomic
(tmp file + rename), so there is no SQLite/WAL-style coordination to manage.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from cryptography.fernet import InvalidToken

from web.secret_box import require_fernet

_HOME = Path(os.path.expanduser("~")) / ".tradingagents"
TOKEN_PATH = Path(os.environ.get("SCHWAB_TOKEN_PATH", str(_HOME / "schwab_tokens.enc")))


@dataclass
class TokenBundle:
    access_token: str
    refresh_token: str
    expires_at: str          # ISO timestamp — when the access token dies
    refresh_issued_at: str   # ISO timestamp — when the *refresh* token was minted


def load() -> TokenBundle | None:
    """Decrypt and load the bundle, or None if absent or undecryptable.

    Every failure mode (wrong/rotated TOKEN_ENCRYPTION_KEY, corrupt file,
    schema drift in the JSON) collapses to None — i.e. "not connected" —
    so the remedy is re-running the Schwab OAuth flow, not a crash.
    """
    if not TOKEN_PATH.exists():
        return None
    try:
        encrypted = TOKEN_PATH.read_bytes()
        plain = require_fernet().decrypt(encrypted)
        data = json.loads(plain.decode())
        return TokenBundle(**data)
    except (InvalidToken, json.JSONDecodeError, TypeError, ValueError):
        return None


def save(bundle: TokenBundle) -> None:
    """Encrypt and atomically replace the token file (tmp + rename, 0600)."""
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(bundle)).encode()
    encrypted = require_fernet().encrypt(payload)
    # Write-then-rename: a concurrent reader sees either the old bundle or the
    # new one, never a torn file.
    tmp = TOKEN_PATH.with_suffix(".tmp")
    tmp.write_bytes(encrypted)
    # Owner-only perms, best-effort — chmod may be unsupported (Windows dev).
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(TOKEN_PATH)


def clear() -> None:
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
