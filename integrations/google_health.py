"""Google Health API provider (Part 2a) — Fitbit / Fitbit Air / Pixel Watch, read-only.

The legacy Fitbit Web API is turned down on 2026-09-30 (dev.fitbit.com carries the
notice); the Google Health API is its successor. Verified against
developers.google.com/health on 2026-09-24:

  auth      the normal Google OAuth 2.0 endpoints, so this reuses the gcal client
            (GOOGLE_OAUTH_CLIENT_ID / SECRET) — the Health API just has to be enabled on
            that Cloud project and its scopes added on the consent screen.
  scopes    googlehealth.activity_and_fitness.readonly (steps, calories, AZM)
            googlehealth.health_metrics_and_measurements.readonly (resting HR, HRV, weight)
            googlehealth.sleep.readonly
            All three are RESTRICTED scopes: fine in Testing mode for listed test users
            (≤100), OAuth verification + CASA before a public launch.
  base      https://health.googleapis.com/v4 ; users/me = the token's owner
  reads     dataPoints:dailyRollUp for interval types (steps / total-calories /
            active-zone-minutes; end date EXCLUSIVE, total-calories max 14 days),
            dataPoints (list) with a date filter for the daily types
            (daily-resting-heart-rate / daily-heart-rate-variability) and weight,
            dataPoints:reconcile for sleep sessions (type MAIN_SLEEP vs NAP).
  identity  GET users/me/identity → healthUserId (the webhook payload's user key).

DO NOT pass include_granted_scopes=true here: if the user ever consented to legacy
fitness.* scopes on this client they get unioned into the token and Health calls 403.
The sync ORCHESTRATION lives in google_health_sync.py, not here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta, date
from urllib.parse import urlencode

import requests

import config
from integrations import base
from integrations.base import Provider, TokenBundle

logger = logging.getLogger("cued.integrations.google_health")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://health.googleapis.com/v4"
SCOPE = ("https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly "
         "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly "
         "https://www.googleapis.com/auth/googlehealth.sleep.readonly")
ALL_SOURCES = "users/me/dataSourceFamilies/all-sources"
WEARABLES = "users/me/dataSourceFamilies/google-wearables"
ROLLUP_MAX_DAYS = {"total-calories": 14}      # the documented 14-day cap; others 90


def _timeout() -> int:
    return getattr(config, "INTEGRATIONS_HTTP_TIMEOUT_S", 15)


def _expires_at(expires_in) -> datetime | None:
    try:
        return (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


class GoogleHealthProvider(Provider):
    name = "google_health"
    label = "fitbit"           # the word users say; covers Fitbit Air / Pixel Watch too
    scopes = SCOPE

    def enabled(self) -> bool:
        return bool(config.GOOGLE_HEALTH_ENABLED)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        params = {
            "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self.scopes,
            "access_type": "offline",   # refresh token
            "prompt": "consent",        # …every time, even on re-connect
            "state": state,
            # deliberately NO include_granted_scopes (see module docstring)
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
            external_id=self._health_user_id(tok.get("access_token")),
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
        # Google does NOT rotate refresh tokens — None here keeps the stored one.
        return TokenBundle(
            access_token=tok.get("access_token"),
            refresh_token=tok.get("refresh_token"),
            expires_at=_expires_at(tok.get("expires_in")),
            scopes=tok.get("scope"),
        )

    def _health_user_id(self, access_token: str | None) -> str | None:
        if not access_token:
            return None
        try:
            return get_identity(access_token).get("healthUserId") or None
        except Exception:
            logger.warning("GOOGLE_HEALTH_IDENTITY_FAILED", exc_info=True)
            return None

    def connected_message(self, integ) -> str:
        return "connected. i'll see ur sleep, steps and heart rate from here — no need to tell me"

    def sync_now(self, user_id: int):
        """First backfill right after connect, so the next reply already has last night."""
        from integrations import google_health_sync
        return google_health_sync.sync_user(user_id, days=config.GOOGLE_HEALTH_BACKFILL_DAYS)


# ─── Health API client (read) — used by google_health_sync ───────────────────

class HealthAPIError(RuntimeError):
    def __init__(self, status: int, path: str, body: str = ""):
        super().__init__(f"google health {status} on {path}: {body[:160]}")
        self.status = status
        self.path = path


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}


def _get(access_token: str, path: str, *, params: dict | None = None) -> dict:
    r = requests.get(f"{API}{path}", headers=_headers(access_token), params=params, timeout=_timeout())
    if r.status_code >= 400:
        raise HealthAPIError(r.status_code, path, r.text or "")
    return r.json() if r.text else {}


def _post(access_token: str, path: str, body: dict) -> dict:
    r = requests.post(f"{API}{path}", headers=_headers(access_token), json=body, timeout=_timeout())
    if r.status_code >= 400:
        raise HealthAPIError(r.status_code, path, r.text or "")
    return r.json() if r.text else {}


def _ymd(d: date) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day}


def _date_of(node) -> str | None:
    """{year,month,day} (or {date:{...}}) → 'YYYY-MM-DD'."""
    if not isinstance(node, dict):
        return None
    d = node.get("date") if "date" in node and isinstance(node.get("date"), dict) else node
    try:
        return date(int(d["year"]), int(d["month"]), int(d["day"])).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def _num(v, cast=float):
    """Health API int64s arrive as strings ("8430"); tolerate both."""
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return None


def get_identity(access_token: str) -> dict:
    return _get(access_token, "/users/me/identity")


def daily_rollup(access_token: str, data_type: str, start: date, end_inclusive: date) -> list[dict]:
    """dataPoints:dailyRollUp, one civil day per window. The API's `end` is EXCLUSIVE
    (start Jul 28 / end Jul 30 → Jul 28 + Jul 29), so pass the inclusive end and we add a
    day. pageSize must not exceed the type's max duration (windowSizeDays × pageSize)."""
    days = (end_inclusive - start).days + 1
    cap = ROLLUP_MAX_DAYS.get(data_type, 90)
    if days > cap:
        start = end_inclusive - timedelta(days=cap - 1)
        days = cap
    body = {
        "range": {"start": {"date": _ymd(start)}, "end": {"date": _ymd(end_inclusive + timedelta(days=1))}},
        "windowSizeDays": 1,
        "pageSize": days,
        "dataSourceFamily": ALL_SOURCES,
    }
    resp = _post(access_token, f"/users/me/dataTypes/{data_type}/dataPoints:dailyRollUp", body)
    return list(resp.get("rollupDataPoints") or [])


def get_steps_by_day(access_token: str, start: date, end: date) -> dict[str, int]:
    out = {}
    for p in daily_rollup(access_token, "steps", start, end):
        d = _date_of(p.get("civilStartTime"))
        n = _num((p.get("steps") or {}).get("countSum"), int)
        if d and n is not None:
            out[d] = n
    return out


def get_calories_by_day(access_token: str, start: date, end: date) -> dict[str, int]:
    out = {}
    for p in daily_rollup(access_token, "total-calories", start, end):
        d = _date_of(p.get("civilStartTime"))
        n = _num((p.get("totalCalories") or {}).get("kcalSum"), int)
        if d and n:
            out[d] = n
    return out


def get_active_zone_minutes_by_day(access_token: str, start: date, end: date) -> dict[str, int]:
    """Fat-burn + cardio + peak minutes per day (Fitbit's Active Zone Minutes)."""
    out = {}
    for p in daily_rollup(access_token, "active-zone-minutes", start, end):
        d = _date_of(p.get("civilStartTime"))
        v = p.get("activeZoneMinutes") or {}
        total = sum(_num(v.get(k), int) or 0 for k in
                    ("sumInFatBurnHeartZone", "sumInCardioHeartZone", "sumInPeakHeartZone"))
        if d:
            out[d] = total
    return out


def _list_all(access_token: str, path: str, params: dict) -> list[dict]:
    out, token = [], None
    while True:
        p = dict(params)
        if token:
            p["pageToken"] = token
        body = _get(access_token, path, params=p)
        out.extend(body.get("dataPoints") or [])
        token = body.get("nextPageToken")
        if not token:
            return out


def get_resting_hr_by_day(access_token: str, start: date, end: date) -> dict[str, int]:
    pts = _list_all(access_token, "/users/me/dataTypes/daily-resting-heart-rate/dataPoints",
                    {"filter": f'daily_resting_heart_rate.date >= "{start.isoformat()}"'})
    out = {}
    for dp in pts:
        v = dp.get("dailyRestingHeartRate") or {}
        d = _date_of(v.get("date"))
        bpm = _num(v.get("beatsPerMinute"), int)
        if d and bpm and start.isoformat() <= d <= end.isoformat():
            out[d] = bpm
    return out


def get_hrv_by_day(access_token: str, start: date, end: date) -> dict[str, float]:
    pts = _list_all(access_token, "/users/me/dataTypes/daily-heart-rate-variability/dataPoints",
                    {"filter": f'daily_heart_rate_variability.date >= "{start.isoformat()}"'})
    out = {}
    for dp in pts:
        v = dp.get("dailyHeartRateVariability") or {}
        d = _date_of(v.get("date"))
        ms = _num(v.get("averageHeartRateVariabilityMilliseconds"))
        if d and ms and start.isoformat() <= d <= end.isoformat():
            out[d] = ms
    return out


def get_sleep_sessions(access_token: str, start: date) -> list[dict]:
    """Reconciled sleep sessions ending on/after `start` (civil). Each: {interval:{startTime,
    endTime,startUtcOffset,endUtcOffset}, type: MAIN_SLEEP|NAP, summary:{minutesAsleep,…}}."""
    body = _get(access_token, "/users/me/dataTypes/sleep/dataPoints:reconcile",
                params={"dataSourceFamily": WEARABLES,
                        "filter": f'sleep.interval.civil_end_time >= "{start.isoformat()}"'})
    return [dp.get("sleep") | {"name": dp.get("name")} for dp in (body.get("dataPoints") or []) if dp.get("sleep")]


def get_weight_samples(access_token: str, start: date) -> list[dict]:
    """Weight samples since `start` (UTC midnight). Each: {name, sample_time_iso, lbs}.
    The REST schema documents weightGrams; the RPC schema says weight_kg — accept both."""
    since = datetime(start.year, start.month, start.day, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    pts = _list_all(access_token, "/users/me/dataTypes/weight/dataPoints",
                    {"filter": f'weight.sample_time.physical_time >= "{since}"'})
    out = []
    for dp in pts:
        w = dp.get("weight") or {}
        kg = _num(w.get("weightKg"))
        if kg is None and w.get("weightGrams") is not None:
            g = _num(w.get("weightGrams"))
            kg = g / 1000.0 if g is not None else None
        st = (w.get("sampleTime") or {})
        when = st.get("physicalTime") or st.get("civilTime")
        if kg and dp.get("name"):
            out.append({"name": dp["name"], "sample_time": when, "lbs": round(kg * 2.20462, 1)})
    return out


base.register(GoogleHealthProvider())
