"""
Live 2026-10-28 14:29–14:41 PT, user 51 (founder): "Now I have until Friday 11:59 … midterm for c104
tmr 7-9pm" → the coach "moved the 70 hw" and "put the c104 midterm on ur calendar" — neither was on
Google (one was an edit to our copy of a synced event, the other a model-only event). "Which calendar?
I'm not seeing it" → "want me to add them there?" → "Yeah" ×5, and every yes came back as "send one
more yes". Three defects, each fixed in code here:

  1. The confirm gate held ONE pending proposal. Staging the midterm then the HW left only the HW;
     the yes confirmed the midterm (≠ pending → re-staged, "proposal changed") then the HW (≠ the
     midterm now pending → re-staged). Neither ever wrote. Pending is now a list keyed per target;
     a confirmed write clears only its own entry; staging names everything else the same yes covers.
  2. manage_log edited the synced row (gcal event 518) in place; the next sync put it back within
     minutes. Edits/deletes of synced rows are refused and pointed at the tool that changes the
     source (move/delete_calendar_event for Google; log_event for a bCourses/Canvas extension).
  3. log_event's result never said the event is only on our side, so the coach said "on ur
     calendar" to someone with Google connected. It says so now.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
from tests.tier1.test_calendar_edit import _Resp, _token, _connected, _event, _get, _tomorrow_at, _no_google  # noqa: F401


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_WRITE_ENABLED", True)
    yield


LA = ZoneInfo("America/Los_Angeles")


def _day(n):
    return (datetime.now(LA) + timedelta(days=n)).date().isoformat()


def MIDTERM():
    return {"summary": "DATA C104 midterm", "date": _day(1), "starts_at": "19:00", "ends_at": "21:00"}


def HW():
    return {"summary": "CS 70 HW due", "date": _day(2), "starts_at": "23:00", "ends_at": "23:59"}


def _posts(monkeypatch):
    from integrations import gcal
    got = []

    def fake_post(url, headers=None, json=None, params=None, timeout=None):
        got.append(json["summary"])
        return _Resp(200, {"id": f"evt{len(got)}"})
    monkeypatch.setattr(gcal.requests, "post", fake_post)
    return got


def _user(db):
    from tests.factories import make_user
    return make_user(db, user_timezone="America/Los_Angeles")


# ── 1. several proposals wait on the same yes ────────────────────────────────

def test_live_user_51_two_staged_creates_both_write_on_one_yes(db, monkeypatch):
    from agent_tools import handle_create_calendar_event as create, _get_pending_calendar_event
    _token(monkeypatch)
    posts = _posts(monkeypatch)
    u = _user(db)
    assert create(u.id, MIDTERM(), message_id="m1").startswith("not created yet")
    assert create(u.id, HW(), message_id="m1").startswith("not created yet")
    a = create(u.id, {**MIDTERM(), "confirmed": True}, message_id="m2")
    b = create(u.id, {**HW(), "confirmed": True}, message_id="m2")
    assert a.startswith("ok") and b.startswith("ok"), (a, b)
    assert posts == ["DATA C104 midterm", "CS 70 HW due"]
    assert _get_pending_calendar_event(u.id) is None


def test_confirm_order_does_not_matter(db, monkeypatch):
    from agent_tools import handle_create_calendar_event as create
    _token(monkeypatch)
    posts = _posts(monkeypatch)
    u = _user(db)
    create(u.id, MIDTERM(), message_id="m1")
    create(u.id, HW(), message_id="m1")
    assert create(u.id, {**HW(), "confirmed": True}, message_id="m2").startswith("ok")
    assert create(u.id, {**MIDTERM(), "confirmed": True}, message_id="m2").startswith("ok")
    assert posts == ["CS 70 HW due", "DATA C104 midterm"]


def test_staging_names_what_else_the_same_yes_covers(db, monkeypatch):
    from agent_tools import handle_create_calendar_event as create
    _no_google(monkeypatch)
    u = _user(db)
    create(u.id, MIDTERM(), message_id="m1")
    out = create(u.id, HW(), message_id="m1")
    assert "DATA C104 midterm" in out and "one yes" in out


def test_a_write_clears_only_its_own_entry(db, monkeypatch):
    from agent_tools import handle_create_calendar_event as create, _pending_list
    _token(monkeypatch)
    _posts(monkeypatch)
    u = _user(db)
    create(u.id, MIDTERM(), message_id="m1")
    create(u.id, HW(), message_id="m1")
    create(u.id, {**MIDTERM(), "confirmed": True}, message_id="m2")
    left = _pending_list(u.id)
    assert [p["summary"] for p in left] == ["CS 70 HW due"]


def test_revising_one_proposal_replaces_it_and_leaves_the_other(db, monkeypatch):
    """'actually make the midterm 8' re-stages the midterm (same target) — the HW still waits."""
    from agent_tools import handle_create_calendar_event as create, _pending_list
    _token(monkeypatch)
    posts = _posts(monkeypatch)
    u = _user(db)
    create(u.id, MIDTERM(), message_id="m1")
    create(u.id, HW(), message_id="m1")
    create(u.id, {**MIDTERM(), "starts_at": "20:00"}, message_id="m2")
    assert sorted(p["summary"] for p in _pending_list(u.id)) == ["CS 70 HW due", "DATA C104 midterm"]
    old = create(u.id, {**MIDTERM(), "confirmed": True}, message_id="m3")    # the 7pm one is gone
    assert old.startswith("not created — the proposal changed")
    assert create(u.id, {**HW(), "confirmed": True}, message_id="m3").startswith("ok")
    assert posts == ["CS 70 HW due"]


def test_same_turn_confirm_is_still_refused_per_entry(db, monkeypatch):
    from agent_tools import handle_create_calendar_event as create
    _no_google(monkeypatch)
    u = _user(db)
    create(u.id, MIDTERM(), message_id="m1")
    create(u.id, HW(), message_id="m2")
    out = create(u.id, {**HW(), "confirmed": True}, message_id="m2")
    assert "SAME turn" in out


def test_a_legacy_single_pending_dict_still_confirms(db, monkeypatch):
    """Rows staged before the deploy hold one dict, not a list."""
    from agent_tools import handle_create_calendar_event as create
    from models import get_session, User
    _token(monkeypatch)
    posts = _posts(monkeypatch)
    u = _user(db)
    create(u.id, MIDTERM(), message_id="m1")
    s = get_session()
    try:
        row = s.get(User, u.id)
        legacy = row.pending_calendar_event
        legacy = legacy[0] if isinstance(legacy, list) else legacy
        row.pending_calendar_event = dict(legacy)
        s.commit()
    finally:
        s.close()
    assert create(u.id, {**MIDTERM(), "confirmed": True}, message_id="m2").startswith("ok")
    assert posts == ["DATA C104 midterm"]


def test_a_delete_and_a_create_can_wait_on_the_same_yes(db, monkeypatch):
    from agent_tools import handle_create_calendar_event as create, handle_delete_calendar_event as delete
    from integrations import gcal
    _token(monkeypatch)
    posts = _posts(monkeypatch)
    deleted = []
    monkeypatch.setattr(gcal.requests, "delete", lambda url, **k: deleted.append(url) or _Resp(204))
    u = _user(db)
    _connected(db, u.id)
    ev = _event(u.id, "old study block", _tomorrow_at(15), external_id="nau@gmail.com:evtOLD")
    assert delete(u.id, {"event_id": ev}, message_id="m1").startswith("not deleted yet")
    create(u.id, MIDTERM(), message_id="m1")
    assert delete(u.id, {"event_id": ev, "confirmed": True}, message_id="m2").startswith("ok")
    assert create(u.id, {**MIDTERM(), "confirmed": True}, message_id="m2").startswith("ok")
    assert len(deleted) == 1 and posts == ["DATA C104 midterm"]


def test_clearing_all_pending_still_works(db):
    """account_lifecycle wipes it with None."""
    from agent_tools import _set_pending_calendar_event, _pending_list, _get_pending_calendar_event
    u = _user(db)
    _set_pending_calendar_event(u.id, {"kind": "create", "summary": "x", "staged_at": datetime.now().isoformat()})
    _set_pending_calendar_event(u.id, None)
    assert _pending_list(u.id) == [] and _get_pending_calendar_event(u.id) is None


# ── 2. a synced row is changed at its source, never in our copy ──────────────

def test_editing_a_google_event_in_our_copy_is_refused(db, monkeypatch):
    from agent_tools import handle_manage_log
    _no_google(monkeypatch)
    u = _user(db)
    start = _tomorrow_at(16)
    ev = _event(u.id, "CS 70 HW Due", start, external_id="ed@berkeley.edu:639tp_20261028T230000Z")
    out = handle_manage_log(u.id, {"action": "edit", "entity": "event", "id": ev, "fields": {"date": _day(3)}})
    assert not out.startswith("ok") and "move_calendar_event" in out and "sync" in out
    assert _get(ev)["occurred_at"] == start


def test_deleting_a_google_event_in_our_copy_is_refused(db, monkeypatch):
    from agent_tools import handle_manage_log
    _no_google(monkeypatch)
    u = _user(db)
    ev = _event(u.id, "CS 61C Lecture", _tomorrow_at(11), external_id="nau@gmail.com:evt61c")
    out = handle_manage_log(u.id, {"action": "delete", "entity": "event", "id": ev})
    assert not out.startswith("ok") and "delete_calendar_event" in out
    assert _get(ev)["deleted_at"] is None


def test_a_bcourses_extension_is_logged_as_a_new_item_not_edited(db, monkeypatch):
    """u77 the same day: 'got an extension, next friday' — the bCourses row can't change at its
    source; the new date goes in as its own log_event item."""
    from agent_tools import handle_manage_log
    u = _user(db)
    start = _tomorrow_at(0)
    ev = _event(u.id, "due: Lab 7: Op Amps 1", start, source="bcourses")
    out = handle_manage_log(u.id, {"action": "edit", "entity": "event", "id": ev, "fields": {"date": _day(9)}})
    assert not out.startswith("ok") and "log_event" in out and "bCourses" in out
    assert _get(ev)["occurred_at"] == start


def test_a_coach_logged_event_still_edits_in_place(db):
    from agent_tools import handle_manage_log
    u = _user(db)
    ev = _event(u.id, "c104 study", _tomorrow_at(14), source="model")
    out = handle_manage_log(u.id, {"action": "edit", "entity": "event", "id": ev, "fields": {"starts_at": "15:00"}})
    assert out.startswith("ok: edited event")


# ── 3. log_event says where it lives ─────────────────────────────────────────

def test_log_event_tells_a_connected_user_it_is_not_on_google(db):
    from agent_tools import handle_log_event
    u = _user(db)
    _connected(db, u.id)
    out = handle_log_event(u.id, {"description": "DATA C104 midterm", "date": _day(1), "starts_at": "19:00",
                                   "ends_at": "21:00"})
    assert out.startswith("ok: noted") and "NOT on their google calendar" in out and "create_calendar_event" in out


def test_log_event_without_google_is_unchanged(db):
    from agent_tools import handle_log_event
    u = _user(db)
    out = handle_log_event(u.id, {"description": "DATA C104 midterm", "date": _day(1), "starts_at": "19:00"})
    assert out.startswith("ok: noted") and "google" not in out
