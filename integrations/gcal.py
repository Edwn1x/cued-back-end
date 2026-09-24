"""Google Calendar provider (Part 1).

Two layers:
  1. GCalProvider — the OAuth half, plugged into the Part 0 framework (authorize →
     exchange → refresh). Scope is calendar.events.readonly ONLY (narrowest that
     lists events); access_type=offline + prompt=consent so a refresh token comes
     back. external_id = the Google account `sub`.
  2. Thin Calendar API client — list_calendars + list_events (incremental via
     syncToken, 410 → caller does a full resync). The sync ORCHESTRATION (upsert
     into the event store, the 30-min job) lives in the sync layer (§1.2), not here.

Verified against current Google docs at build time (2026-09-18): auth endpoint
accounts.google.com/o/oauth2/v2/auth, token oauth2.googleapis.com/token, events.list
syncToken is incompatible with timeMin/timeMax and 410s on expiry.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests

import config
from integrations import base
from integrations.base import Provider, TokenBundle

logger = logging.getLogger("cued.integrations.gcal")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://www.googleapis.com/oauth2/v1/userinfo"
CAL_API = "https://www.googleapis.com/calendar/v3"
# events.readonly alone can read events but NOT list calendars — calendarList.list
# 403'd live (2026-09-24). calendarlist.readonly is the narrowest scope that lists them;
# still no write, no calendar.readonly (that one exposes ACLs/settings).
SCOPE = ("https://www.googleapis.com/auth/calendar.events.readonly "
         "https://www.googleapis.com/auth/calendar.calendarlist.readonly")


def _timeout() -> int:
    return getattr(config, "INTEGRATIONS_HTTP_TIMEOUT_S", 15)


def _expires_at(expires_in) -> datetime | None:
    try:
        return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


class GCalProvider(Provider):
    name = "gcal"
    label = "calendar"
    scopes = SCOPE

    def enabled(self) -> bool:
        return bool(config.GCAL_ENABLED)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scopes,
            "access_type": "offline",   # get a refresh token
            "prompt": "consent",        # ensure the refresh token comes every time
            "include_granted_scopes": "true",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, *, redirect_uri: str) -> TokenBundle:
        resp = requests.post(TOKEN_URL, data={
            "code": code,
            "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=_timeout())
        resp.raise_for_status()
        tok = resp.json()
        return TokenBundle(
            access_token=tok.get("access_token"),
            refresh_token=tok.get("refresh_token"),
            expires_at=_expires_at(tok.get("expires_in")),
            scopes=tok.get("scope") or self.scopes,
            external_id=self._account_sub(tok.get("access_token")),
        )

    def refresh(self, refresh_token: str) -> TokenBundle:
        resp = requests.post(TOKEN_URL, data={
            "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=_timeout())
        resp.raise_for_status()
        tok = resp.json()
        # Google does NOT return a new refresh_token on refresh — keep the existing
        # one (base only overwrites when refresh_token is non-None).
        return TokenBundle(
            access_token=tok.get("access_token"),
            refresh_token=tok.get("refresh_token"),   # normally None → base keeps old
            expires_at=_expires_at(tok.get("expires_in")),
            scopes=tok.get("scope"),
        )

    def _account_sub(self, access_token: str | None) -> str | None:
        if not access_token:
            return None
        try:
            r = requests.get(USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"},
                             timeout=_timeout())
            r.raise_for_status()
            return str(r.json().get("id") or "") or None
        except Exception:
            logger.info("GCAL_USERINFO_UNAVAILABLE (no profile scope) — using primary calendar id")
        try:
            r = requests.get(f"{CAL_API}/users/me/calendarList/primary",
                             headers={"Authorization": f"Bearer {access_token}"},
                             timeout=_timeout())
            r.raise_for_status()
            return (str(r.json().get("id") or "") or None)
        except Exception:
            logger.warning("GCAL_ACCOUNT_ID_FAILED", exc_info=True)
            return None

    def connected_message(self, integ) -> str:
        return "connected. i'll pull ur calendar in and plan around it"


# ─── Calendar API client (read) — used by the §1.2 sync layer ─────────────────

class SyncTokenExpired(Exception):
    """410 GONE — the caller must drop the stored syncToken and do a full resync."""


def list_calendars(access_token: str) -> list[dict]:
    """Every calendar on the account the user hasn't hidden (calendarList.list).
    Returns the raw items (id, summary, selected, primary, ...)."""
    out, page = [], None
    while True:
        params = {"maxResults": 250, "showHidden": False}
        if page:
            params["pageToken"] = page
        r = requests.get(f"{CAL_API}/users/me/calendarList",
                         headers={"Authorization": f"Bearer {access_token}"},
                         params=params, timeout=_timeout())
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("items", []))
        page = body.get("nextPageToken")
        if not page:
            return out


def list_events(access_token: str, calendar_id: str, *, sync_token: str | None = None,
                time_min: str | None = None, time_max: str | None = None) -> tuple[list[dict], str | None]:
    """One incremental (or full) pull for a calendar. Returns (events, next_sync_token).

    With a sync_token: incremental — do NOT pass timeMin/timeMax (Google rejects the
    combination). Without one: a full pull, pass time_min/time_max (RFC3339). A 410
    GONE raises SyncTokenExpired so the caller drops the token and re-pulls in full.
    singleEvents=true expands recurring events into instances."""
    out, page, next_sync = [], None, None
    while True:
        params = {"singleEvents": "true", "maxResults": 2500, "showDeleted": True}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["orderBy"] = "startTime"
            if time_min:
                params["timeMin"] = time_min
            if time_max:
                params["timeMax"] = time_max
        if page:
            params["pageToken"] = page
        r = requests.get(f"{CAL_API}/calendars/{calendar_id}/events",
                         headers={"Authorization": f"Bearer {access_token}"},
                         params=params, timeout=_timeout())
        if r.status_code == 410:
            raise SyncTokenExpired(calendar_id)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("items", []))
        page = body.get("nextPageToken")
        if page:
            continue
        next_sync = body.get("nextSyncToken")
        return out, next_sync


base.register(GCalProvider())
