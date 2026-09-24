"""Integration framework: provider registry + row lifecycle + token refresh/revoke.

One table (models.Integration), one refresh helper, one revoke path — every
provider is the same shape. Provider modules (gcal.py, strava.py) subclass
`Provider` and call `register(...)` on import; this module never imports them, so
Part 0 stands alone and the routes 404 for a provider that isn't wired yet.

Token discipline: access/refresh tokens live only as Fernet ciphertext (crypto.py)
and are decrypted at point of use. `status_line` is the ONLY thing the coach sees.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import config
from models import get_session, Integration
from integrations import crypto

logger = logging.getLogger("cued.integrations")

REFRESH_SKEW_S = 5 * 60   # refresh if the access token dies within 5 minutes


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── provider registry ────────────────────────────────────────────────────────

@dataclass
class TokenBundle:
    access_token: str | None = None
    refresh_token: str | None = None
    expires_at: datetime | None = None      # naive UTC
    scopes: str | None = None
    external_id: str | None = None


class Provider:
    """Interface a provider module implements. Subclass, set the class attrs, and
    call base.register(MyProvider())."""
    name: str = ""            # url slug + integrations.provider value, e.g. "gcal"
    label: str = ""           # human word for messages, e.g. "calendar", "strava"
    scopes: str = ""          # scopes requested at authorize time

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        raise NotImplementedError

    def exchange_code(self, code: str, *, redirect_uri: str, state: str | None = None) -> TokenBundle:
        """`state` is the connect token the authorize step was minted with. Providers
        that need per-handshake material (Fitbit's PKCE verifier) derive it from
        `state` rather than storing it; the others ignore it."""
        raise NotImplementedError

    def refresh(self, refresh_token: str) -> TokenBundle:
        raise NotImplementedError

    def connected_message(self, integ: Integration) -> str:
        """The one iMessage sent after a successful connect. ≤2 bubbles, plain."""
        return "connected. i'll take it from here"

    def enabled(self) -> bool:
        """Whether this provider's flag is on. Overridden per provider."""
        return True

    def sync_now(self, user_id: int) -> None:
        """Best-effort immediate pull right after a successful connect, so the user
        isn't blind until the next scheduled sync (they often ask 'what's on it?'
        seconds later). Default no-op; providers with a sync override it."""
        return None


PROVIDERS: dict[str, Provider] = {}


def register(provider: Provider) -> None:
    PROVIDERS[provider.name] = provider


def get_provider(name: str) -> Provider | None:
    return PROVIDERS.get(name)


# ─── row lifecycle ────────────────────────────────────────────────────────────

def get_integration(session, user_id: int, provider: str) -> Integration | None:
    return (session.query(Integration)
            .filter(Integration.user_id == user_id, Integration.provider == provider)
            .one_or_none())


def _get_or_create(session, user_id: int, provider: str) -> Integration:
    integ = get_integration(session, user_id, provider)
    if integ is None:
        integ = Integration(user_id=user_id, provider=provider, status="pending", meta={})
        session.add(integ)
        session.flush()
    return integ


def set_pending(user_id: int, provider: str, nonce: str, exp: int) -> None:
    """Record the in-flight connect handshake: status=pending + the single-use
    nonce the callback must match. Does not touch existing tokens (a re-connect
    of an already-connected provider just refreshes the nonce)."""
    session = get_session()
    try:
        integ = _get_or_create(session, user_id, provider)
        meta = dict(integ.meta or {})
        meta["connect_nonce"] = nonce
        meta["connect_exp"] = int(exp)
        integ.meta = meta
        if integ.status != "connected":
            integ.status = "pending"
        integ.updated_at = _utcnow()
        session.commit()
    finally:
        session.close()


def pending_nonce(user_id: int, provider: str) -> str | None:
    session = get_session()
    try:
        integ = get_integration(session, user_id, provider)
        return (integ.meta or {}).get("connect_nonce") if integ else None
    finally:
        session.close()


def complete_connection(user_id: int, provider: str, bundle: TokenBundle) -> None:
    """Success callback: store encrypted tokens, mark connected, burn the nonce."""
    session = get_session()
    try:
        integ = _get_or_create(session, user_id, provider)
        integ.status = "connected"
        if bundle.access_token is not None:
            integ.access_token = crypto.encrypt(bundle.access_token)
        if bundle.refresh_token is not None:
            integ.refresh_token = crypto.encrypt(bundle.refresh_token)
        integ.expires_at = bundle.expires_at
        integ.scopes = bundle.scopes
        integ.external_id = bundle.external_id
        meta = dict(integ.meta or {})
        meta.pop("connect_nonce", None)
        meta.pop("connect_exp", None)
        meta.pop("last_error", None)
        integ.meta = meta
        integ.updated_at = _utcnow()
        session.commit()
    finally:
        session.close()


def mark_error(user_id: int, provider: str, reason: str) -> None:
    session = get_session()
    try:
        integ = _get_or_create(session, user_id, provider)
        integ.status = "error"
        meta = dict(integ.meta or {})
        meta["last_error"] = str(reason)[:300]
        meta.pop("connect_nonce", None)
        integ.meta = meta
        integ.updated_at = _utcnow()
        session.commit()
    finally:
        session.close()
    logger.warning("INTEGRATION_ERROR user=%s provider=%s reason=%s", user_id, provider, str(reason)[:120])


def mark_revoked(user_id: int, provider: str) -> None:
    """Deauth / 401-after-refresh: clear tokens, keep the row so the coach can see
    the disconnected state once if relevant."""
    session = get_session()
    try:
        integ = get_integration(session, user_id, provider)
        if integ is None:
            return
        integ.status = "revoked"
        integ.access_token = None
        integ.refresh_token = None
        integ.expires_at = None
        integ.updated_at = _utcnow()
        session.commit()
    finally:
        session.close()
    logger.info("INTEGRATION_REVOKED user=%s provider=%s", user_id, provider)


class RefreshFailed(RuntimeError):
    pass


# ─── pull-style providers (feed URL / pasted token): sync bookkeeping ─────────

def note_sync_failure(user_id: int, provider: str, err: Exception, *, fails_before_error: int = 3) -> None:
    """A fetch failed. Count it in meta.fail_count; flip status to `error` only after
    `fails_before_error` consecutive misses so a transient blip never shows the coach
    an error state. `error` rows keep being polled and heal on the next good pull."""
    session = get_session()
    try:
        integ = get_integration(session, user_id, provider)
        if integ is None:
            return
        meta = dict(integ.meta or {})
        meta["fail_count"] = int(meta.get("fail_count") or 0) + 1
        meta["last_error"] = str(err)[:300]
        integ.meta = meta
        integ.updated_at = _utcnow()
        if meta["fail_count"] >= fails_before_error and integ.status == "connected":
            integ.status = "error"
        session.commit()
    finally:
        session.close()
    logger.warning("INTEGRATION_SYNC_FAILED user=%s provider=%s err=%s", user_id, provider, str(err)[:120])


def note_sync_success(user_id: int, provider: str, now: datetime | None = None, **meta_updates) -> None:
    n = now or _utcnow()
    session = get_session()
    try:
        integ = get_integration(session, user_id, provider)
        if integ is None:
            return
        meta = dict(integ.meta or {})
        meta["fail_count"] = 0
        meta.pop("last_error", None)
        meta["last_sync_at"] = n.isoformat()
        meta.update(meta_updates)
        integ.meta = meta
        integ.status = "connected"
        integ.updated_at = n
        session.commit()
    finally:
        session.close()


def redact_inbound(user_id: int, secret: str, placeholder: str) -> int:
    """A pasted feed URL / access token was logged as a normal inbound row before the
    pre-pass saw it. Scrub it from the stored body so the conversation window (and
    therefore the model) never carries the secret. Returns rows changed."""
    from models import Message
    if not secret:
        return 0
    session = get_session()
    try:
        rows = (session.query(Message)
                .filter(Message.user_id == user_id, Message.direction == "in",
                        Message.body.contains(secret))
                .order_by(Message.id.desc()).limit(3).all())
        for m in rows:
            m.body = m.body.replace(secret, placeholder)
        if rows:
            session.commit()
        return len(rows)
    finally:
        session.close()


def get_valid_access_token(user_id: int, provider: str) -> str | None:
    """Return a usable access token, refreshing first if it's within REFRESH_SKEW_S
    of expiry. On refresh failure, mark the row revoked and return None (the coach
    then sees `provider: disconnected`). None also for not-connected providers."""
    prov = get_provider(provider)
    session = get_session()
    try:
        integ = get_integration(session, user_id, provider)
        if integ is None or integ.status != "connected":
            return None
        access = crypto.decrypt(integ.access_token)
        refresh = crypto.decrypt(integ.refresh_token)
        expires_at = integ.expires_at
        needs = expires_at is not None and expires_at <= _utcnow() + timedelta(seconds=REFRESH_SKEW_S)
    finally:
        session.close()

    if not needs:
        return access
    if prov is None or not refresh:
        return access  # can't refresh (no provider wired or no refresh token) — try as-is

    try:
        bundle = prov.refresh(refresh)
    except Exception as e:
        logger.warning("INTEGRATION_REFRESH_FAILED user=%s provider=%s err=%s", user_id, provider, e)
        mark_revoked(user_id, provider)
        return None

    # persist the rotated tokens
    session = get_session()
    try:
        integ = get_integration(session, user_id, provider)
        if integ is not None:
            if bundle.access_token is not None:
                integ.access_token = crypto.encrypt(bundle.access_token)
            if bundle.refresh_token is not None:
                integ.refresh_token = crypto.encrypt(bundle.refresh_token)
            if bundle.expires_at is not None:
                integ.expires_at = bundle.expires_at
            integ.updated_at = _utcnow()
            session.commit()
    finally:
        session.close()
    return bundle.access_token or access


# ─── coach-facing status (NEVER a token) ──────────────────────────────────────

def _strava_scope_suffix(scopes: str | None) -> str:
    s = scopes or ""
    read = "activity:read" in s
    write = "activity:write" in s
    if read and write:
        return " (read+post)"
    if write:
        return " (post)"
    if read:
        return " (read)"
    return ""


def status_line(user_id: int) -> str | None:
    """The single line the coach sees, e.g.
        INTEGRATIONS: gcal connected · strava connected (read+post)
    Revoked/error providers surface too (so the coach can mention it once).
    Returns None when the user has no integration rows at all."""
    session = get_session()
    try:
        rows = (session.query(Integration)
                .filter(Integration.user_id == user_id)
                .order_by(Integration.provider).all())
        parts = []
        for r in rows:
            if r.status == "connected":
                extra = _strava_scope_suffix(r.scopes) if r.provider == "strava" else ""
                if r.provider == "canvas":
                    codes = [c for c in ((r.meta or {}).get("course_codes") or []) if c][:6]
                    if codes:
                        extra = " (" + " · ".join(codes) + ")"
                parts.append(f"{r.provider} connected{extra}")
            elif r.status == "revoked":
                parts.append(f"{r.provider} disconnected")
            elif r.status == "error":
                parts.append(f"{r.provider} error")
            # pending rows are in-flight — don't advertise them to the coach
    finally:
        session.close()
    return " · ".join(parts) if parts else None


__all__ = [
    "Provider", "TokenBundle", "PROVIDERS", "register", "get_provider",
    "get_integration", "set_pending", "pending_nonce", "complete_connection",
    "mark_error", "mark_revoked", "get_valid_access_token", "status_line",
    "RefreshFailed", "REFRESH_SKEW_S",
    "note_sync_failure", "note_sync_success", "redact_inbound",
]
