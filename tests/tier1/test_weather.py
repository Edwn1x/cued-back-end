"""
Weather — the morning-brief line + reactive get_weather answer, powered by open-meteo
(FREE, NO API KEY). HTTP is MOCKED throughout (no network): forecast + geocoding fixtures
mirror the open-meteo shapes (current.{temperature_2m,weather_code,precipitation},
daily.temperature_2m_{max,min}; geocoding results[].{latitude,longitude,name,admin1}).

The point is the resolution chain + the failure envelope: unset location → Berkeley,
a stated city → geocode + store, and ANY API failure → fail-open (no brief line, an
honest reactive answer) — weather never blocks a message.
"""
from __future__ import annotations

import pytest

import config
import weather
from tests.factories import make_user


# ─── fake HTTP ──────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


def _forecast_payload(temp=46, code=61, precip=0.4, high=52, low=44):
    return {
        "current": {"temperature_2m": temp, "weather_code": code, "precipitation": precip},
        "daily": {"temperature_2m_max": [high], "temperature_2m_min": [low]},
    }


def _geocode_payload(name="Los Angeles", region="California", cc="US", lat=34.0522, lng=-118.2437):
    return {"results": [{"latitude": lat, "longitude": lng, "name": name,
                         "admin1": region, "country_code": cc}]}


def _patch_forecast(monkeypatch, payload=None, *, status=200, boom=None):
    def fake_get(url, params=None, timeout=None):
        if boom:
            raise boom
        return _FakeResp(payload if payload is not None else _forecast_payload(), status)
    monkeypatch.setattr(weather.requests, "get", fake_get)


@pytest.fixture(autouse=True)
def _clear_cache():
    """The per-process forecast cache is module-level — clear it around every test so a
    prior test's fixture doesn't satisfy this test's fetch."""
    weather._CACHE.clear()
    yield
    weather._CACHE.clear()


class _U:
    """Minimal user stand-in for the pure resolution/line helpers."""
    def __init__(self, lat=None, lng=None, place=None):
        self.id = 1
        self.weather_lat = lat
        self.weather_lng = lng
        self.weather_place = place


# ─── location resolution chain ────────────────────────────────────────────────

def test_unset_location_falls_back_to_berkeley():
    lat, lng, place = weather.resolve_location(_U())
    assert (round(lat, 4), round(lng, 4)) == (config.WEATHER_DEFAULT_LAT, config.WEATHER_DEFAULT_LNG)
    assert place == "Berkeley"


def test_stored_location_overrides_the_default():
    lat, lng, place = weather.resolve_location(_U(lat=34.05, lng=-118.24, place="Los Angeles, California"))
    assert (lat, lng, place) == (34.05, -118.24, "Los Angeles, California")


# ─── the brief line + actionable hint ─────────────────────────────────────────

def test_weather_line_rain_gets_a_jacket_hint(monkeypatch):
    _patch_forecast(monkeypatch, _forecast_payload(temp=46, code=61))  # 61 = rain
    line = weather.weather_line(_U())
    assert line is not None
    assert line.startswith("46° & rain in Berkeley")
    assert "jacket" in line and "indoor-cardio" in line


def test_weather_line_hot_gets_a_hydrate_hint(monkeypatch):
    _patch_forecast(monkeypatch, _forecast_payload(temp=92, code=0, precip=0))  # clear + hot
    line = weather.weather_line(_U())
    assert "92°" in line and "hydrate" in line


def test_weather_line_cold_and_dry_gets_a_layer_hint(monkeypatch):
    _patch_forecast(monkeypatch, _forecast_payload(temp=38, code=3, precip=0))  # overcast + cold
    line = weather.weather_line(_U())
    assert "38°" in line and "layer up" in line


def test_weather_line_snow_gets_a_bundle_hint(monkeypatch):
    _patch_forecast(monkeypatch, _forecast_payload(temp=30, code=73, precip=1.0))  # snow
    line = weather.weather_line(_U())
    assert "snow" in line and "bundle up" in line


# ─── failure envelope: fail-open ─────────────────────────────────────────────

def test_forecast_timeout_fails_open_no_line(monkeypatch):
    import requests
    _patch_forecast(monkeypatch, boom=requests.Timeout("slow"))
    assert weather.weather_line(_U()) is None


def test_forecast_http_error_fails_open_no_line(monkeypatch):
    _patch_forecast(monkeypatch, {}, status=500)
    assert weather.weather_line(_U()) is None


def test_forecast_bad_payload_fails_open(monkeypatch):
    _patch_forecast(monkeypatch, {"current": {}})  # no temp / code
    assert weather.get_forecast(1.0, 2.0) is None
    assert weather.weather_line(_U()) is None


def test_flag_off_is_inert(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_ENABLED", False)
    _patch_forecast(monkeypatch, _forecast_payload())  # would succeed if consulted
    assert weather.weather_line(_U()) is None
    assert weather.weather_summary(_U()) == "weather isn't available right now"


# ─── caching ─────────────────────────────────────────────────────────────────

def test_forecast_is_cached_within_ttl(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return _FakeResp(_forecast_payload())
    monkeypatch.setattr(weather.requests, "get", fake_get)
    weather.get_forecast(37.87, -122.27)
    weather.get_forecast(37.87, -122.27)
    assert calls["n"] == 1  # second read served from cache


# ─── geocoding + store (DB) ──────────────────────────────────────────────────

def test_geocode_builds_a_city_region_label(monkeypatch):
    monkeypatch.setattr(weather.requests, "get",
                        lambda url, params=None, timeout=None: _FakeResp(_geocode_payload()))
    lat, lng, label = weather.geocode("LA")
    assert (round(lat, 2), round(lng, 2)) == (34.05, -118.24)
    assert label == "Los Angeles, California"


def test_geocode_no_match_returns_none(monkeypatch):
    monkeypatch.setattr(weather.requests, "get",
                        lambda url, params=None, timeout=None: _FakeResp({"results": []}))
    assert weather.geocode("asdfghjkl") is None


def test_set_weather_location_stores_lat_lng_place(db, monkeypatch):
    user = make_user(db)
    monkeypatch.setattr(weather.requests, "get",
                        lambda url, params=None, timeout=None: _FakeResp(_geocode_payload(
                            name="Seattle", region="Washington", lat=47.6062, lng=-122.3321)))
    label = weather.set_weather_location_for_user(user.id, "seattle")
    assert label == "Seattle, Washington"
    db.expire_all()
    from models import User
    u = db.get(User, user.id)
    assert round(u.weather_lat, 2) == 47.61 and round(u.weather_lng, 2) == -122.33
    assert u.weather_place == "Seattle, Washington"


def test_set_weather_location_geocode_miss_leaves_user_unchanged(db, monkeypatch):
    user = make_user(db)
    monkeypatch.setattr(weather.requests, "get",
                        lambda url, params=None, timeout=None: _FakeResp({"results": []}))
    assert weather.set_weather_location_for_user(user.id, "nowhere") is None
    db.expire_all()
    from models import User
    u = db.get(User, user.id)
    assert u.weather_lat is None and u.weather_place is None


# ─── tool handlers ───────────────────────────────────────────────────────────

def test_get_weather_tool_returns_a_sane_summary(db, monkeypatch):
    import agent_tools
    user = make_user(db)
    _patch_forecast(monkeypatch, _forecast_payload(temp=46, code=61, high=52, low=44))
    out = agent_tools.handle_get_weather(user.id, {})
    assert out.startswith("ok:")
    assert "46°" in out and "rain" in out and "Berkeley" in out
    assert "high 52°" in out and "low 44°" in out


def test_get_weather_tool_fails_open_on_api_error(db, monkeypatch):
    import agent_tools, requests
    user = make_user(db)
    _patch_forecast(monkeypatch, boom=requests.Timeout("slow"))
    out = agent_tools.handle_get_weather(user.id, {})
    assert out.startswith("ok:") and "couldn't pull the weather" in out


def test_set_weather_location_tool_geocodes_and_stores(db, monkeypatch):
    import agent_tools
    user = make_user(db)
    monkeypatch.setattr(weather.requests, "get",
                        lambda url, params=None, timeout=None: _FakeResp(_geocode_payload()))
    out = agent_tools.handle_set_weather_location(user.id, {"city": "LA"})
    assert out.startswith("ok:") and "Los Angeles, California" in out


def test_set_weather_location_tool_reports_a_miss(db, monkeypatch):
    import agent_tools
    user = make_user(db)
    monkeypatch.setattr(weather.requests, "get",
                        lambda url, params=None, timeout=None: _FakeResp({"results": []}))
    out = agent_tools.handle_set_weather_location(user.id, {"city": "zzzz"})
    assert out.startswith("error:") and "couldn't find" in out


# ─── morning-brief integration ───────────────────────────────────────────────

def test_briefing_extras_includes_the_weather_line(db, monkeypatch):
    import heartbeat
    user = make_user(db)
    _patch_forecast(monkeypatch, _forecast_payload(temp=46, code=61))
    material = heartbeat._daily_briefing_extras(user, db)
    assert "Weather: 46° & rain in Berkeley" in material


def test_briefing_extras_fails_open_when_weather_down(db, monkeypatch):
    """A failing weather fetch must NOT crash the brief — it just omits the line."""
    import heartbeat, requests
    user = make_user(db)
    _patch_forecast(monkeypatch, boom=requests.Timeout("slow"))
    material = heartbeat._daily_briefing_extras(user, db)  # must not raise
    assert "Weather:" not in material
