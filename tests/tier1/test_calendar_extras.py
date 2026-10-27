"""Google Calendar — which CALENDAR, what COLOUR, which dates to SKIP (founder 2026-10-27, from
Alexey's thread: "Yes add every friday, Also make sure it's added to the Class schedule
calendar and is yellow" → coach: "i can't choose which calendar it goes on or set the color").
Google mocked.

Covers:
  - colour names → Google colorId; unknown colour is an honest ask, not a write;
  - a calendar picked by name (exact / prefix / contains / all-words) from the user's writable
    calendars; unknown name lists them; ambiguous name asks;
  - create posts to THAT calendar's events endpoint with colorId, and mirrors keyed by that
    calendar id (what the sync uses);
  - skip_dates → EXDATE lines matching the DTSTART form (TZID local / VALUE=DATE);
  - move: new_calendar → events.move (event keeps its id; mirror re-keyed), new_color → patch;
    a series: master moved, re-mirrored from the new calendar;
  - a radar item can't take a calendar or colour.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

import config

TZ = "America/Los_Angeles"
CALS = [{"id": "nau@gmail.com", "summary": "Nau", "accessRole": "owner", "primary": True},
        {"id": "c_class@group.calendar.google.com", "summary": "Class schedule", "accessRole": "owner"},
        {"id": "c_work@group.calendar.google.com", "summary": "Work", "accessRole": "writer"},
        {"id": "c_shared@group.calendar.google.com", "summary": "Frat events", "accessRole": "reader"},
        {"id": "en.usa#holiday@group.v.calendar.google.com", "summary": "Holidays in United States", "accessRole": "reader"}]


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", True)
    from integrations import base, gcal
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: "AT")
    monkeypatch.setattr(gcal, "list_calendars", lambda token: [dict(c) for c in CALS])
    yield


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _user(db):
    from tests.factories import make_user
    from models import get_session, Integration
    u = make_user(db, user_timezone=TZ)
    s = get_session()
    try:
        s.add(Integration(user_id=u.id, provider="gcal", status="connected", meta={}, external_id="nau@gmail.com"))
        s.commit()
    finally:
        s.close()
    return u


def _google(monkeypatch, *, master=None, instances=None):
    from integrations import gcal
    calls = []

    def _get(url, headers=None, params=None, timeout=None):
        calls.append(("GET", url, params))
        return _Resp(200, {"items": instances or []} if url.endswith("/instances") else (master or {}))

    def _post(url, headers=None, json=None, params=None, timeout=None):
        calls.append(("POST", url, json if json is not None else params))
        return _Resp(200, {"id": "new1", **(json or {})})

    def _patch(url, headers=None, json=None, timeout=None):
        calls.append(("PATCH", url, json))
        return _Resp(200, {**(master or {}), **(json or {})})

    monkeypatch.setattr(gcal.requests, "get", _get)
    monkeypatch.setattr(gcal.requests, "post", _post)
    monkeypatch.setattr(gcal.requests, "patch", _patch)
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _Resp(204))
    return calls


def _local(days_ahead, hour):
    loc = (datetime.now(ZoneInfo(TZ)) + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return loc.astimezone(timezone.utc).replace(tzinfo=None)


def _event(user_id, title, days_ahead, hour, *, cal="nau@gmail.com", gid="e1", source="gcal", series=None):
    from events import upsert_external_event, record_event
    s = _local(days_ahead, hour)
    if source != "gcal":
        return record_event(user_id, "scheduled", source="model", raw_text=title, occurred_at=s, ends_at=s + timedelta(hours=1))
    return upsert_external_event(user_id, source="gcal", external_id=f"{cal}:{gid}", title=title, occurred_at=s,
                                 ends_at=s + timedelta(hours=1), series_id=series)


def _ext(event_id):
    from models import get_session, Event
    s = get_session()
    try:
        e = s.get(Event, event_id)
        return e.external_id, e.deleted_at
    finally:
        s.close()


# ─── colours + calendar names ────────────────────────────────────────────────

def test_color_names_map_to_google_ids():
    from agent_tools import _color_id
    assert _color_id("yellow") == ("5", None)
    assert _color_id("Banana") == ("5", None)
    assert _color_id("red") == ("11", None) and _color_id("tomato") == ("11", None)
    assert _color_id("light green") == ("2", None) and _color_id("green") == ("10", None)
    assert _color_id("bright yellow") == ("5", None)
    assert _color_id("7") == ("7", None)
    cid, err = _color_id("chartreuse")
    assert cid is None and err.startswith("error: don't know the colour") and "banana (yellow)" in err
    assert _color_id("") == (None, None)


def test_calendar_resolves_by_name_lists_on_miss_asks_on_ambiguity(db):
    from agent_tools import _resolve_calendar
    u = _user(db)
    assert _resolve_calendar(u.id, "Class schedule") == ("c_class@group.calendar.google.com", "Class schedule", None)
    assert _resolve_calendar(u.id, "class")[0] == "c_class@group.calendar.google.com"          # prefix
    assert _resolve_calendar(u.id, "schedule")[0] == "c_class@group.calendar.google.com"       # contains
    assert _resolve_calendar(u.id, "schedule class")[0] == "c_class@group.calendar.google.com" # all words
    assert _resolve_calendar(u.id, "")[0] == "nau@gmail.com"                                   # primary key
    assert _resolve_calendar(u.id, "main")[0] == "nau@gmail.com"
    _, _, err = _resolve_calendar(u.id, "Frat events")                                         # read-only → not offered
    assert err.startswith("no calendar of theirs called 'Frat events'") and "'Class schedule'" in err and "'Work'" in err
    assert "Holidays" not in err and "(main)" in err


# ─── create: calendar + colour + skip dates ──────────────────────────────────

def test_create_on_named_calendar_with_colour_posts_there_and_mirrors_by_that_id(db, monkeypatch):
    from agent_tools import handle_create_calendar_event
    from models import get_session, Event, active
    u = _user(db)
    calls = _google(monkeypatch)
    args = {"summary": "INDENG 142A OH", "starts_at": "13:00", "ends_at": "15:00", "date": "tomorrow",
            "location": "Etcheverry 4176A", "calendar": "Class schedule", "color": "yellow"}
    staged = handle_create_calendar_event(u.id, args, message_id="m1")
    assert staged.startswith("not created yet"), staged
    assert "to their 'Class schedule' calendar" in staged and "banana (yellow)" in staged, staged
    assert calls == []
    out = handle_create_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: added 'INDENG 142A OH'") and "'Class schedule' calendar" in out, out
    post = [c for c in calls if c[0] == "POST"][0]
    assert "/calendars/c_class%40group.calendar.google.com/events" in post[1]
    assert post[2]["colorId"] == "5" and post[2]["location"] == "Etcheverry 4176A"
    s = get_session()
    try:
        e = active(s, Event, user_id=u.id).one()
        assert e.external_id == "c_class@group.calendar.google.com:new1"
    finally:
        s.close()


def test_create_unknown_calendar_or_colour_is_an_ask_not_a_write(db, monkeypatch):
    from agent_tools import handle_create_calendar_event, _get_pending_calendar_event
    u = _user(db)
    calls = _google(monkeypatch)
    out = handle_create_calendar_event(u.id, {"summary": "x", "starts_at": "10:00", "calendar": "Gym stuff"}, message_id="m1")
    assert out.startswith("no calendar of theirs called 'Gym stuff'") and "'Class schedule'" in out
    out = handle_create_calendar_event(u.id, {"summary": "x", "starts_at": "10:00", "color": "chartreuse"}, message_id="m1")
    assert out.startswith("error: don't know the colour")
    assert calls == [] and _get_pending_calendar_event(u.id) is None


def test_create_repeating_with_skip_dates_adds_exdates_in_local_time(db, monkeypatch):
    from agent_tools import handle_create_calendar_event
    u = _user(db)
    calls = _google(monkeypatch, instances=[])
    today = datetime.now(ZoneInfo(TZ)).date()
    fri = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
    skip = fri + timedelta(days=28)
    args = {"summary": "INDENG 142A OH", "starts_at": "13:00", "ends_at": "15:00", "date": fri.isoformat(),
            "repeat": "weekly", "repeat_days": ["fri"], "repeat_until": (fri + timedelta(days=42)).isoformat(),
            "skip_dates": [skip.isoformat()], "calendar": "class", "color": "yellow"}
    staged = handle_create_calendar_event(u.id, args, message_id="m1")
    assert "every fri 1:00pm–3:00pm through" in staged and f"skipping {skip.strftime('%b %-d')}" in staged, staged
    out = handle_create_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: added 'INDENG 142A OH' every fri 1:00pm–3:00pm"), out
    body = [c for c in calls if c[0] == "POST"][0][2]
    assert body["recurrence"][0].startswith("RRULE:FREQ=WEEKLY;BYDAY=FR;UNTIL=")
    assert body["recurrence"][1] == f"EXDATE;TZID={TZ}:{skip.strftime('%Y%m%d')}T130000"
    assert body["colorId"] == "5" and body["start"]["timeZone"] == TZ
    inst = [c for c in calls if c[0] == "GET" and c[1].endswith("/instances")][0]
    assert "/calendars/c_class%40group.calendar.google.com/events/new1/instances" in inst[1]


def test_create_all_day_skip_dates_use_value_date(db, monkeypatch):
    from agent_tools import handle_create_calendar_event
    u = _user(db)
    calls = _google(monkeypatch, instances=[])
    args = {"summary": "no-phone day", "all_day": True, "date": "tomorrow", "repeat": "daily",
            "skip_dates": ["2026-12-25"], "repeat_until": "2026-12-31"}
    handle_create_calendar_event(u.id, args, message_id="m1")
    out = handle_create_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok")
    body = [c for c in calls if c[0] == "POST"][0][2]
    assert body["recurrence"] == ["RRULE:FREQ=DAILY;UNTIL=20261231", "EXDATE;VALUE=DATE:20261225"]


def test_create_skip_dates_bad_format_is_an_error(db, monkeypatch):
    from agent_tools import handle_create_calendar_event
    u = _user(db)
    _google(monkeypatch)
    out = handle_create_calendar_event(u.id, {"summary": "x", "starts_at": "10:00", "repeat": "daily", "skip_dates": ["thanksgiving"]})
    assert out.startswith("error: skip_dates must be 'YYYY-MM-DD'")


# ─── move: calendar + colour ─────────────────────────────────────────────────

def test_move_one_onto_another_calendar_and_recolour(db, monkeypatch):
    from agent_tools import handle_move_calendar_event
    u = _user(db)
    a = _event(u.id, "INDENG 142A OH", 3, 13, gid="oh1")
    calls = _google(monkeypatch)
    args = {"event_id": a, "new_calendar": "Class schedule", "new_color": "yellow"}
    staged = handle_move_calendar_event(u.id, args, message_id="m1")
    assert staged.startswith("not moved yet") and "onto their 'Class schedule' calendar" in staged and "colour → banana (yellow)" in staged, staged
    assert calls == []
    out = handle_move_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: 'INDENG 142A OH' is now") and "on their 'Class schedule' calendar, banana" in out, out
    patch = [c for c in calls if c[0] == "PATCH"][0]
    assert patch[1].endswith("/calendars/nau%40gmail.com/events/oh1") and patch[2] == {"colorId": "5"}
    mv = [c for c in calls if c[0] == "POST"][0]
    assert mv[1].endswith("/calendars/nau%40gmail.com/events/oh1/move")
    assert mv[2] == {"destination": "c_class@group.calendar.google.com"}
    ext, deleted = _ext(a)
    assert ext == "c_class@group.calendar.google.com:oh1" and deleted is None     # re-keyed, kept


def test_move_to_the_calendar_it_is_already_on_is_nothing_to_change(db, monkeypatch):
    from agent_tools import handle_move_calendar_event
    u = _user(db)
    a = _event(u.id, "gym", 1, 16, cal="c_class@group.calendar.google.com", gid="g1")
    _google(monkeypatch)
    out = handle_move_calendar_event(u.id, {"event_id": a, "new_calendar": "Class schedule"}, message_id="m1")
    assert out.startswith("nothing to change")
    b = _event(u.id, "lift", 1, 17, gid="l1")                       # on primary (keyed by address)
    out = handle_move_calendar_event(u.id, {"event_id": b, "new_calendar": "main"}, message_id="m1")
    assert out.startswith("nothing to change")


def test_move_series_onto_another_calendar_moves_master_and_remirrors_there(db, monkeypatch):
    from agent_tools import handle_move_calendar_event
    from models import get_session, Event, active
    u = _user(db)
    for k in range(3):
        s = _local(1 + 7 * k, 13)
        _event(u.id, "INDENG 142A OH", 1 + 7 * k, 13, gid=f"oh1_{s.strftime('%Y%m%dT%H%M%SZ')}", series="oh1")
    loc = _local(-6, 13).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TZ))
    master = {"id": "oh1", "summary": "INDENG 142A OH", "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=FR"],
              "start": {"dateTime": loc.isoformat(), "timeZone": TZ}, "end": {"dateTime": (loc + timedelta(hours=2)).isoformat(), "timeZone": TZ}}
    inst = [{"id": f"oh1_{_local(1 + 7 * k, 13).strftime('%Y%m%dT%H%M%SZ')}", "recurringEventId": "oh1", "summary": "INDENG 142A OH",
             "start": {"dateTime": _local(1 + 7 * k, 13).replace(tzinfo=timezone.utc).isoformat()},
             "end": {"dateTime": (_local(1 + 7 * k, 13) + timedelta(hours=2)).replace(tzinfo=timezone.utc).isoformat()}} for k in range(3)]
    calls = _google(monkeypatch, master=master, instances=inst)
    args = {"query": "indeng", "scope": "series", "new_calendar": "Class schedule", "new_color": "yellow"}
    staged = handle_move_calendar_event(u.id, args, message_id="m1")
    assert "moving EVERY occurrence of 'INDENG 142A OH'" in staged and "onto their 'Class schedule' calendar" in staged, staged
    out = handle_move_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: every 'INDENG 142A OH' is now 1:00pm–3:00pm every fri on their 'Class schedule' calendar, banana"), out
    assert [c for c in calls if c[0] == "PATCH"][0][2] == {"colorId": "5"}
    mv = [c for c in calls if c[0] == "POST"][0]
    assert mv[1].endswith("/events/oh1/move") and mv[2] == {"destination": "c_class@group.calendar.google.com"}
    inst_call = [c for c in calls if c[0] == "GET" and c[1].endswith("/instances")][0]
    assert "/calendars/c_class%40group.calendar.google.com/events/oh1/instances" in inst_call[1]
    s = get_session()
    try:
        rows = active(s, Event, user_id=u.id).all()
        assert len(rows) == 3 and all(r.external_id.startswith("c_class@group.calendar.google.com:oh1_") for r in rows)
    finally:
        s.close()


def test_radar_item_cannot_take_calendar_or_colour(db, monkeypatch):
    from agent_tools import handle_move_calendar_event
    u = _user(db)
    r = _event(u.id, "quiz", 2, 9, source="model")
    _google(monkeypatch)
    out = handle_move_calendar_event(u.id, {"event_id": r, "new_color": "yellow"}, message_id="m1")
    assert "on your radar" in out and "no calendar or colour" in out
