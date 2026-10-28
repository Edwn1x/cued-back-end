"""Google Calendar EDIT — move + delete, behind CALENDAR_WRITE_ENABLED (sibling of
test_calendar_writeback.py, which covers create).

Covers, all with the Google API mocked (no network):
  - gcal.delete_event DELETEs the right URL; 404 → EventGone; 403 insufficient-scope →
    WriteAccessDenied; 403 forbidden (not the owner) → WriteForbidden;
  - gcal.update_event PATCHes only the fields given, start+end together;
  - resolution: by [id N] (exact), by title keyword, by keyword + date; several matches
    are LISTED and the model is told to ask; nothing matching is honest;
  - confirm-before-write in CODE: stage → yes in a later turn → write; same-turn confirm,
    nothing staged, changed proposal, and a stale staging are all refused;
  - a confirmed delete hits Google and soft-deletes the mirror (every local copy of that
    Google id); already-gone on Google still clears the mirror;
  - a confirmed move PATCHes Google and updates the mirror row IN PLACE (same id/external_id);
  - coach-logged radar items (source='model') are edited locally, no Google call;
  - bCourses/Canvas items are refused — not theirs to change;
  - a readonly grant / not-owner forbidden / not-connected each produce an honest message,
    never an 'ok';
  - the create tool's mirror is keyed by the account's primary calendar address so the
    next sync updates that row rather than adding a copy;
  - the two tools are offered only when the flag is on.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

import config


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", True)
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


def _token(monkeypatch, value="AT"):
    from integrations import base
    monkeypatch.setattr(base, "get_valid_access_token", lambda uid, prov, **kw: value)


def _no_google(monkeypatch):
    """Any Google write is a test failure."""
    from integrations import gcal
    boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("hit google"))  # noqa: E731
    monkeypatch.setattr(gcal.requests, "delete", boom)
    monkeypatch.setattr(gcal.requests, "patch", boom)
    monkeypatch.setattr(gcal.requests, "post", boom)


def _connected(db, user_id, external_id="nau@gmail.com"):
    from models import get_session, Integration
    s = get_session()
    try:
        s.add(Integration(user_id=user_id, provider="gcal", status="connected", meta={}, external_id=external_id))
        s.commit()
    finally:
        s.close()


def _tomorrow_at(hour, tz="America/Los_Angeles"):
    from zoneinfo import ZoneInfo
    loc = datetime.now(ZoneInfo(tz)).replace(hour=hour, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return loc.astimezone(timezone.utc).replace(tzinfo=None)


def _event(user_id, title, start_utc, *, source="gcal", external_id=None, hours=1, all_day=False):
    from events import upsert_external_event, record_event
    if source == "gcal":
        return upsert_external_event(user_id, source="gcal", external_id=external_id or f"nau@gmail.com:{title}",
                                     title=title, occurred_at=start_utc, ends_at=start_utc + timedelta(hours=hours),
                                     all_day=all_day)
    if source in ("bcourses", "canvas"):
        return upsert_external_event(user_id, source=source, external_id=external_id or f"{source}:{title}",
                                     title=title, occurred_at=start_utc, ends_at=None, all_day=all_day)
    return record_event(user_id, "scheduled", source="model", raw_text=title, occurred_at=start_utc,
                        ends_at=start_utc + timedelta(hours=hours))


def _get(event_id):
    from models import get_session, Event
    s = get_session()
    try:
        e = s.get(Event, event_id)
        return {"title": e.title, "raw_text": e.raw_text, "occurred_at": e.occurred_at, "ends_at": e.ends_at,
                "deleted_at": e.deleted_at, "external_id": e.external_id} if e else None
    finally:
        s.close()


# ─── gcal client ─────────────────────────────────────────────────────────────

def test_delete_event_hits_the_right_url(monkeypatch):
    from integrations import gcal
    _token(monkeypatch)
    seen = {}
    monkeypatch.setattr(gcal.requests, "delete", lambda url, headers=None, params=None, timeout=None: seen.update(url=url, h=headers) or _Resp(204))
    assert gcal.delete_event(7, "nau@gmail.com", "evt 1") is True
    assert seen["url"].endswith("/calendar/v3/calendars/nau%40gmail.com/events/evt%201")
    assert seen["h"]["Authorization"] == "Bearer AT"


def test_delete_event_404_is_gone_403_splits_scope_vs_owner(monkeypatch):
    from integrations import gcal
    _token(monkeypatch)
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _Resp(404, {"error": {"message": "Not Found"}}))
    with pytest.raises(gcal.EventGone):
        gcal.delete_event(7, "primary", "x")
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _Resp(403, {"error": {
        "message": "Request had insufficient authentication scopes.",
        "errors": [{"reason": "insufficientPermissions"}]}}))
    with pytest.raises(gcal.WriteAccessDenied):
        gcal.delete_event(7, "primary", "x")
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _Resp(403, {"error": {
        "message": "Forbidden", "errors": [{"reason": "forbidden"}]}}))
    with pytest.raises(gcal.WriteForbidden):
        gcal.delete_event(7, "primary", "x")


def test_delete_event_not_connected(monkeypatch):
    from integrations import gcal
    _token(monkeypatch, None)
    _no_google(monkeypatch)
    with pytest.raises(gcal.NotConnected):
        gcal.delete_event(7, "primary", "x")


def test_update_event_patches_only_given_fields(monkeypatch):
    from integrations import gcal
    _token(monkeypatch)
    seen = {}

    def fake_patch(url, headers=None, json=None, params=None, timeout=None):
        seen.update(url=url, body=json)
        return _Resp(200, {"id": "e1"})
    monkeypatch.setattr(gcal.requests, "patch", fake_patch)
    s = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)
    gcal.update_event(7, "primary", "e1", start_dt=s, end_dt=s + timedelta(hours=1))
    assert seen["url"].endswith("/calendars/primary/events/e1")
    assert seen["body"] == {"start": {"dateTime": "2026-10-28T00:00:00Z"}, "end": {"dateTime": "2026-10-28T01:00:00Z"}}
    gcal.update_event(7, "primary", "e1", summary="legs")
    assert seen["body"] == {"summary": "legs"}
    with pytest.raises(ValueError):
        gcal.update_event(7, "primary", "e1")


# ─── resolution ──────────────────────────────────────────────────────────────

def test_delete_needs_an_identifier_and_is_honest_when_nothing_matches(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    _no_google(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    _event(u.id, "gym — push", _tomorrow_at(16))
    assert handle_delete_calendar_event(u.id, {}, message_id="m1").startswith("error: say which event")
    out = handle_delete_calendar_event(u.id, {"query": "dentist"}, message_id="m1")
    assert out.startswith("no event matching 'dentist'") and "don't guess" in out


def test_several_matches_are_listed_not_picked(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    _no_google(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    a = _event(u.id, "CS61A lecture", _tomorrow_at(10), external_id="c:a")
    b = _event(u.id, "CS61A lecture", _tomorrow_at(10) + timedelta(days=2), external_id="c:b")
    out = handle_delete_calendar_event(u.id, {"query": "cs61a"}, message_id="m1")
    assert out.startswith("several match") and f"[id {a}]" in out and f"[id {b}]" in out
    # a date pins it to one; the stage then names that one
    out = handle_delete_calendar_event(u.id, {"query": "cs61a", "date": "tomorrow"}, message_id="m1")
    assert out.startswith("not deleted yet") and "10:00am" in out
    assert _get(a)["deleted_at"] is None and _get(b)["deleted_at"] is None


def test_bcourses_item_is_refused(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event, handle_move_calendar_event
    _no_google(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    hw = _event(u.id, "HW4 due", _tomorrow_at(23), source="bcourses")
    out = handle_delete_calendar_event(u.id, {"event_id": hw, "confirmed": True}, message_id="m1")
    assert out.startswith("can't be deleted") and "bCourses" in out
    out = handle_move_calendar_event(u.id, {"event_id": hw, "new_starts_at": "10:00"}, message_id="m1")
    assert out.startswith("can't be moved") and "bCourses" in out
    assert _get(hw)["deleted_at"] is None


# ─── confirm gate (shared with create) ───────────────────────────────────────

def test_delete_confirm_gate_refuses_unstaged_same_turn_and_changed(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event, _set_pending_calendar_event, _get_pending_calendar_event
    _no_google(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    a = _event(u.id, "gym — push", _tomorrow_at(16), external_id="c:a")
    b = _event(u.id, "study block", _tomorrow_at(19), external_id="c:b")

    out = handle_delete_calendar_event(u.id, {"event_id": a, "confirmed": True}, message_id="m1")
    assert out.startswith("not deleted — nothing was staged") and "gym — push" in out
    # staged now (by the refused call); confirming in the SAME turn is refused
    out = handle_delete_calendar_event(u.id, {"event_id": a, "confirmed": True}, message_id="m1")
    assert "SAME turn" in out
    # next turn but a DIFFERENT event → restage, not delete
    out = handle_delete_calendar_event(u.id, {"event_id": b, "confirmed": True}, message_id="m2")
    assert out.startswith("not deleted — the proposal changed") and "study block" in out
    # stale staging → refused
    p = _get_pending_calendar_event(u.id); p["staged_at"] = "2026-01-01T00:00:00"; _set_pending_calendar_event(u.id, p)
    out = handle_delete_calendar_event(u.id, {"event_id": b, "confirmed": True}, message_id="m3")
    assert "stale" in out
    assert _get(a)["deleted_at"] is None and _get(b)["deleted_at"] is None


def test_a_staged_delete_cannot_be_confirmed_as_a_create(db, monkeypatch):
    """Same pending slot, different kind — a create confirm after a delete stage must restage."""
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event, handle_create_calendar_event
    _no_google(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    a = _event(u.id, "gym", _tomorrow_at(16), external_id="c:a")
    handle_delete_calendar_event(u.id, {"event_id": a}, message_id="m1")
    out = handle_create_calendar_event(u.id, {"summary": "gym", "starts_at": "16:00", "date": "tomorrow", "confirmed": True}, message_id="m2")
    assert out.startswith("not created — the proposal changed")


# ─── delete: Google + mirror ─────────────────────────────────────────────────

def test_confirmed_delete_hits_google_and_drops_every_local_copy(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    from integrations import gcal
    from models import get_session, User
    _token(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    start = _tomorrow_at(16)
    a = _event(u.id, "gym — push", start, external_id="nau@gmail.com:evt9")
    dup = _event(u.id, "gym — push", start, external_id="primary:evt9")      # an older coach-created mirror
    other = _event(u.id, "gym — pull", start + timedelta(days=1), external_id="nau@gmail.com:evt10")
    seen = {}
    monkeypatch.setattr(gcal.requests, "delete", lambda url, headers=None, params=None, timeout=None: seen.update(url=url) or _Resp(204))

    staged = handle_delete_calendar_event(u.id, {"query": "push"}, message_id="m1")
    assert staged.startswith("not deleted yet") and "removing 'gym — push'" in staged and "4:00pm" in staged
    out = handle_delete_calendar_event(u.id, {"query": "push", "confirmed": True}, message_id="m2")
    assert out.startswith("ok: deleted 'gym — push'")
    assert seen["url"].endswith("/calendars/nau%40gmail.com/events/evt9")
    assert _get(a)["deleted_at"] is not None
    assert _get(dup)["deleted_at"] is not None          # the primary:<id> copy went too
    assert _get(other)["deleted_at"] is None            # untouched
    s = get_session()
    try:
        assert s.get(User, u.id).pending_calendar_event is None
    finally:
        s.close()


def test_delete_already_gone_on_google_still_clears_mirror(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    from integrations import gcal
    _token(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    a = _event(u.id, "gym", _tomorrow_at(16), external_id="nau@gmail.com:evt9")
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _Resp(404, {"error": {"message": "Not Found"}}))
    handle_delete_calendar_event(u.id, {"event_id": a}, message_id="m1")
    out = handle_delete_calendar_event(u.id, {"event_id": a, "confirmed": True}, message_id="m2")
    assert out.startswith("ok") and "already gone" in out
    assert _get(a)["deleted_at"] is not None


def test_delete_readonly_grant_and_not_owner_are_honest(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    from integrations import gcal
    _token(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    a = _event(u.id, "gym", _tomorrow_at(16), external_id="nau@gmail.com:evt9")
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _Resp(403, {"error": {
        "message": "Request had insufficient authentication scopes.", "errors": [{"reason": "insufficientPermissions"}]}}))
    handle_delete_calendar_event(u.id, {"event_id": a}, message_id="m1")
    out = handle_delete_calendar_event(u.id, {"event_id": a, "confirmed": True}, message_id="m2")
    assert "read-only" in out and "reconnect" in out and not out.startswith("ok")
    assert _get(a)["deleted_at"] is None
    monkeypatch.setattr(gcal.requests, "delete", lambda *a, **k: _Resp(403, {"error": {
        "message": "Forbidden", "errors": [{"reason": "forbidden"}]}}))
    out = handle_delete_calendar_event(u.id, {"event_id": a, "confirmed": True}, message_id="m3")
    assert "isn't theirs to delete" in out and not out.startswith("ok")
    assert _get(a)["deleted_at"] is None


def test_delete_not_connected_is_honest(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    _token(monkeypatch, None)
    _no_google(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    a = _event(u.id, "gym", _tomorrow_at(16), external_id="nau@gmail.com:evt9")   # stale mirror, row gone
    handle_delete_calendar_event(u.id, {"event_id": a}, message_id="m1")
    out = handle_delete_calendar_event(u.id, {"event_id": a, "confirmed": True}, message_id="m2")
    assert "no google calendar connected" in out and not out.startswith("ok")
    assert _get(a)["deleted_at"] is None


def test_radar_item_is_taken_off_locally_without_google(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_delete_calendar_event
    _no_google(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    r = _event(u.id, "quiz 3 (chem)", _tomorrow_at(9), source="model")
    staged = handle_delete_calendar_event(u.id, {"query": "quiz"}, message_id="m1")
    assert "your radar" in staged and "not their google calendar" in staged
    out = handle_delete_calendar_event(u.id, {"query": "quiz", "confirmed": True}, message_id="m2")
    assert out.startswith("ok: took 'quiz 3 (chem)'") and "off my radar" in out
    assert _get(r)["deleted_at"] is not None


# ─── move: Google + mirror ───────────────────────────────────────────────────

def test_confirmed_move_patches_google_and_updates_mirror_in_place(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    from integrations import gcal
    _token(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    start = _tomorrow_at(16)
    a = _event(u.id, "gym — push", start, external_id="nau@gmail.com:evt9")
    seen = {}

    def fake_patch(url, headers=None, json=None, params=None, timeout=None):
        seen.update(url=url, body=json)
        return _Resp(200, {"id": "evt9"})
    monkeypatch.setattr(gcal.requests, "patch", fake_patch)

    staged = handle_move_calendar_event(u.id, {"query": "push", "new_starts_at": "17:00"}, message_id="m1")
    assert staged.startswith("not moved yet") and "4:00pm–5:00pm to" in staged and "5:00pm–6:00pm" in staged
    assert "5am or 5pm?" in staged                       # bare-hour ask rides along
    out = handle_move_calendar_event(u.id, {"query": "push", "new_starts_at": "17:00", "confirmed": True}, message_id="m2")
    assert out.startswith("ok: 'gym — push' is now") and "5:00pm–6:00pm" in out
    assert seen["url"].endswith("/calendars/nau%40gmail.com/events/evt9")
    assert set(seen["body"]) == {"start", "end"}        # no summary sent when the title didn't change
    row = _get(a)
    assert row["external_id"] == "nau@gmail.com:evt9"  # same row, same key — the next sync is a no-op
    assert row["occurred_at"] == start + timedelta(hours=1)
    assert row["ends_at"] == start + timedelta(hours=2)   # old length kept
    assert row["deleted_at"] is None


def test_move_to_another_day_keeps_clock_time_and_rename_only_sends_summary(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    from integrations import gcal
    _token(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    start = _tomorrow_at(16)
    a = _event(u.id, "gym — push", start, external_id="nau@gmail.com:evt9")
    bodies = []
    monkeypatch.setattr(gcal.requests, "patch", lambda url, headers=None, json=None, params=None, timeout=None: bodies.append(json) or _Resp(200, {}))

    day_after = (start + timedelta(days=1)).replace(tzinfo=timezone.utc)
    from zoneinfo import ZoneInfo
    nd = day_after.astimezone(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    handle_move_calendar_event(u.id, {"event_id": a, "new_date": nd}, message_id="m1")
    out = handle_move_calendar_event(u.id, {"event_id": a, "new_date": nd, "confirmed": True}, message_id="m2")
    assert out.startswith("ok") and "4:00pm–5:00pm" in out
    assert _get(a)["occurred_at"] == start + timedelta(days=1)

    handle_move_calendar_event(u.id, {"event_id": a, "new_summary": "gym — legs"}, message_id="m3")
    out = handle_move_calendar_event(u.id, {"event_id": a, "new_summary": "gym — legs", "confirmed": True}, message_id="m4")
    assert out.startswith("ok: 'gym — legs'")
    assert bodies[-1] == {"summary": "gym — legs"}
    assert _get(a)["title"] == "gym — legs"


def test_move_with_nothing_different_is_a_question_not_a_write(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    _no_google(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    a = _event(u.id, "gym", _tomorrow_at(16), external_id="c:a")
    out = handle_move_calendar_event(u.id, {"event_id": a, "new_starts_at": "16:00", "confirmed": True}, message_id="m1")
    assert out.startswith("nothing to change")


def test_move_readonly_grant_is_honest_and_mirror_untouched(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    from integrations import gcal
    _token(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id)
    start = _tomorrow_at(16)
    a = _event(u.id, "gym", start, external_id="nau@gmail.com:evt9")
    monkeypatch.setattr(gcal.requests, "patch", lambda *a, **k: _Resp(403, {"error": {
        "message": "Request had insufficient authentication scopes.", "errors": [{"reason": "insufficientPermissions"}]}}))
    handle_move_calendar_event(u.id, {"event_id": a, "new_starts_at": "17:00"}, message_id="m1")
    out = handle_move_calendar_event(u.id, {"event_id": a, "new_starts_at": "17:00", "confirmed": True}, message_id="m2")
    assert "read-only" in out and not out.startswith("ok")
    assert _get(a)["occurred_at"] == start


def test_move_radar_item_updates_locally(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_move_calendar_event
    _no_google(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    start = _tomorrow_at(9)
    r = _event(u.id, "quiz 3", start, source="model")
    handle_move_calendar_event(u.id, {"event_id": r, "new_starts_at": "11:00"}, message_id="m1")
    out = handle_move_calendar_event(u.id, {"event_id": r, "new_starts_at": "11:00", "confirmed": True}, message_id="m2")
    assert out.startswith("ok") and "on your radar" in out
    assert _get(r)["occurred_at"] == start + timedelta(hours=2)


# ─── create's mirror key follows the sync layer ───────────────────────────────

def test_create_mirror_is_keyed_by_primary_calendar_address(db, monkeypatch):
    from tests.factories import make_user
    from agent_tools import handle_create_calendar_event
    from integrations import gcal
    from models import get_session, Event, active
    _token(monkeypatch)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _connected(db, u.id, external_id="nau@gmail.com")
    monkeypatch.setattr(gcal.requests, "post", lambda *a, **k: _Resp(200, {"id": "evtABC"}))
    handle_create_calendar_event(u.id, {"summary": "gym", "starts_at": "16:00", "date": "tomorrow"}, message_id="m1")
    out = handle_create_calendar_event(u.id, {"summary": "gym", "starts_at": "16:00", "date": "tomorrow", "confirmed": True}, message_id="m2")
    assert out.startswith("ok")
    s = get_session()
    try:
        assert active(s, Event, user_id=u.id).one().external_id == "nau@gmail.com:evtABC"
    finally:
        s.close()


# ─── loop assembly ───────────────────────────────────────────────────────────

def _tool_names_offered(user, anthropic_stub):
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.update(tools=kw.get("tools")) or "hey")
    from agent_loop import run_agent_loop
    run_agent_loop(user, "yo", "freeform")
    return {t.get("name") for t in (seen.get("tools") or []) if isinstance(t, dict)}


def test_edit_tools_follow_the_flag(db, monkeypatch, anthropic_stub):
    from tests.factories import make_user
    user = make_user(db, onboarding_step=3)
    names = _tool_names_offered(user, anthropic_stub)
    assert {"create_calendar_event", "move_calendar_event", "delete_calendar_event"} <= names
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", False)
    names = _tool_names_offered(user, anthropic_stub)
    assert not ({"move_calendar_event", "delete_calendar_event"} & names)
