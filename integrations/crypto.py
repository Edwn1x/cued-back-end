"""At-rest encryption for third-party OAuth tokens.

The FIRST encryption helper in the codebase (everything else is env-var secrets
+ HMAC). OAuth access/refresh tokens are long-lived credentials to a user's
Google/Strava account — they must never sit in the DB as plaintext, never be
logged, never reach the coach's context. Fernet (AES-128-CBC + HMAC) from the
`cryptography` lib, keyed by config.INTEGRATION_TOKEN_ENC_KEY.

Discipline: if the key is unset, encrypt() RAISES rather than storing plaintext.
A missing key is a misconfiguration to fix, not a silent downgrade.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

import config


class MissingEncryptionKey(RuntimeError):
    """Raised when INTEGRATION_TOKEN_ENC_KEY is absent — we refuse to store or
    read tokens in plaintext."""


def _box() -> Fernet:
    key = config.INTEGRATION_TOKEN_ENC_KEY
    if not key:
        raise MissingEncryptionKey(
            "INTEGRATION_TOKEN_ENC_KEY not set — refusing to handle integration "
            "tokens without at-rest encryption. Generate one with "
            "Fernet.generate_key()."
        )
    if isinstance(key, str):
        key = key.encode("utf-8")
    return Fernet(key)


def encrypt(plaintext: str | None) -> str | None:
    """Plaintext token -> Fernet ciphertext string. None passes through (a
    provider like bcourses has no token)."""
    if plaintext is None:
        return None
    return _box().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str | None) -> str | None:
    """Fernet ciphertext -> plaintext token. None/empty passes through. A
    ciphertext that doesn't verify raises InvalidToken (surfaces a key rotation
    or corruption problem loudly rather than returning a wrong token)."""
    if not ciphertext:
        return None
    return _box().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def key_configured() -> bool:
    return bool(config.INTEGRATION_TOKEN_ENC_KEY)


__all__ = ["encrypt", "decrypt", "key_configured", "MissingEncryptionKey", "InvalidToken"]
