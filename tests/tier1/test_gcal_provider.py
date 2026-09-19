"""Part 1 — Google Calendar OAuth provider + Calendar API client (mocked HTTP).

Verifies the provider plugs into the Part 0 framework (authorize/exchange/refresh)
and the read client handles the incremental-sync 410. No network — requests is
monkeypatched."""
from __future__ import annotations

import pytest

import config


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_ID", "cid.apps.googleusercontent.com")
    monkeypatch.setattr(config, "GOOGLE_OAUTH_CLIENT_SECRET", "secret")
    yield


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_registered_in_part0_framework():
    from integrations import gcal, base
    p = base.get_provider("gcal")
    assert p is not None and p.name == "gcal" and p.enabled()


def test_authorize_url_has_offline_consent_and_readonly_scope():
    from integrations.base import get_provider
    url = get_provider("gcal").authorize_url(state="TOK", redirect_uri="https://app/oauth/gcal/callback")
    for s in ("accounts.google.com/o/oauth2/v2/auth", "access_type=offline", "prompt=consent",
              "response_type=code", "state=TOK", "calendar.events.readonly"):
        assert s in url, (s, url)
    assert "calendar.readonly" not in url.replace("calendar.events.readonly", "")  # not the broad scope


def test_exchange_code_returns_tokens_and_account_sub(monkeypatch):
    from integrations import gcal
    calls = {}

    def fake_post(url, data=None, timeout=None):
        calls["post"] = data
        return _Resp(200, {"access_token": "AT", "refresh_token": "RT",
                           "expires_in": 3599, "scope": gcal.SCOPE, "token_type": "Bearer"})

    def fake_get(url, headers=None, params=None, timeout=None):
        assert headers["Authorization"] == "Bearer AT"
        return _Resp(200, {"id": "google-sub-123", "email": "x@y.com"})

    monkeypatch.setattr(gcal.requests, "post", fake_post)
    monkeypatch.setattr(gcal.requests, "get", fake_get)

    b = gcal.GCalProvider().exchange_code("AUTHCODE", redirect_uri="https://app/oauth/gcal/callback")
    assert calls["post"]["grant_type"] == "authorization_code"
    assert calls["post"]["code"] == "AUTHCODE"
    assert b.access_token == "AT" and b.refresh_token == "RT"
    assert b.external_id == "google-sub-123"
    assert b.expires_at is not None


def test_refresh_keeps_existing_refresh_token(monkeypatch):
    from integrations import gcal

    def fake_post(url, data=None, timeout=None):
        assert data["grant_type"] == "refresh_token"
        return _Resp(200, {"access_token": "AT2", "expires_in": 3599, "scope": gcal.SCOPE})

    monkeypatch.setattr(gcal.requests, "post", fake_post)
    b = gcal.GCalProvider().refresh("RT")
    assert b.access_token == "AT2"
    assert b.refresh_token is None   # base keeps the stored one when None


def test_list_events_410_raises_sync_expired(monkeypatch):
    from integrations import gcal

    def fake_get(url, headers=None, params=None, timeout=None):
        return _Resp(410, {})

    monkeypatch.setattr(gcal.requests, "get", fake_get)
    with pytest.raises(gcal.SyncTokenExpired):
        gcal.list_events("AT", "primary", sync_token="stale")


def test_list_events_returns_items_and_next_sync_token(monkeypatch):
    from integrations import gcal

    def fake_get(url, headers=None, params=None, timeout=None):
        # incremental pull must NOT carry timeMin/timeMax alongside a syncToken
        assert "timeMin" not in params and "timeMax" not in params
        return _Resp(200, {"items": [{"id": "e1"}, {"id": "e2"}], "nextSyncToken": "SYNC2"})

    monkeypatch.setattr(gcal.requests, "get", fake_get)
    events, next_sync = gcal.list_events("AT", "primary", sync_token="SYNC1")
    assert [e["id"] for e in events] == ["e1", "e2"]
    assert next_sync == "SYNC2"


def test_full_pull_passes_time_window(monkeypatch):
    from integrations import gcal

    def fake_get(url, headers=None, params=None, timeout=None):
        assert params.get("timeMin") == "2026-09-18T00:00:00Z"
        assert "syncToken" not in params
        return _Resp(200, {"items": [], "nextSyncToken": "S"})

    monkeypatch.setattr(gcal.requests, "get", fake_get)
    gcal.list_events("AT", "primary", time_min="2026-09-18T00:00:00Z", time_max="2026-11-17T00:00:00Z")
