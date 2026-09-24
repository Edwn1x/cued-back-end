"""Fitbit provider (Part 2a) — the OAuth half + a thin Web API read client.

Fitbit accounts sign in with Google, but the Fitbit Web API is its OWN OAuth server
with its own app registration (dev.fitbit.com) and scopes — the gcal grant can't be
widened into it. Verified against dev.fitbit.com on 2026-09-24:

  authorize  https://www.fitbit.com/oauth2/authorize   (PKCE S256 — required for
             Personal/Client apps, recommended for Server; we always send it)
  token      https://api.fitbit.com/oauth2/token       (HTTP Basic client_id:secret)
  access tokens live 8h; REFRESH TOKENS ROTATE — single-use, a new one comes back on
  every refresh (base persists a returned refresh token, so this just works; the
  scheduler's max_instances=1 keeps two refreshes from racing in one process).

PKCE without a new column: the verifier is derived from the connect-link `state`
(HMAC with the connect secret), so authorize_url and exchange_code recompute the same
value from the same token. The state is single-use + 30-min anyway (tokens.py).

The sync ORCHESTRATION (rows in wearable_days / weight_logs, the 30-min job, the
context block) lives in fitbit_sync.py, not here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests

import config
from integrations import base
from integrations.base import Provider, TokenBundle

logger = logging.getLogger("cued.integrations.fitbit")

AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
TOKEN_URL = "https://api.fitbit.com/oauth2/token"
API = "https://api.fitbit.com"
# activity = steps/calories/active minutes; heartrate = resting HR + HRV; sleep; weight =
# scale readings; profile = the account's id/timezone. NOT nutrition (Cued is the food
# log), NOT location/social/settings.
SCOPE = "activity heartrate sleep weight profile"
SUBSCRIPTION_COLLECTIONS = ("activities", "sleep", "body")


def _timeout() -> int:
    return getattr(config, "INTEGRATIONS_HTTP_TIMEOUT_S", 15)


def _expires_at(expires_in) -> datetime | None:
    try:
        return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


# ─── PKCE (derived from the connect state, never stored) ─────────────────────

def _pkce_secret() -> bytes:
    return (config.CONNECT_TOKEN_SECRET or config.CARD_TOKEN_SECRET
            or config.PROFILE_TOKEN_SECRET or config.FLASK_SECRET_KEY).encode("utf-8")


def code_verifier(state: str) -> str:
    """43-char base64url of HMAC-SHA256(secret, 'pkce:' + state). Deterministic per
    state so the callback can recompute it; unguessable without the server secret."""
    digest = hmac.new(_pkce_secret(), f"pkce:{state}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def code_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")


def _basic_auth() -> str:
    raw = f"{config.FITBIT_CLIENT_ID}:{config.FITBIT_CLIENT_SECRET}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _token_request(data: dict) -> dict:
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if config.FITBIT_CLIENT_SECRET:
        headers["Authorization"] = _basic_auth()
    resp = requests.post(TOKEN_URL, data=data, headers=headers, timeout=_timeout())
    resp.raise_for_status()
    return resp.json()


class FitbitProvider(Provider):
    name = "fitbit"
    label = "fitbit"
    scopes = SCOPE

    def enabled(self) -> bool:
        return bool(config.FITBIT_ENABLED)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": config.FITBIT_CLIENT_ID,
            "response_type": "code",
            "scope": self.scopes,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge(code_verifier(state)),
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, *, redirect_uri: str, state: str | None = None) -> TokenBundle:
        if not state:
            raise ValueError("fitbit exchange needs the connect state (PKCE verifier is derived from it)")
        tok = _token_request({
            "client_id": config.FITBIT_CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier(state),
        })
        return TokenBundle(
            access_token=tok.get("access_token"),
            refresh_token=tok.get("refresh_token"),
            expires_at=_expires_at(tok.get("expires_in")),
            scopes=tok.get("scope") or self.scopes,
            external_id=(str(tok.get("user_id") or "") or None),   # the subscription ownerId
        )

    def refresh(self, refresh_token: str) -> TokenBundle:
        tok = _token_request({
            "client_id": config.FITBIT_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        })
        # Fitbit ROTATES: the old refresh token is dead after this call. Return the new
        # one so base persists it (base overwrites whenever refresh_token is non-None).
        return TokenBundle(
            access_token=tok.get("access_token"),
            refresh_token=tok.get("refresh_token"),
            expires_at=_expires_at(tok.get("expires_in")),
            scopes=tok.get("scope"),
        )

    def connected_message(self, integ) -> str:
        return "connected. i'll see ur sleep, steps and heart rate from here — no need to tell me"

    def sync_now(self, user_id: int):
        """Right after connect: subscribe (so Fitbit pushes) + the first backfill pull, so
        the very next reply already has last night's sleep instead of waiting 30 min."""
        from integrations import fitbit_sync
        token = base.get_valid_access_token(user_id, self.name)
        if token:
            try:
                subscribe_all(token, user_id)
            except Exception as e:  # best-effort; polling covers it
                logger.warning("FITBIT_SUBSCRIBE_FAILED user=%s err=%s", user_id, e)
        return fitbit_sync.sync_user(user_id, days=config.FITBIT_BACKFILL_DAYS)


# ─── Web API client (read) — used by fitbit_sync ─────────────────────────────

class FitbitAPIError(RuntimeError):
    def __init__(self, status: int, path: str, body: str = ""):
        super().__init__(f"fitbit {status} on {path}: {body[:160]}")
        self.status = status
        self.path = path


def _get(access_token: str, path: str, *, params: dict | None = None) -> dict:
    """GET api.fitbit.com/<path> as the token's owner (`user/-`). Accept-Language en_US
    makes weights come back in POUNDS (default is metric)."""
    r = requests.get(f"{API}{path}",
                     headers={"Authorization": f"Bearer {access_token}",
                              "Accept-Language": "en_US", "Accept": "application/json"},
                     params=params, timeout=_timeout())
    if r.status_code >= 400:
        raise FitbitAPIError(r.status_code, path, r.text or "")
    return r.json() if r.text else {}


def get_steps_series(access_token: str, start: str, end: str) -> dict[str, int]:
    """{'YYYY-MM-DD': steps} for the range (activities/steps time series)."""
    body = _get(access_token, f"/1/user/-/activities/steps/date/{start}/{end}.json")
    out = {}
    for row in body.get("activities-steps") or []:
        try:
            out[row["dateTime"]] = int(float(row.get("value") or 0))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def get_resting_hr_series(access_token: str, start: str, end: str) -> dict[str, int]:
    """{'YYYY-MM-DD': resting bpm} — days without a reading are omitted."""
    body = _get(access_token, f"/1/user/-/activities/heart/date/{start}/{end}.json")
    out = {}
    for row in body.get("activities-heart") or []:
        rhr = (row.get("value") or {}).get("restingHeartRate")
        if rhr is not None and row.get("dateTime"):
            try:
                out[row["dateTime"]] = int(rhr)
            except (TypeError, ValueError):
                continue
    return out


def get_hrv_series(access_token: str, start: str, end: str) -> dict[str, float]:
    """{'YYYY-MM-DD': dailyRmssd ms} keyed by the sleep-log date (the morning)."""
    body = _get(access_token, f"/1/user/-/hrv/date/{start}/{end}.json")
    out = {}
    for row in body.get("hrv") or []:
        v = (row.get("value") or {}).get("dailyRmssd")
        if v is not None and row.get("dateTime"):
            try:
                out[row["dateTime"]] = float(v)
            except (TypeError, ValueError):
                continue
    return out


def get_sleep_range(access_token: str, start: str, end: str) -> list[dict]:
    """Raw sleep logs (v1.2) for the range (≤100 days). Each has dateOfSleep (the
    morning it ends), isMainSleep, minutesAsleep, startTime/endTime (LOCAL, no zone),
    efficiency, levels.summary."""
    body = _get(access_token, f"/1.2/user/-/sleep/date/{start}/{end}.json")
    return list(body.get("sleep") or [])


def get_activity_summary(access_token: str, day: str) -> dict:
    """The daily summary for ONE day: caloriesOut, veryActiveMinutes, fairlyActiveMinutes,
    steps, restingHeartRate. Used for today only (the series calls cover the rest)."""
    body = _get(access_token, f"/1/user/-/activities/date/{day}.json")
    return dict(body.get("summary") or {})


def get_weight_logs(access_token: str, day: str) -> list[dict]:
    """Scale/manual weight entries logged ON that day, in pounds (Accept-Language en_US).
    Each: logId, weight, date, time ('HH:MM:SS'), source."""
    body = _get(access_token, f"/1/user/-/body/log/weight/date/{day}.json")
    return list(body.get("weight") or [])


def get_profile(access_token: str) -> dict:
    body = _get(access_token, "/1/user/-/profile.json")
    return dict(body.get("user") or {})


def subscribe(access_token: str, collection: str, subscription_id: str) -> int:
    """Create a push subscription for one collection. 201 new, 200 already there,
    409 = the user is subscribed under a different id (treated as fine — data still
    flows to our subscriber endpoint). Anything else raises."""
    path = f"/1/user/-/{collection}/apiSubscriptions/{subscription_id}.json"
    r = requests.post(f"{API}{path}", headers={"Authorization": f"Bearer {access_token}",
                                                "Accept": "application/json"},
                      timeout=_timeout())
    if r.status_code in (200, 201, 409):
        return r.status_code
    raise FitbitAPIError(r.status_code, path, r.text or "")


def subscribe_all(access_token: str, user_id: int) -> dict[str, int]:
    """One subscription per collection we read; id = '<user_id>-<collection>' so a
    notification's subscriptionId is self-describing. Skipped entirely when no
    verify code is configured (the endpoint can't have been verified with Fitbit)."""
    if not config.FITBIT_SUBSCRIBER_VERIFY_CODE:
        return {}
    out = {}
    for coll in SUBSCRIPTION_COLLECTIONS:
        out[coll] = subscribe(access_token, coll, f"{user_id}-{coll}")
    logger.info("FITBIT_SUBSCRIBED user=%s %s", user_id, out)
    return out


base.register(FitbitProvider())
