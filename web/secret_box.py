"""Symmetric encryption for secrets stored in the web database.

Reuses the same ``TOKEN_ENCRYPTION_KEY`` (Fernet) that already protects Schwab
OAuth tokens to encrypt provider API keys and app settings at rest, so there is
one key to manage rather than two.

Encryption is **opt-in by key presence**, for backward compatibility with
existing deployments:

  * If ``TOKEN_ENCRYPTION_KEY`` is set, secrets are Fernet-encrypted on write and
    decrypted on read, and existing plaintext rows are migrated on startup.
  * If it is NOT set, values are stored as-is (the historical behavior) so a
    deployment that never configured the key keeps working. This is logged once
    as a warning, since at-rest encryption is strongly recommended.

Encrypted values carry an ``enc:v1:`` prefix so encrypted and legacy-plaintext
rows can coexist in the same column and be told apart unambiguously.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

# Version-tagged prefix on every ciphertext. The version lets us evolve the
# scheme later (e.g. key rotation) without ambiguity about how to decode a row.
_PREFIX = "enc:v1:"

_warned_no_key = False


def _raw_key() -> str | None:
    return os.environ.get("TOKEN_ENCRYPTION_KEY") or None


def validate_key() -> None:
    """Fail fast at startup if ``TOKEN_ENCRYPTION_KEY`` is set but malformed.

    A no-op when the key is unset (encryption is simply disabled). When set, the
    value must be a valid Fernet key (32 url-safe base64-encoded bytes) — we
    surface a clear, actionable error rather than letting a cryptic ValueError
    escape from the first encrypt/decrypt call deep in a request.
    """
    raw = _raw_key()
    if raw is None:
        return
    try:
        Fernet(raw.encode())
    except (ValueError, TypeError) as e:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is set but is not a valid Fernet key (expected "
            "32 url-safe base64-encoded bytes). Generate one with: "
            "python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'"
        ) from e


def _fernet() -> Fernet | None:
    raw = _raw_key()
    return Fernet(raw.encode()) if raw is not None else None


def require_fernet() -> Fernet:
    """Return a Fernet instance, raising if no key is configured.

    For callers where encryption is mandatory (Schwab token storage).
    """
    f = _fernet()
    if f is None:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY env var is required")
    return f


def encryption_enabled() -> bool:
    """True when a key is configured and secrets will actually be encrypted."""
    return _raw_key() is not None


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(_PREFIX)


def encrypt_secret(value: str | None) -> str | None:
    """Encrypt a secret for storage.

    Pass-through for empty values, already-encrypted values, or when no key is
    configured (logs once in that case).
    """
    global _warned_no_key
    if not value or is_encrypted(value):
        return value
    f = _fernet()
    if f is None:
        if not _warned_no_key:
            log.warning(
                "TOKEN_ENCRYPTION_KEY is not set; storing secrets in plaintext. "
                "Set it to encrypt API keys and app settings at rest."
            )
            _warned_no_key = True
        return value
    return _PREFIX + f.encrypt(value.encode()).decode()


def decrypt_secret(value: str | None) -> str | None:
    """Decrypt a stored secret. Legacy/plaintext values are returned unchanged."""
    if not is_encrypted(value):
        return value
    f = _fernet()
    if f is None:
        raise RuntimeError(
            "Found an encrypted secret but TOKEN_ENCRYPTION_KEY is not set; cannot "
            "decrypt. Restore the key that was used to encrypt this database."
        )
    try:
        return f.decrypt(value[len(_PREFIX):].encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "Failed to decrypt a stored secret — TOKEN_ENCRYPTION_KEY may have "
            "changed since it was written."
        ) from e
