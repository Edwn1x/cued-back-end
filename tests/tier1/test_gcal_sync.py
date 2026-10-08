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
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: "AT")
    monkeypatch.setattr(gcal, "list_calendars", lambda tok: [{"id": "primary", "summary": "Primary"}])

    soon = datetime.now(timezone.utc) + timedelta(days=2)
    # all-day events use relative dates so the fixture never rots at date rollover
    ad0 = soon.date().isoformat()
    ad1 = (soon + timedelta(days=1)).date().isoformat()
    gevents = [
        {"id": "keep1", "status": "confirmed", "summary": "ochem midterm",
         "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(hours=2))}},
        {"id": "allday", "status": "confirmed", "summary": "trip",
         "start": {"date": ad0}, "end": {"date": ad1}},
        {"id": "declined", "status": "confirmed", "summary": "meeting",
         "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(hours=1))},
         "attendees": [{"self": True, "responseStatus": "declined"}]},
        {"id": "free", "status": "confirmed", "summary": "focus", "transparency": "transparent",
         "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(hours=1))}},
        {"id": "bday", "status": "confirmed", "summary": "birthday", "eventType": "birthday",
         "start": {"date": ad0}, "end": {"date": ad1}},
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
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: "AT")
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
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: None)
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


def test_list_calendars_failure_falls_back_to_primary(db, monkeypatch):
    """Live 2026-09-24: a grant with only events.readonly 403s on calendarList.list.
    The primary calendar is still readable, so sync it instead of syncing nothing."""
    from tests.factories import make_user
    from integrations import gcal_sync, base, gcal
    from events import upcoming_events

    user = make_user(db)
    _connected_gcal(db, user.id)
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: "AT")

    def _forbidden(tok):
        raise RuntimeError("403 Client Error: Forbidden for url: .../users/me/calendarList")
    monkeypatch.setattr(gcal, "list_calendars", _forbidden)
    asked = []
    soon = datetime.now(timezone.utc) + timedelta(days=2)
    ev = [{"id": "e1", "status": "confirmed", "summary": "ochem midterm",
           "start": {"dateTime": _iso(soon)}, "end": {"dateTime": _iso(soon + timedelta(hours=2))}}]

    def _list_events(tok, cid, **kw):
        asked.append(cid)
        return ev, "S1"
    monkeypatch.setattr(gcal, "list_events", _list_events)

    res = gcal_sync.sync_user(user.id)
    assert res == {"upserted": 1, "deleted": 0, "accounts": 1}   # accounts: every connected Google login synced
    assert asked == ["primary"]
    assert "ochem midterm" in {e.title for e in upcoming_events(user.id, days=60)}


def test_scope_can_list_calendars():
    from integrations.gcal import SCOPE
    assert "calendar.events.readonly" in SCOPE
    assert "calendar.calendarlist.readonly" in SCOPE
    assert "auth/calendar.readonly" not in SCOPE and "auth/calendar " not in SCOPE + " "



# ─── perf + the holidays calendar (live 2026-10-06, user 48) ─────────────────

def test_resync_leaves_unchanged_rows_alone_and_counts_them(db, monkeypatch):
    """135 events re-saved one by one every 30 min took 3 minutes. Now: one session per
    calendar, unchanged rows untouched; only a real change counts as upserted."""
    from tests.factories import make_user
    from integrations import gcal_sync, base, gcal
    from models import get_session, Event
    user = make_user(db)
    _connected_gcal(db, user.id)
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: "AT")
    monkeypatch.setattr(gcal, "list_calendars", lambda tok: [{"id": "primary"}])
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    evs = [{"id": f"e{i}", "status": "confirmed", "summary": f"lecture {i}",
            "start": {"dateTime": _iso(soon + timedelta(hours=i))}, "end": {"dateTime": _iso(soon + timedelta(hours=i + 1))}}
           for i in range(5)]
    monkeypatch.setattr(gcal, "list_events", lambda tok, cid, **kw: (evs, None))
    s0 = get_session()
    try:
        from integrations.base import get_integration
        rid = get_integration(s0, user.id, "gcal").id
    finally:
        s0.close()
    first = gcal_sync._sync_row(user.id, rid)
    assert first["upserted"] == 5 and first["unchanged"] == 0
    second = gcal_sync._sync_row(user.id, rid)
    assert second["upserted"] == 0 and second["unchanged"] == 5
    evs[2]["summary"] = "lecture 2 (moved)"
    third = gcal_sync._sync_row(user.id, rid)
    assert third["upserted"] == 1 and third["unchanged"] == 4
    s = get_session()
    try:
        assert s.query(Event).filter(Event.user_id == user.id, Event.deleted_at.is_(None)).count() == 5
        assert s.query(Event).filter(Event.external_id == "primary:e2").one().title == "lecture 2 (moved)"
    finally:
        s.close()


def test_batch_upsert_revives_a_cancelled_row_and_handles_empty(db):
    from tests.factories import make_user
    from events import upsert_external_events, delete_external_event
    from models import get_session, Event
    user = make_user(db)
    at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1)
    assert upsert_external_events(user.id, source="gcal", rows=[]) == {"inserted": 0, "updated": 0, "unchanged": 0}
    c = upsert_external_events(user.id, source="gcal", rows=[{"external_id": "p:x", "title": "x", "occurred_at": at, "ends_at": None, "all_day": False}])
    assert c["inserted"] == 1
    assert delete_external_event(user.id, source="gcal", external_id="p:x") is True
    c2 = upsert_external_events(user.id, source="gcal", rows=[{"external_id": "p:x", "title": "x", "occurred_at": at, "ends_at": None, "all_day": False}])
    assert c2 == {"inserted": 0, "updated": 1, "unchanged": 0}
    s = get_session()
    try:
        assert s.query(Event).filter(Event.external_id == "p:x").one().deleted_at is None
    finally:
        s.close()


def test_calendar_id_with_a_hash_is_percent_encoded(monkeypatch):
    """en.usa#holiday@group.v.calendar.google.com → the '#' was a URL fragment, so the
    request went to /calendars/en.usa and 403'd on every tick."""
    from integrations import gcal
    seen = {}

    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"items": [], "nextSyncToken": None}
    def _get(url, headers=None, params=None, timeout=None):
        seen.setdefault("url", url)
        return _R()
    monkeypatch.setattr(gcal.requests, "get", _get)
    gcal.list_events("AT", "en.usa#holiday@group.v.calendar.google.com", time_min="2026-10-01T00:00:00Z", time_max="2026-11-01T00:00:00Z")
    assert seen["url"] == "https://www.googleapis.com/calendar/v3/calendars/en.usa%23holiday%40group.v.calendar.google.com/events"
