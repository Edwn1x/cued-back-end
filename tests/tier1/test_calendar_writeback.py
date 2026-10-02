"""Google Calendar WRITE-BACK (create-only), behind CALENDAR_WRITE_ENABLED (default OFF).

Covers, all with the Google API mocked (no network):
  - the authorize_url gains the read/WRITE scope only when the flag is on, and keeps the
    readonly scopes either way (existing readonly grants must still sync);
  - gcal.create_event POSTs events.insert on the primary calendar with the right body;
  - a 403 / insufficient-scope raises WriteAccessDenied (readonly grant), and NotConnected
    when there's no token — neither is a fake success;
  - the tool confirms before writing (no write until confirmed=true);
  - a confirmed create mirrors into the local Event store (source='gcal', keyed like sync);
  - the tool is absent from the coach loop when the flag is off, present when on.
"""
from __future__ import annotations

from datetime import datetime, timezone

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


# ─── scope: write is ADDED behind the flag, read never removed ────────────────

def test_authorize_url_adds_write_scope_only_when_flag_on(monkeypatch):
    from integrations.base import get_provider
    # flag OFF (default): exactly the readonly scopes, no write scope
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", False)
    off = get_provider("gcal").authorize_url(state="TOK", redirect_uri="https://app/oauth/gcal/callback")
    assert "calendar.events.readonly" in off
    # the write scope encodes as ".../auth/calendar.events" WITHOUT the .readonly suffix
    assert "calendar.events&" not in off and not off.rstrip().endswith("calendar.events")
    assert "auth%2Fcalendar.events&" not in off

    # flag ON: write scope appended, readonly still present (read keeps working)
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", True)
    on = get_provider("gcal").authorize_url(state="TOK", redirect_uri="https://app/oauth/gcal/callback")
    assert "calendar.events.readonly" not in on       # superset of the write scope — not requested twice (verification review)
    assert "calendar.calendarlist.readonly" in on     # listing calendars still needs its own scope
    assert "calendar.calendarlist.readonly" in on
    from integrations.gcal import WRITE_SCOPE
    from urllib.parse import quote
    assert quote(WRITE_SCOPE, safe="") in on          # write scope requested


# ─── gcal.create_event: events.insert body + degrade signals ──────────────────

def test_create_event_posts_insert_with_right_body(monkeypatch):
    from integrations import gcal, base
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url
        seen["headers"] = headers
        seen["body"] = json
        return _Resp(200, {"id": "evt123", "htmlLink": "https://cal/evt123"})

    monkeypatch.setattr(gcal.requests, "post", fake_post)

    start = datetime(2026, 10, 1, 23, 0, tzinfo=timezone.utc)
    end = datetime(2026, 10, 2, 0, 0, tzinfo=timezone.utc)
    ev = gcal.create_event(7, "gym — push", start, end, description="hypertrophy")

    assert ev["id"] == "evt123"
    assert seen["url"].endswith("/calendar/v3/calendars/primary/events")
    assert seen["headers"]["Authorization"] == "Bearer AT"
    assert seen["body"]["summary"] == "gym — push"
    assert seen["body"]["start"]["dateTime"] == "2026-10-01T23:00:00Z"
    assert seen["body"]["end"]["dateTime"] == "2026-10-02T00:00:00Z"
    assert seen["body"]["description"] == "hypertrophy"


def test_create_event_403_raises_write_access_denied(monkeypatch):
    from integrations import gcal, base
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
    monkeypatch.setattr(gcal.requests, "post", lambda *a, **k: _Resp(403, {"error": "insufficient"}))
    with pytest.raises(gcal.WriteAccessDenied):
        gcal.create_event(7, "x", datetime.now(timezone.utc), datetime.now(timezone.utc))


def test_create_event_not_connected_when_no_token(monkeypatch):
    from integrations import gcal, base
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: None)
    # requests.post must never even be reached
    monkeypatch.setattr(gcal.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("posted")))
    with pytest.raises(gcal.NotConnected):
        gcal.create_event(7, "x", datetime.now(timezone.utc), datetime.now(timezone.utc))


# ─── the tool: confirm-before-write, mirror, honest degrade ───────────────────

def test_tool_confirms_before_writing(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_create_calendar_event
    from integrations import gcal, base
    from models import get_session, Event, active

    user = make_user(db, user_timezone="America/Los_Angeles")
    # a write attempt would blow up — proving no write happened before confirm
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
    monkeypatch.setattr(gcal.requests, "post",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrote before confirm")))

    out = handle_create_calendar_event(user.id, {
        "summary": "study block", "starts_at": "14:00", "ends_at": "16:00", "date": "today",
    })
    assert "not created yet" in out.lower() and "confirmed=true" in out
    s = get_session()
    try:
        assert active(s, Event, user_id=user.id).count() == 0   # nothing written
    finally:
        s.close()


def test_confirmed_create_writes_and_mirrors_to_event_store(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_create_calendar_event
    from integrations import gcal, base
    from models import get_session, Event, active

    user = make_user(db, user_timezone="America/Los_Angeles")
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
    monkeypatch.setattr(gcal.requests, "post",
                        lambda *a, **k: _Resp(200, {"id": "evtABC"}))

    out = handle_create_calendar_event(user.id, {
        "summary": "gym — push", "starts_at": "16:00", "ends_at": "17:00",
        "date": "today", "confirmed": True,
    })
    assert out.startswith("ok") and "gym — push" in out

    s = get_session()
    try:
        rows = active(s, Event, user_id=user.id).all()
        assert len(rows) == 1
        e = rows[0]
        assert e.source == "gcal"                       # keyed like the sync layer
        assert e.external_id == "primary:evtABC"        # so a re-sync updates, not dupes
        assert e.title == "gym — push"
        assert e.occurred_at is not None and e.ends_at is not None
        assert e.ends_at > e.occurred_at
    finally:
        s.close()


def test_tool_default_duration_when_no_end(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_create_calendar_event
    from integrations import gcal, base
    from models import get_session, Event, active

    user = make_user(db, user_timezone="America/Los_Angeles")
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
    monkeypatch.setattr(gcal.requests, "post", lambda *a, **k: _Resp(200, {"id": "e1"}))

    out = handle_create_calendar_event(user.id, {
        "summary": "lift", "starts_at": "09:00", "duration_minutes": 45,
        "date": "today", "confirmed": True,
    })
    assert out.startswith("ok")
    s = get_session()
    try:
        e = active(s, Event, user_id=user.id).one()
        assert (e.ends_at - e.occurred_at).total_seconds() == 45 * 60
    finally:
        s.close()


def test_tool_readonly_grant_returns_reconnect_message(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_create_calendar_event
    from integrations import gcal, base
    from models import get_session, Event, active

    user = make_user(db, user_timezone="America/Los_Angeles")
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
    monkeypatch.setattr(gcal.requests, "post", lambda *a, **k: _Resp(403, {}))

    out = handle_create_calendar_event(user.id, {
        "summary": "study", "starts_at": "10:00", "date": "today", "confirmed": True,
    })
    low = out.lower()
    assert "read" in low and "can't add" in low and "reconnect" in low
    assert not low.startswith("ok")            # never a fake success
    s = get_session()
    try:
        assert active(s, Event, user_id=user.id).count() == 0   # nothing mirrored
    finally:
        s.close()


def test_tool_not_connected_is_honest(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_create_calendar_event
    from integrations import base

    user = make_user(db, user_timezone="America/Los_Angeles")
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: None)
    out = handle_create_calendar_event(user.id, {
        "summary": "study", "starts_at": "10:00", "date": "today", "confirmed": True,
    })
    low = out.lower()
    assert "no google calendar connected" in low and not low.startswith("ok")


# ─── loop assembly: gated by the flag ─────────────────────────────────────────

def _tool_names_offered(user, monkeypatch, anthropic_stub):
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.update(tools=kw.get("tools")) or "hey")
    from agent_loop import run_agent_loop
    run_agent_loop(user, "yo", "freeform")
    tools = seen.get("tools") or []
    return {t.get("name") for t in tools if isinstance(t, dict)}


def test_tool_absent_when_flag_off(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", False)
    user = make_user(db, onboarding_step=3)
    names = _tool_names_offered(user, monkeypatch, anthropic_stub)
    assert "create_calendar_event" not in names


def test_tool_present_when_flag_on(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", True)
    user = make_user(db, onboarding_step=3)
    names = _tool_names_offered(user, monkeypatch, anthropic_stub)
    assert "create_calendar_event" in names
