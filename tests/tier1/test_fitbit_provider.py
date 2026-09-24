"""Part 2a — Fitbit OAuth provider + Web API read client (mocked HTTP).

The provider plugs into the Part 0 framework (authorize → exchange → refresh) with
PKCE derived from the connect state, HTTP Basic client auth on the token endpoint,
and ROTATING refresh tokens (the returned refresh token must be persisted). No
network — requests is monkeypatched."""
from __future__ import annotations

import base64
import hashlib

import pytest

import config


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "FITBIT_ENABLED", True)
    monkeypatch.setattr(config, "FITBIT_CLIENT_ID", "23ABCD")
    monkeypatch.setattr(config, "FITBIT_CLIENT_SECRET", "fitbit-secret")
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "connect-test-secret")
    monkeypatch.setattr(config, "FITBIT_SUBSCRIBER_VERIFY_CODE", "verify-me")
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
    from integrations import fitbit, base  # noqa: F401
    p = base.get_provider("fitbit")
    assert p is not None and p.name == "fitbit" and p.enabled()


def test_authorize_url_carries_pkce_s256_and_scopes():
    from integrations import fitbit
    url = fitbit.FitbitProvider().authorize_url(state="TOK.1", redirect_uri="https://app/oauth/fitbit/callback")
    assert url.startswith("https://www.fitbit.com/oauth2/authorize?")
    for s in ("client_id=23ABCD", "response_type=code", "code_challenge_method=S256", "state=TOK.1",
              "scope=activity+heartrate+sleep+weight+profile"):
        assert s in url, (s, url)
    assert "nutrition" not in url and "location" not in url
    # the challenge is SHA256(verifier) where the verifier derives from the SAME state
    ver = fitbit.code_verifier("TOK.1")
    assert 43 <= len(ver) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).decode().rstrip("=")
    assert f"code_challenge={expected}" in url
    # a different state → a different verifier (per-handshake material)
    assert fitbit.code_verifier("TOK.2") != ver


def test_exchange_uses_basic_auth_and_verifier_from_state(monkeypatch):
    from integrations import fitbit
    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen["url"], seen["data"], seen["headers"] = url, data, headers
        return _Resp(200, {"access_token": "AT", "refresh_token": "RT1", "expires_in": 28800,
                           "scope": "activity heartrate sleep weight profile", "user_id": "GGNJL9",
                           "token_type": "Bearer"})

    monkeypatch.setattr(fitbit.requests, "post", fake_post)
    b = fitbit.FitbitProvider().exchange_code("AUTHCODE", redirect_uri="https://app/oauth/fitbit/callback",
                                              state="TOK.1")
    assert seen["url"] == "https://api.fitbit.com/oauth2/token"
    assert seen["data"]["grant_type"] == "authorization_code"
    assert seen["data"]["code_verifier"] == fitbit.code_verifier("TOK.1")
    assert seen["headers"]["Authorization"] == "Basic " + base64.b64encode(b"23ABCD:fitbit-secret").decode()
    assert b.access_token == "AT" and b.refresh_token == "RT1" and b.external_id == "GGNJL9"
    assert b.expires_at is not None and "sleep" in b.scopes


def test_exchange_without_state_refuses():
    from integrations import fitbit
    with pytest.raises(ValueError):
        fitbit.FitbitProvider().exchange_code("AUTHCODE", redirect_uri="https://app/x")


def test_refresh_returns_the_rotated_refresh_token(monkeypatch):
    from integrations import fitbit

    def fake_post(url, data=None, headers=None, timeout=None):
        assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "RT1"
        return _Resp(200, {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 28800})

    monkeypatch.setattr(fitbit.requests, "post", fake_post)
    b = fitbit.FitbitProvider().refresh("RT1")
    assert b.access_token == "AT2" and b.refresh_token == "RT2"   # base persists this — must not be None


def test_framework_persists_rotated_refresh_token(db, monkeypatch):
    """End-to-end through base.get_valid_access_token: a near-expiry fitbit row refreshes
    and the NEW refresh token is what's stored (the old one is dead at Fitbit)."""
    from datetime import datetime, timezone, timedelta
    from cryptography.fernet import Fernet
    from integrations import fitbit, base, crypto
    from models import Integration
    from tests.factories import make_user

    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    user = make_user(db)
    base.complete_connection(user.id, "fitbit", base.TokenBundle(
        access_token="OLD", refresh_token="RT1",
        expires_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).replace(tzinfo=None)))

    def fake_post(url, data=None, headers=None, timeout=None):
        return _Resp(200, {"access_token": "AT2", "refresh_token": "RT2", "expires_in": 28800})

    monkeypatch.setattr(fitbit.requests, "post", fake_post)
    assert base.get_valid_access_token(user.id, "fitbit") == "AT2"
    db.expire_all()
    row = db.query(Integration).filter_by(user_id=user.id, provider="fitbit").one()
    assert crypto.decrypt(row.refresh_token) == "RT2"


def test_read_client_parses_series_sleep_hrv_weight(monkeypatch):
    from integrations import fitbit
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append(url)
        assert headers["Authorization"] == "Bearer AT" and headers["Accept-Language"] == "en_US"
        if "/activities/steps/date/" in url:
            return _Resp(200, {"activities-steps": [{"dateTime": "2026-09-23", "value": "8123"},
                                                    {"dateTime": "2026-09-24", "value": "4210"}]})
        if "/activities/heart/date/" in url:
            return _Resp(200, {"activities-heart": [{"dateTime": "2026-09-23", "value": {"restingHeartRate": 55}},
                                                    {"dateTime": "2026-09-24", "value": {}}]})
        if "/hrv/date/" in url:
            return _Resp(200, {"hrv": [{"dateTime": "2026-09-24", "value": {"dailyRmssd": 34.5, "deepRmssd": 40}}]})
        if "/sleep/date/" in url:
            assert "/1.2/" in url
            return _Resp(200, {"sleep": [{"dateOfSleep": "2026-09-24", "isMainSleep": True, "minutesAsleep": 372}]})
        if "/body/log/weight/date/" in url:
            return _Resp(200, {"weight": [{"logId": 1, "weight": 171.2, "date": "2026-09-24", "time": "07:10:00"}]})
        if "/activities/date/" in url:
            return _Resp(200, {"summary": {"caloriesOut": 2410, "veryActiveMinutes": 12, "fairlyActiveMinutes": 20}})
        raise AssertionError(url)

    monkeypatch.setattr(fitbit.requests, "get", fake_get)
    assert fitbit.get_steps_series("AT", "2026-09-23", "2026-09-24") == {"2026-09-23": 8123, "2026-09-24": 4210}
    assert fitbit.get_resting_hr_series("AT", "2026-09-23", "2026-09-24") == {"2026-09-23": 55}
    assert fitbit.get_hrv_series("AT", "2026-09-23", "2026-09-24") == {"2026-09-24": 34.5}
    assert fitbit.get_sleep_range("AT", "2026-09-23", "2026-09-24")[0]["minutesAsleep"] == 372
    assert fitbit.get_weight_logs("AT", "2026-09-24")[0]["weight"] == 171.2
    assert fitbit.get_activity_summary("AT", "2026-09-24")["caloriesOut"] == 2410


def test_read_client_raises_typed_error_with_status(monkeypatch):
    from integrations import fitbit
    monkeypatch.setattr(fitbit.requests, "get",
                        lambda url, headers=None, params=None, timeout=None: _Resp(429, text="rate limited"))
    with pytest.raises(fitbit.FitbitAPIError) as ei:
        fitbit.get_steps_series("AT", "2026-09-23", "2026-09-24")
    assert ei.value.status == 429


def test_subscribe_all_one_per_collection_and_tolerates_existing(monkeypatch):
    from integrations import fitbit
    posted = []

    def fake_post(url, headers=None, timeout=None, data=None):
        posted.append(url)
        return _Resp(200 if "sleep" in url else 201)

    monkeypatch.setattr(fitbit.requests, "post", fake_post)
    out = fitbit.subscribe_all("AT", 31)
    assert set(out) == {"activities", "sleep", "body"}
    assert any(u.endswith("/1/user/-/activities/apiSubscriptions/31-activities.json") for u in posted)


def test_subscribe_all_skipped_without_verify_code(monkeypatch):
    from integrations import fitbit
    monkeypatch.setattr(config, "FITBIT_SUBSCRIBER_VERIFY_CODE", "")
    monkeypatch.setattr(fitbit.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no post")))
    assert fitbit.subscribe_all("AT", 31) == {}


def test_connect_tool_accepts_fitbit_and_gates_on_flag(db, monkeypatch, sms_capture):
    from agent_tools import handle_send_connect_link, SEND_CONNECT_LINK_TOOL
    from integrations import base
    from tests.factories import make_user
    assert "fitbit" in SEND_CONNECT_LINK_TOOL["input_schema"]["properties"]["provider"]["enum"]
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "s")
    user = make_user(db, phone="+15105558888")
    monkeypatch.setattr(config, "FITBIT_ENABLED", False)
    assert handle_send_connect_link(user.id, {"provider": "fitbit"}).startswith("error")
    monkeypatch.setattr(config, "FITBIT_ENABLED", True)
    assert handle_send_connect_link(user.id, {"provider": "fitbit"}).startswith("ok")
    assert any("/c/fitbit?t=" in body for _p, body in sms_capture)
    assert base.pending_nonce(user.id, "fitbit")
