"""Single-use, short-lived connect-link token.

Same HMAC family as the card token (card_page.py) — a signed, self-describing
string, no server session — with two additions the OAuth handshake needs:
  1. an expiry baked into the signature (30 min default), and
  2. a random nonce that the integration row records when the link is minted and
     clears on first successful callback, so a captured link can't be replayed.

Format:  "<user_id>.<provider>.<exp>.<nonce>.<mac>"
Providers are lowercase [a-z]; they never contain a ".", so the 4-dot split is
unambiguous. `verify_connect_token` checks shape, expiry, and MAC; the single-use
check (nonce == the row's stored nonce) is enforced by the caller against the
Integration row, since that's where the mint recorded it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time

import config

TOKEN_TTL_S = 30 * 60          # 30 minutes
_TOKEN_BYTES = 18
_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{1,15}$")


def _secret() -> bytes:
    return (config.CONNECT_TOKEN_SECRET or config.CARD_TOKEN_SECRET
            or config.PROFILE_TOKEN_SECRET or config.FLASK_SECRET_KEY).encode("utf-8")


def _mac(user_id: int, provider: str, exp: int, nonce: str) -> str:
    msg = f"connect:{user_id}:{provider}:{exp}:{nonce}".encode("utf-8")
    digest = hmac.new(_secret(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:_TOKEN_BYTES]).decode("ascii").rstrip("=")


def new_nonce() -> str:
    return secrets.token_urlsafe(12)


def connect_token(user_id: int, provider: str, *, nonce: str | None = None,
                  exp: int | None = None, ttl_s: int = TOKEN_TTL_S) -> tuple[str, str]:
    """Return (token, nonce). Persist `nonce` on the Integration row so the
    callback can enforce single use."""
    if not _PROVIDER_RE.match(provider):
        raise ValueError(f"bad provider {provider!r}")
    nonce = nonce or new_nonce()
    exp = int(exp if exp is not None else time.time() + ttl_s)
    token = f"{int(user_id)}.{provider}.{exp}.{nonce}.{_mac(int(user_id), provider, exp, nonce)}"
    return token, nonce


def verify_connect_token(token: str | None, *, now: float | None = None) -> tuple[int, str, str] | None:
    """(user_id, provider, nonce) or None. Malformed, expired, or tampered → None.
    The nonce is returned so the caller can compare it to the stored row nonce
    (single-use). This function does NOT enforce single use by itself."""
    if not token or token.count(".") != 4:
        return None
    u, provider, e, nonce, mac = token.split(".")
    if not (u.isdigit() and e.isdigit()) or len(u) > 12 or len(e) > 12:
        return None
    if not _PROVIDER_RE.match(provider):
        return None
    if int(e) < (now if now is not None else time.time()):
        return None
    if not hmac.compare_digest(mac, _mac(int(u), provider, int(e), nonce)):
        return None
    return int(u), provider, nonce


__all__ = ["connect_token", "verify_connect_token", "new_nonce", "TOKEN_TTL_S"]
