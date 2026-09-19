"""Part 1.2 — Google Calendar sync into the event store (mocked API).

sync_user pulls calendars → upserts timed + all-day events, skips declined /
transparent / birthday / >24h, deletes cancelled, persists the per-calendar
syncToken, and the results surface through the normal UPCOMING reader."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

import config


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    yield


def _connected_gcal(db, user_id):
    from models import get_session, Integration
    s = get_session()
    try:
        s.add(Integration(user_id=user_id, provider="gcal", status="connected", meta={}))
        s.commit()
    finally:
        s.close()


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def test_sync_upserts_skips_and_deletes(db, monkeypatch):
    from tests.factories import make_user
    from integrations import gcal_sync, base, gcal
    from events import upcoming_events
    from models import get_session, Event

    user = make_user(db)
    _connected_gcal(db, user.id)
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
    monkeypatch.setattr(gcal, "list_calendars", lambda tok: [{"id": "primary", "summary": "Primary"}])

    soon = datetime.now(timezone.utc) + timedelta(days=2)
    gevents = [
        {"id": "keep1", "status": "confirmed", "summary": "ochem midterm",
         "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(hours=2))}},
        {"id": "allday", "status": "confirmed", "summary": "trip",
         "start": {"date": "2026-09-25"}, "end": {"date": "2026-09-26"}},
        {"id": "declined", "status": "confirmed", "summary": "meeting",
         "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(hours=1))},
         "attendees": [{"self": True, "responseStatus": "declined"}]},
        {"id": "free", "status": "confirmed", "summary": "focus", "transparency": "transparent",
         "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(hours=1))}},
        {"id": "bday", "status": "confirmed", "summary": "birthday", "eventType": "birthday",
         "start": {"date": "2026-09-25"}, "end": {"date": "2026-09-26"}},
        {"id": "toolong", "status": "confirmed", "summary": "semester",
         "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(days=40))}},
    ]
    monkeypatch.setattr(gcal, "list_events",
                        lambda tok, cid, **kw: (gevents, "SYNC-NEXT"))

    res = gcal_sync.sync_user(user.id)
    assert res["upserted"] == 2   # keep1 + allday only

    titles = {e.title for e in upcoming_events(user.id, days=60)}
    assert "ochem midterm" in titles and "trip" in titles
    assert "meeting" not in titles and "focus" not in titles and "birthday" not in titles
    assert "semester" not in titles

    # syncToken persisted for incremental next time
    s = get_session()
    try:
        from integrations.base import get_integration
        integ = get_integration(s, user.id, "gcal")
        assert (integ.meta or {}).get("gcal_sync", {}).get("primary") == "SYNC-NEXT"
    finally:
        s.close()

    # a re-pull that marks keep1 cancelled removes it (no duplicate, honors delete)
    cancel = [{"id": "keep1", "status": "cancelled",
               "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon)}}]
    monkeypatch.setattr(gcal, "list_events", lambda tok, cid, **kw: (cancel, "SYNC-3"))
    res2 = gcal_sync.sync_user(user.id)
    assert res2["deleted"] == 1
    titles2 = {e.title for e in upcoming_events(user.id, days=60)}
    assert "ochem midterm" not in titles2


def test_sync_is_idempotent_no_duplicates(db, monkeypatch):
    from tests.factories import make_user
    from integrations import gcal_sync, base, gcal
    from models import get_session, Event

    user = make_user(db)
    _connected_gcal(db, user.id)
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: "AT")
    monkeypatch.setattr(gcal, "list_calendars", lambda tok: [{"id": "primary"}])
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    ev = [{"id": "e1", "status": "confirmed", "summary": "lab",
           "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(hours=2))}}]
    monkeypatch.setattr(gcal, "list_events", lambda tok, cid, **kw: (ev, "S"))

    gcal_sync.sync_user(user.id)
    gcal_sync.sync_user(user.id)   # same event again
    s = get_session()
    try:
        n = s.query(Event).filter(Event.user_id == user.id, Event.source == "gcal",
                                  Event.deleted_at.is_(None)).count()
        assert n == 1, "re-pull duplicated the event"
    finally:
        s.close()


def test_sync_skips_when_not_connected(db, monkeypatch):
    from tests.factories import make_user
    from integrations import gcal_sync, base
    user = make_user(db)
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov: None)
    assert gcal_sync.sync_user(user.id) == {"skipped": "not connected"}


def test_calendar_block_soon_gates_heartbeat(db):
    """A timed calendar block starting within 90 min suppresses a proactive nudge;
    an all-day event does not."""
    from tests.factories import make_user
    from models import get_session, Event
    from events import calendar_block_soon

    user = make_user(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    s = get_session()
    try:
        # exam in 30 min (timed) → blocks
        s.add(Event(user_id=user.id, source="gcal", external_id="c:exam", event_type="scheduled",
                    title="midterm", raw_text="midterm", all_day=False,
                    occurred_at=now + timedelta(minutes=30), ends_at=now + timedelta(minutes=120)))
        s.commit()
    finally:
        s.close()
    assert calendar_block_soon(user.id) is True

    # far-future block does NOT gate
    user2 = make_user(db, phone="+15105550000")
    s = get_session()
    try:
        s.add(Event(user_id=user2.id, source="gcal", external_id="c:later", event_type="scheduled",
                    title="lab", raw_text="lab", all_day=False,
                    occurred_at=now + timedelta(hours=6), ends_at=now + timedelta(hours=8)))
        # all-day today does NOT gate (would suppress the whole day)
        s.add(Event(user_id=user2.id, source="gcal", external_id="c:trip", event_type="scheduled",
                    title="trip", raw_text="trip", all_day=True,
                    occurred_at=now - timedelta(hours=2), ends_at=now + timedelta(hours=22)))
        s.commit()
    finally:
        s.close()
    assert calendar_block_soon(user2.id) is False


def test_heartbeat_guardrail_returns_calendar_block(db):
    from tests.factories import make_user
    from models import get_session, Event
    from heartbeat import guardrail_reason
    user = make_user(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    s = get_session()
    try:
        s.add(Event(user_id=user.id, source="gcal", external_id="c:cls", event_type="scheduled",
                    title="lecture", raw_text="lecture", all_day=False,
                    occurred_at=now + timedelta(minutes=10), ends_at=now + timedelta(minutes=80)))
        s.commit()
        u = s.get(__import__("models").User, user.id)
        assert guardrail_reason(u, s) == "calendar_block"
    finally:
        s.close()
