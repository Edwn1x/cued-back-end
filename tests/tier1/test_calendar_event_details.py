"""Google Calendar — the rest of an event: reminders, busy/free, visibility, guests (real
invites), a Meet link, room/note edits on existing events, every-N / monthly / N-times
repeats, and RSVP to invitations (founder 2026-10-27: "what other functions does the google
call support that we don't?" → the first group). Google mocked.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import pytest

import config

TZ = "America/Los_Angeles"


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", True)
    from integrations import base, gcal
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: "AT")
    monkeypatch.setattr(gcal, "list_calendars", lambda token: [{"id": "nau@gmail.com", "summary": "Nau", "accessRole": "owner", "primary": True}])
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


def _google(monkeypatch, *, get_payload=None, post_payload=None, patch_payload=None):
    from integrations import gcal
    calls = []

    def _get(url, headers=None, params=None, timeout=None):
        calls.append(("GET", url, params))
        return _Resp(200, {"items": []} if url.endswith("/instances") else (get_payload or {}))

    def _post(url, headers=None, json=None, params=None, timeout=None):
        calls.append(("POST", url, {"body": json, "params": params}))
        return _Resp(200, {"id": "new1", **(post_payload or {}), **(json or {})})

    def _patch(url, headers=None, json=None, params=None, timeout=None):
        calls.append(("PATCH", url, {"body": json, "params": params}))
        return _Resp(200, {**(get_payload or {}), **(patch_payload or {}), **(json or {})})

    def _delete(url, headers=None, params=None, timeout=None):
        calls.append(("DELETE", url, {"params": params}))
        return _Resp(204)

    monkeypatch.setattr(gcal.requests, "get", _get)
    monkeypatch.setattr(gcal.requests, "post", _post)
    monkeypatch.setattr(gcal.requests, "patch", _patch)
    monkeypatch.setattr(gcal.requests, "delete", _delete)
    return calls


def _local(days_ahead, hour):
    loc = (datetime.now(ZoneInfo(TZ)) + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return loc.astimezone(timezone.utc).replace(tzinfo=None)


def _event(user_id, title, days_ahead, hour, *, gid="e1", source="gcal"):
    from events import upsert_external_event, record_event
    s = _local(days_ahead, hour)
    if source != "gcal":
        return record_event(user_id, "scheduled", source="model", raw_text=title, occurred_at=s, ends_at=s + timedelta(hours=1))
    return upsert_external_event(user_id, source="gcal", external_id=f"nau@gmail.com:{gid}", title=title,
                                 occurred_at=s, ends_at=s + timedelta(hours=1))


def _deleted(event_id):
    from models import get_session, Event
    s = get_session()
    try:
        return s.get(Event, event_id).deleted_at is not None
    finally:
        s.close()


# ─── repeat: interval / count / monthly ──────────────────────────────────────

def test_repeat_rule_interval_count_monthly():
    from agent_tools import _repeat_rule, _describe_rrule
    tz = ZoneInfo(TZ)
    sat = date(2026, 10, 31)
    rule, human, until, first, _ = _repeat_rule("weekly", ["mon"], sat, None, tz, all_day=False, interval=2, count=6)
    assert rule == "RRULE:FREQ=WEEKLY;BYDAY=MO;INTERVAL=2;COUNT=6"
    assert human == "every 2 weeks on mon" and until == "(6 times)" and first == date(2026, 11, 2)
    rule, human, until, first, _ = _repeat_rule("monthly", None, date(2026, 11, 3), "2027-05-01", tz, all_day=False)
    assert rule.startswith("RRULE:FREQ=MONTHLY;UNTIL=2027050") and human == "every month on the 3rd" and first == date(2026, 11, 3)
    rule, human, _, _, _ = _repeat_rule("daily", None, sat, None, tz, all_day=True, interval=3)
    assert rule == "RRULE:FREQ=DAILY;INTERVAL=3" and human == "every 3 days"
    with pytest.raises(ValueError):
        _repeat_rule("weekly", None, sat, "2026-12-01", tz, all_day=False, count=4)       # not both
    with pytest.raises(ValueError):
        _repeat_rule("weekly", None, sat, None, tz, all_day=False, interval="lots")
    assert _describe_rrule(["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;COUNT=8"]) == "every 2 weeks on tue/thu (8x)"
    assert _describe_rrule(["RRULE:FREQ=MONTHLY"]) == "every month"


# ─── details parsing ─────────────────────────────────────────────────────────

def test_parse_details_create_and_move_keys():
    from agent_tools import _parse_details, _DETAIL_KEYS_CREATE, _DETAIL_KEYS_MOVE
    d, bits, err = _parse_details({"reminders": [30, 60], "busy": False, "visibility": "private",
                                   "guests": ["Sam@Berkeley.edu", "a@b.co"], "meet_link": True}, _DETAIL_KEYS_CREATE)
    assert err is None
    assert d == {"reminders": [30, 60], "busy": False, "visibility": "private", "guests": ["a@b.co", "sam@berkeley.edu"], "meet": True}
    assert "reminder 30 min + 1h before" in bits and "marked free (won't block their schedule)" in bits
    assert "private" in bits and "inviting a@b.co, sam@berkeley.edu (they get an email)" in bits and "with a google meet link" in bits
    d, bits, err = _parse_details({"new_reminders": [], "new_location": "Soda 405", "new_description": "bring laptop"}, _DETAIL_KEYS_MOVE)
    assert d == {"reminders": [], "location": "Soda 405", "description": "bring laptop"} and "no reminder" in bits and "room → Soda 405" in bits
    _, _, err = _parse_details({"guests": ["sam"]}, _DETAIL_KEYS_CREATE)
    assert err.startswith("error: guests needs email addresses") and "ask for the email" in err
    _, _, err = _parse_details({"visibility": "secret"}, _DETAIL_KEYS_CREATE)
    assert err.startswith("error: visibility must be")
    d, _, _ = _parse_details({"reminders": "30 min"}, _DETAIL_KEYS_CREATE)
    assert d == {"reminders": [30]}
    d, _, _ = _parse_details({"reminders": "none"}, _DETAIL_KEYS_CREATE)
    assert d == {"reminders": []}
    assert _parse_details({}, _DETAIL_KEYS_CREATE) == ({}, [], None)


# ─── create with details ─────────────────────────────────────────────────────

def test_create_with_reminders_guests_meet_private_posts_everything_and_returns_link(db, monkeypatch):
    from agent_tools import handle_create_calendar_event
    u = _user(db)
    calls = _google(monkeypatch, post_payload={"hangoutLink": "https://meet.google.com/abc-defg-hij"})
    args = {"summary": "study group", "starts_at": "19:00", "ends_at": "21:00", "date": "tomorrow", "location": "Moffitt 4th",
            "reminders": [30], "guests": ["sam@berkeley.edu"], "meet_link": True, "visibility": "private"}
    staged = handle_create_calendar_event(u.id, args, message_id="m1")
    assert staged.startswith("not created yet"), staged
    for bit in ("at Moffitt 4th", "reminder 30 min before", "inviting sam@berkeley.edu (they get an email)", "with a google meet link", "private"):
        assert bit in staged, (bit, staged)
    assert calls == []
    out = handle_create_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: added 'study group'"), out
    assert "meet link: https://meet.google.com/abc-defg-hij" in out and "invites emailed to sam@berkeley.edu" in out
    post = [c for c in calls if c[0] == "POST"][0][2]
    body, params = post["body"], post["params"]
    assert body["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}
    assert body["attendees"] == [{"email": "sam@berkeley.edu"}]
    assert body["visibility"] == "private" and body["location"] == "Moffitt 4th"
    assert body["conferenceData"]["createRequest"]["conferenceSolutionKey"] == {"type": "hangoutsMeet"}
    assert params == {"sendUpdates": "all", "conferenceDataVersion": 1}
    assert "transparency" not in body                     # busy not given → Google default (busy)


def test_create_marked_free_is_not_mirrored_and_says_so(db, monkeypatch):
    from agent_tools import handle_create_calendar_event
    from models import get_session, Event, active
    u = _user(db)
    calls = _google(monkeypatch)
    args = {"summary": "maybe gym", "starts_at": "16:00", "date": "tomorrow", "busy": False}
    staged = handle_create_calendar_event(u.id, args, message_id="m1")
    assert "marked free (won't block their schedule)" in staged
    out = handle_create_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok") and "marked free, so i won't plan around it" in out
    assert [c for c in calls if c[0] == "POST"][0][2]["body"]["transparency"] == "transparent"
    s = get_session()
    try:
        assert active(s, Event, user_id=u.id).count() == 0
    finally:
        s.close()


def test_create_details_change_restages(db, monkeypatch):
    """Staged with a 30-min reminder, confirmed with 10 → not the same proposal."""
    from agent_tools import handle_create_calendar_event
    u = _user(db)
    calls = _google(monkeypatch)
    handle_create_calendar_event(u.id, {"summary": "x", "starts_at": "10:00", "reminders": [30]}, message_id="m1")
    out = handle_create_calendar_event(u.id, {"summary": "x", "starts_at": "10:00", "reminders": [10], "confirmed": True}, message_id="m2")
    assert out.startswith("not created — the proposal changed") and calls == []


# ─── move with details ───────────────────────────────────────────────────────

def test_move_changes_room_note_reminders_and_adds_guests_merging_existing(db, monkeypatch):
    from agent_tools import handle_move_calendar_event
    u = _user(db)
    a = _event(u.id, "INDENG 142A OH", 3, 13, gid="oh1")
    calls = _google(monkeypatch, get_payload={"id": "oh1", "attendees": [{"email": "old@x.com", "responseStatus": "accepted"}]})
    args = {"event_id": a, "new_location": "Soda 405", "new_description": "bring the pset", "new_reminders": [15],
            "add_guests": ["sam@berkeley.edu", "old@x.com"]}
    staged = handle_move_calendar_event(u.id, args, message_id="m1")
    assert staged.startswith("not moved yet") and "room → Soda 405" in staged and "note updated" in staged, staged
    assert "reminder 15 min before" in staged and "inviting old@x.com, sam@berkeley.edu (they get an email)" in staged
    assert calls == []
    out = handle_move_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: 'INDENG 142A OH' is now") and "room → Soda 405" in out, out
    patch = [c for c in calls if c[0] == "PATCH"][0][2]
    body, params = patch["body"], patch["params"]
    assert body["location"] == "Soda 405" and body["description"] == "bring the pset"
    assert body["reminders"]["overrides"] == [{"method": "popup", "minutes": 15}]
    assert body["attendees"] == [{"email": "old@x.com", "responseStatus": "accepted"}, {"email": "sam@berkeley.edu"}]   # merged, not replaced
    assert "start" not in body and "summary" not in body                              # time/title untouched
    assert params["sendUpdates"] == "all"


def test_move_series_adds_meet_link_and_free_on_master(db, monkeypatch):
    from agent_tools import handle_move_calendar_event
    from events import upsert_external_events
    u = _user(db)
    rows = []
    for k in range(3):
        s = _local(1 + 7 * k, 17)
        rows.append({"external_id": f"nau@gmail.com:dis1_{s.strftime('%Y%m%dT%H%M%SZ')}", "title": "ELENG 105 DIS",
                     "occurred_at": s, "ends_at": s + timedelta(hours=1), "all_day": False, "series_id": "dis1"})
    upsert_external_events(u.id, source="gcal", rows=rows)
    loc = _local(-20, 17).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TZ))
    master = {"id": "dis1", "summary": "ELENG 105 DIS", "recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=TU"],
              "start": {"dateTime": loc.isoformat(), "timeZone": TZ}, "end": {"dateTime": (loc + timedelta(hours=1)).isoformat(), "timeZone": TZ}}
    calls = _google(monkeypatch, get_payload=master, patch_payload={"hangoutLink": "https://meet.google.com/xyz"})
    args = {"query": "eleng", "scope": "series", "add_meet_link": True, "new_visibility": "private"}
    staged = handle_move_calendar_event(u.id, args, message_id="m1")
    assert "with a google meet link" in staged and "private" in staged and "EVERY occurrence" in staged, staged
    out = handle_move_calendar_event(u.id, {**args, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: every 'ELENG 105 DIS'") and "meet link: https://meet.google.com/xyz" in out, out
    patch = [c for c in calls if c[0] == "PATCH"][0]
    assert patch[1].endswith("/events/dis1")
    assert patch[2]["body"]["visibility"] == "private" and "conferenceData" in patch[2]["body"]
    assert patch[2]["params"]["conferenceDataVersion"] == 1


def test_move_radar_item_rejects_details(db, monkeypatch):
    from agent_tools import handle_move_calendar_event
    u = _user(db)
    r = _event(u.id, "quiz", 2, 9, source="model")
    _google(monkeypatch)
    out = handle_move_calendar_event(u.id, {"event_id": r, "new_reminders": [30]}, message_id="m1")
    assert "on your radar" in out and "reminders" in out


# ─── delete tells guests ─────────────────────────────────────────────────────

def test_delete_sends_cancellation_to_guests(db, monkeypatch):
    from agent_tools import handle_delete_calendar_event
    u = _user(db)
    a = _event(u.id, "study group", 2, 19, gid="sg1")
    calls = _google(monkeypatch)
    handle_delete_calendar_event(u.id, {"event_id": a}, message_id="m1")
    out = handle_delete_calendar_event(u.id, {"event_id": a, "confirmed": True}, message_id="m2")
    assert out.startswith("ok: deleted")
    assert [c for c in calls if c[0] == "DELETE"][0][2]["params"] == {"sendUpdates": "all"}


# ─── RSVP ────────────────────────────────────────────────────────────────────

def test_rsvp_decline_confirms_patches_our_attendee_row_and_drops_mirror(db, monkeypatch):
    from agent_tools import handle_rsvp_calendar_event, _get_pending_calendar_event
    u = _user(db)
    a = _event(u.id, "frat meeting", 2, 20, gid="fm1")
    inv = {"id": "fm1", "summary": "frat meeting", "organizer": {"email": "pres@frat.org", "displayName": "Jake"},
           "attendees": [{"email": "pres@frat.org", "organizer": True, "responseStatus": "accepted"},
                         {"email": "nau@gmail.com", "self": True, "responseStatus": "needsAction"}]}
    calls = _google(monkeypatch, get_payload=inv)
    staged = handle_rsvp_calendar_event(u.id, {"query": "frat", "response": "declined"}, message_id="m1")
    assert staged.startswith("not answered yet") and "replying 'no' to 'frat meeting'" in staged and "organizer gets notified" in staged
    assert calls == [] and _get_pending_calendar_event(u.id)["kind"] == "rsvp"
    out = handle_rsvp_calendar_event(u.id, {"query": "frat", "response": "declined", "confirmed": True}, message_id="m2")
    assert out.startswith("ok: declined 'frat meeting'") and "Jake was notified" in out and "took it off their schedule" in out, out
    patch = [c for c in calls if c[0] == "PATCH"][0][2]
    assert patch["params"] == {"sendUpdates": "all"}
    assert patch["body"]["attendees"][1] == {"email": "nau@gmail.com", "self": True, "responseStatus": "declined"}
    assert patch["body"]["attendees"][0]["responseStatus"] == "accepted"      # the organizer's row untouched
    assert _deleted(a)


def test_rsvp_accept_keeps_mirror_and_not_an_invitation_is_honest(db, monkeypatch):
    from agent_tools import handle_rsvp_calendar_event
    u = _user(db)
    a = _event(u.id, "study group", 2, 19, gid="sg1")
    _google(monkeypatch, get_payload={"id": "sg1", "attendees": [{"email": "nau@gmail.com", "self": True, "responseStatus": "needsAction"}]})
    handle_rsvp_calendar_event(u.id, {"event_id": a, "response": "accepted"}, message_id="m1")
    out = handle_rsvp_calendar_event(u.id, {"event_id": a, "response": "accepted", "confirmed": True}, message_id="m2")
    assert out.startswith("ok: replied 'yes'") and not _deleted(a)

    b = _event(u.id, "my own thing", 3, 10, gid="own1")
    _google(monkeypatch, get_payload={"id": "own1", "attendees": []})
    handle_rsvp_calendar_event(u.id, {"event_id": b, "response": "declined"}, message_id="m3")
    out = handle_rsvp_calendar_event(u.id, {"event_id": b, "response": "declined", "confirmed": True}, message_id="m4")
    assert "their OWN event" in out and "nothing to RSVP to" in out and not _deleted(b)

    r = _event(u.id, "radar thing", 2, 9, source="model")
    out = handle_rsvp_calendar_event(u.id, {"event_id": r, "response": "declined"}, message_id="m5")
    assert "not an invitation" in out


def test_rsvp_needs_a_valid_response_and_is_offered_with_the_flag(db, monkeypatch, anthropic_stub):
    from agent_tools import handle_rsvp_calendar_event
    from tests.factories import make_user
    u = _user(db)
    assert handle_rsvp_calendar_event(u.id, {"query": "x", "response": "sure"}).startswith("error: response must be")
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.update(tools=kw.get("tools")) or "hey")
    from agent_loop import run_agent_loop
    user = make_user(db, onboarding_step=3)
    run_agent_loop(user, "yo", "freeform")
    names = {t.get("name") for t in (seen.get("tools") or []) if isinstance(t, dict)}
    assert "rsvp_calendar_event" in names
