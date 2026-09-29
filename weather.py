"""
Weather — a concise line in the morning brief plus a reactive "what's the weather"
answer. Powered by open-meteo (https://open-meteo.com) — FREE, NO API KEY.
============================================================================

Two open-meteo endpoints, both keyless:
  * forecast:  https://api.open-meteo.com/v1/forecast — current temp + weather code +
    precipitation + today's high/low for a lat/lng.
  * geocoding: https://geocoding-api.open-meteo.com/v1/search — turn a city name a user
    states ("I'm in LA this week") into a lat/lng we store on the User.

Location resolution (fallback chain): per-user stored weather_lat/lng/place if set →
else the Berkeley default (config.WEATHER_DEFAULT_*). Auto-location is unreliable for an
SMS coach (no request IP, the wearable has no GPS, area codes lie), so we DEFAULT to
Berkeley and let the user correct it in plain language.

Failure envelope: EVERY failure (timeout, HTTP error, bad payload, no geocoding match)
degrades to None — the morning brief still sends without a weather line, and the reactive
answer says it can't pull the weather right now. Weather NEVER blocks a message.

A short per-process TTL cache (config.WEATHER_CACHE_TTL_S) keyed by rounded lat/lng keeps
us from hammering the API on every heartbeat tick / turn.
"""

from __future__ import annotations

import logging
import time

import requests

import config

logger = logging.getLogger("cued.weather")

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

# (lat_rounded, lng_rounded) -> (fetched_at_epoch, forecast_dict). Per-process, short TTL.
_CACHE: dict[tuple[float, float], tuple[float, dict]] = {}


# ─── location resolution ──────────────────────────────────────────────────────

def resolve_location(user) -> tuple[float, float, str]:
    """Per-user stored location if set, else the Berkeley default. Always returns a
    usable (lat, lng, place) — never None (Berkeley is the floor)."""
    lat = getattr(user, "weather_lat", None)
    lng = getattr(user, "weather_lng", None)
    if lat is not None and lng is not None:
        place = getattr(user, "weather_place", None) or "your area"
        return (float(lat), float(lng), place)
    return (config.WEATHER_DEFAULT_LAT, config.WEATHER_DEFAULT_LNG, config.WEATHER_DEFAULT_PLACE)


def geocode(city: str) -> tuple[float, float, str] | None:
    """City name → (lat, lng, label) via open-meteo geocoding (free, no key). None on
    any failure or no match — the caller keeps the existing/Berkeley location."""
    city = (city or "").strip()
    if not city:
        return None
    try:
        resp = requests.get(GEOCODING_URL,
                            params={"name": city, "count": 1, "language": "en", "format": "json"},
                            timeout=config.WEATHER_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("WEATHER_GEOCODE_FAILED city=%r err=%s", city, e)
        return None
    results = payload.get("results") or []
    if not results:
        logger.info("WEATHER_GEOCODE_NO_MATCH city=%r", city)
        return None
    top = results[0]
    try:
        lat = float(top["latitude"])
        lng = float(top["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    # A friendly label: "City, ST" / "City, Country" when the parts are present.
    name = str(top.get("name") or city).strip()
    region = str(top.get("admin1") or "").strip()
    country = str(top.get("country_code") or "").strip()
    if region and region.lower() != name.lower():
        label = f"{name}, {region}"
    elif country:
        label = f"{name}, {country}"
    else:
        label = name
    return (lat, lng, label[:120])


# ─── forecast fetch (cached, fail-open) ───────────────────────────────────────

def get_forecast(lat: float, lng: float) -> dict | None:
    """Current conditions + today's high/low for a lat/lng. Returns a dict:
        {temp_f, high_f, low_f, code, precip, is_rain, is_snow, description}
    or None on any failure (timeout / HTTP / payload). Cached per-process for
    config.WEATHER_CACHE_TTL_S keyed by rounded lat/lng."""
    key = (round(float(lat), 2), round(float(lng), 2))
    now = time.time()
    hit = _CACHE.get(key)
    if hit and (now - hit[0]) <= config.WEATHER_CACHE_TTL_S:
        return hit[1]

    t0 = time.time()
    try:
        resp = requests.get(FORECAST_URL, params={
            "latitude": lat, "longitude": lng,
            "current": "temperature_2m,weather_code,precipitation",
            "daily": "temperature_2m_max,temperature_2m_min",
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "forecast_days": 1,
        }, timeout=config.WEATHER_TIMEOUT_S)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("WEATHER_FORECAST_FAILED lat=%s lng=%s err=%s", lat, lng, e)
        return None

    parsed = _parse_forecast(payload)
    if parsed is None:
        logger.warning("WEATHER_FORECAST_BAD_PAYLOAD lat=%s lng=%s", lat, lng)
        return None
    _CACHE[key] = (now, parsed)
    ms = int((time.time() - t0) * 1000)
    logger.info("WEATHER_FORECAST lat=%s lng=%s ms=%d temp=%s code=%s",
                lat, lng, ms, parsed.get("temp_f"), parsed.get("code"))
    return parsed


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _parse_forecast(payload: dict) -> dict | None:
    cur = (payload or {}).get("current") or {}
    daily = (payload or {}).get("daily") or {}
    temp = _num(cur.get("temperature_2m"))
    code = cur.get("weather_code")
    if temp is None or code is None:
        return None
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    high_f = _num(highs[0]) if highs else None
    low_f = _num(lows[0]) if lows else None
    precip = _num(cur.get("precipitation")) or 0.0
    return {
        "temp_f": round(temp),
        "high_f": round(high_f) if high_f is not None else None,
        "low_f": round(low_f) if low_f is not None else None,
        "code": code,
        "precip": precip,
        "is_rain": _is_rain(code),
        "is_snow": _is_snow(code),
        "description": _weather_desc(code),
    }


# ─── WMO weather-code interpretation ──────────────────────────────────────────
# https://open-meteo.com/en/docs — WMO weather interpretation codes (WW).

def _is_rain(code: int) -> bool:
    return code in {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}


def _is_snow(code: int) -> bool:
    return code in {71, 73, 75, 77, 85, 86}


def _weather_desc(code: int) -> str:
    if code == 0:
        return "clear"
    if code in {1, 2}:
        return "partly cloudy"
    if code == 3:
        return "overcast"
    if code in {45, 48}:
        return "fog"
    if code in {51, 53, 55, 56, 57}:
        return "drizzle"
    if code in {61, 63, 65, 80, 81, 82}:
        return "rain"
    if code in {66, 67}:
        return "freezing rain"
    if code in {71, 73, 75, 77, 85, 86}:
        return "snow"
    if code in {95, 96, 99}:
        return "thunderstorms"
    return "mixed conditions"


def _hint(f: dict) -> str:
    """One short actionable clause. Precipitation first, then temperature."""
    temp = f.get("temp_f")
    if f.get("is_snow"):
        return "bundle up & watch for ice"
    if f.get("is_rain"):
        return "grab a jacket, good indoor-cardio day"
    if temp is not None and temp >= 85:
        return "hydrate & train early"
    if temp is not None and temp <= 45:
        return "layer up"
    if f.get("code") == 0:
        return "clear one — get outside if you can"
    return ""


# ─── surfaces ─────────────────────────────────────────────────────────────────

def weather_line(user) -> str | None:
    """The concise ONE-clause line for the morning brief, e.g.
        "46° & rain in Berkeley — grab a jacket, good indoor-cardio day"
    None (fail-open) when the flag is off or the fetch fails — the brief still sends."""
    if not config.WEATHER_ENABLED:
        return None
    lat, lng, place = resolve_location(user)
    f = get_forecast(lat, lng)
    if not f:
        return None
    head = f"{f['temp_f']}° & {f['description']}"
    where = f" in {place}" if place else ""
    hint = _hint(f)
    tail = f" — {hint}" if hint else ""
    return f"{head}{where}{tail}"


def weather_summary(user) -> str:
    """The reactive get_weather answer — a sane, fuller summary the coach turns into a
    friend's reply. Never raises; a fetch failure returns an honest can't-pull line."""
    lat, lng, place = resolve_location(user)
    if not config.WEATHER_ENABLED:
        return "weather isn't available right now"
    f = get_forecast(lat, lng)
    where = place or "your area"
    if not f:
        return f"couldn't pull the weather for {where} right now"
    bits = [f"{f['temp_f']}° and {f['description']} in {where}"]
    if f.get("high_f") is not None and f.get("low_f") is not None:
        bits.append(f"high {f['high_f']}° / low {f['low_f']}°")
    hint = _hint(f)
    if hint:
        bits.append(hint)
    return " — ".join(bits)


def set_weather_location_for_user(user_id: int, city: str) -> str | None:
    """Geocode `city` and store weather_lat/lng/place on the user. Returns the stored
    place label on success, None on a geocoding miss/failure (location unchanged)."""
    resolved = geocode(city)
    if not resolved:
        return None
    lat, lng, label = resolved
    from models import get_session, User

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return None
        user.weather_lat = lat
        user.weather_lng = lng
        user.weather_place = label
        session.commit()
        logger.info("WEATHER_LOCATION_SET user=%s place=%r lat=%s lng=%s", user_id, label, lat, lng)
        return label
    finally:
        session.close()
