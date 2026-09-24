"""Part 2a — Google Health API provider (Fitbit / Pixel Watch) + read client (mocked HTTP).

The provider plugs into the Part 0 framework on the SAME Google OAuth client as gcal
with the googlehealth.* scopes, never include_granted_scopes, external_id =
healthUserId. The client parses dailyRollUp / list / reconcile shapes as documented
(int64s arrive as strings, dates as {year,month,day}). No network."""
from __future__ import annotations

import pytest

import config


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "connect-test-secret")
    yield


class _Resp:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = text if text is not None else ("{}" if payload is None else "x")

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_registered_in_part0_framework():
    from integrations import google_health, base  # noqa: F401
    p = base.get_provider("google_health")
    assert p is not None and p.name == "google_health" and p.label == "fitbit" and p.enabled()


def test_authorize_url_google_endpoint_health_scopes_no_granted_scope_union():
    from integrations import google_health as gh
    url = gh.GoogleHealthProvider().authorize_url(state="TOK", redirect_uri="https://app/oauth/google_health/callback")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    for s in ("client_id=cid.apps.googleusercontent.com", "access_type=offline", "prompt=consent", "state=TOK",
              "googlehealth.activity_and_fitness.readonly", "googlehealth.health_metrics_and_measurements.readonly",
              "googlehealth.sleep.readonly"):
        assert s in url, (s, url)
    assert "include_granted_scopes" not in url        # legacy fitness.* union → 403s
    assert "nutrition" not in url and "writeonly" not in url and "calendar" not in url


def test_exchange_code_stores_health_user_id(monkeypatch):
    from integrations import google_health as gh
    seen = {}

    def fake_post(url, data=None, timeout=None):
        seen["url"], seen["data"] = url, data
        return _Resp(200, {"access_token": "AT", "refresh_token": "RT", "expires_in": 3599,
                           "scope": gh.SCOPE, "token_type": "Bearer"})

    def fake_get(url, headers=None, params=None, timeout=None):
        assert url.endswith("/v4/users/me/identity") and headers["Authorization"] == "Bearer AT"
        return _Resp(200, {"name": "users/me/identity", "legacyUserId": "A1B2C3", "healthUserId": "111111256096816351"})

    monkeypatch.setattr(gh.requests, "post", fake_post)
    monkeypatch.setattr(gh.requests, "get", fake_get)
    b = gh.GoogleHealthProvider().exchange_code("AUTHCODE", redirect_uri="https://app/oauth/google_health/callback")
    assert seen["url"] == "https://oauth2.googleapis.com/token" and seen["data"]["grant_type"] == "authorization_code"
    assert b.access_token == "AT" and b.refresh_token == "RT" and b.external_id == "111111256096816351"


def test_refresh_keeps_existing_refresh_token(monkeypatch):
    from integrations import google_health as gh
    monkeypatch.setattr(gh.requests, "post",
                        lambda url, data=None, timeout=None: _Resp(200, {"access_token": "AT2", "expires_in": 3599}))
    b = gh.GoogleHealthProvider().refresh("RT")
    assert b.access_token == "AT2" and b.refresh_token is None     # base keeps the stored one


def test_daily_rollup_end_is_exclusive_and_capped(monkeypatch):
    from datetime import date
    from integrations import google_health as gh
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"], seen["body"] = url, json
        return _Resp(200, {"rollupDataPoints": [
            {"civilStartTime": {"date": {"year": 2026, "month": 9, "day": 23}, "time": {}}, "steps": {"countSum": "8430"}},
            {"civilStartTime": {"date": {"year": 2026, "month": 9, "day": 24}, "time": {}}, "steps": {"countSum": "4210"}},
        ]})

    monkeypatch.setattr(gh.requests, "post", fake_post)
    out = gh.get_steps_by_day("AT", date(2026, 9, 23), date(2026, 9, 24))
    assert out == {"2026-09-23": 8430, "2026-09-24": 4210}
    assert seen["url"].endswith("/v4/users/me/dataTypes/steps/dataPoints:dailyRollUp")
    b = seen["body"]
    assert b["range"]["start"]["date"] == {"year": 2026, "month": 9, "day": 23}
    assert b["range"]["end"]["date"] == {"year": 2026, "month": 9, "day": 25}      # exclusive end = +1 day
    assert b["windowSizeDays"] == 1 and b["pageSize"] == 2                          # duration = window × page
    # total-calories is capped at 14 days: a 30-day ask shrinks to the last 14
    gh.get_calories_by_day("AT", date(2026, 8, 26), date(2026, 9, 24))
    assert seen["body"]["pageSize"] == 14 and seen["body"]["range"]["start"]["date"]["day"] == 11


def test_daily_types_sleep_and_weight_parse(monkeypatch):
    from datetime import date
    from integrations import google_health as gh
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append((url, params))
        if "daily-resting-heart-rate" in url:
            assert params["filter"].startswith('daily_resting_heart_rate.date >= "2026-09-23"')
            return _Resp(200, {"dataPoints": [
                {"dailyRestingHeartRate": {"date": {"year": 2026, "month": 9, "day": 23}, "beatsPerMinute": "55"}},
                {"dailyRestingHeartRate": {"date": {"year": 2026, "month": 9, "day": 24}, "beatsPerMinute": "59"}},
            ]})
        if "daily-heart-rate-variability" in url:
            return _Resp(200, {"dataPoints": [
                {"dailyHeartRateVariability": {"date": {"year": 2026, "month": 9, "day": 24},
                                               "averageHeartRateVariabilityMilliseconds": 34.5}}]})
        if "sleep/dataPoints:reconcile" in url:
            assert params["dataSourceFamily"].endswith("google-wearables")
            return _Resp(200, {"dataPoints": [{"name": "users/1/dataTypes/sleep/dataPoints/s1", "sleep": {
                "interval": {"startTime": "2026-09-24T06:48:00Z", "endTime": "2026-09-24T13:31:00Z",
                             "startUtcOffset": "-25200s", "endUtcOffset": "-25200s"},
                "type": "MAIN_SLEEP", "summary": {"minutesAsleep": "372", "minutesAwake": "35"}}}]})
        if "/weight/dataPoints" in url:
            assert 'weight.sample_time.physical_time >= "2026-09-23T00:00:00Z"' == params["filter"]
            return _Resp(200, {"dataPoints": [
                {"name": "users/1/dataTypes/weight/dataPoints/w1",
                 "weight": {"sampleTime": {"physicalTime": "2026-09-24T14:10:00Z"}, "weightKg": 77.65}},
                {"name": "users/1/dataTypes/weight/dataPoints/w2",
                 "weight": {"sampleTime": {"physicalTime": "2026-09-23T14:10:00Z"}, "weightGrams": 77800}},
            ]})
        raise AssertionError(url)

    monkeypatch.setattr(gh.requests, "get", fake_get)
    assert gh.get_resting_hr_by_day("AT", date(2026, 9, 23), date(2026, 9, 24)) == {"2026-09-23": 55, "2026-09-24": 59}
    assert gh.get_hrv_by_day("AT", date(2026, 9, 23), date(2026, 9, 24)) == {"2026-09-24": 34.5}
    s = gh.get_sleep_sessions("AT", date(2026, 9, 23))
    assert s[0]["type"] == "MAIN_SLEEP" and s[0]["name"].endswith("/s1")
    w = gh.get_weight_samples("AT", date(2026, 9, 23))
    assert [x["lbs"] for x in w] == [171.2, 171.5] and w[0]["name"].endswith("/w1")


def test_list_paginates(monkeypatch):
    from datetime import date
    from integrations import google_health as gh
    pages = {None: ({"dataPoints": [{"dailyRestingHeartRate": {"date": {"year": 2026, "month": 9, "day": 23}, "beatsPerMinute": "55"}}],
                     "nextPageToken": "p2"}),
             "p2": ({"dataPoints": [{"dailyRestingHeartRate": {"date": {"year": 2026, "month": 9, "day": 24}, "beatsPerMinute": "57"}}]})}
    monkeypatch.setattr(gh.requests, "get",
                        lambda url, headers=None, params=None, timeout=None: _Resp(200, pages[params.get("pageToken")]))
    assert gh.get_resting_hr_by_day("AT", date(2026, 9, 23), date(2026, 9, 24)) == {"2026-09-23": 55, "2026-09-24": 57}


def test_read_client_raises_typed_error_with_status(monkeypatch):
    from datetime import date
    from integrations import google_health as gh
    monkeypatch.setattr(gh.requests, "post",
                        lambda url, headers=None, json=None, timeout=None: _Resp(403, text="PERMISSION_DENIED"))
    with pytest.raises(gh.HealthAPIError) as ei:
        gh.get_steps_by_day("AT", date(2026, 9, 23), date(2026, 9, 24))
    assert ei.value.status == 403


def test_connect_tool_accepts_google_health_and_gates_on_flag(db, monkeypatch, sms_capture):
    from agent_tools import handle_send_connect_link, SEND_CONNECT_LINK_TOOL
    from integrations import base
    from tests.factories import make_user
    assert "google_health" in SEND_CONNECT_LINK_TOOL["input_schema"]["properties"]["provider"]["enum"]
    assert "fitbit" not in SEND_CONNECT_LINK_TOOL["input_schema"]["properties"]["provider"]["enum"]
    user = make_user(db, phone="+15105558888")
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", False)
    assert handle_send_connect_link(user.id, {"provider": "google_health"}).startswith("error")
    assert handle_send_connect_link(user.id, {"provider": "fitbit"}).startswith("error")
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    assert handle_send_connect_link(user.id, {"provider": "google_health"}).startswith("ok")
    assert any("/c/google_health?t=" in body for _p, body in sms_capture)
    assert base.pending_nonce(user.id, "google_health")
