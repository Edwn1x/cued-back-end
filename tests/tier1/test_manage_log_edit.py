"""
Burn-in item 3 — manage_log edit: field-level, ID-targeted, audited; events editable
(description/start/end) like meals and workouts. An edited row otherwise silently claims
to have always held its new value, so the prior value is retained in row.edits.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta


def _naive_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(model, rid):
    from models import get_session
    s = get_session()
    try:
        return s.get(model, rid)
    finally:
        s.close()


def test_meal_edit_round_trip_recomputes_and_audits(db):
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, User

    user = make_user(db, calorie_target=2000)
    s = get_session()
    try:
        m = Meal(user_id=user.id, description="burrito", calories=1250, protein_g=40,
                 eaten_at=_naive_utcnow(), source="text", log_type="user_reported")
        s.add(m); s.commit(); mid = m.id
    finally:
        s.close()

    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal",
                                      "id": mid, "fields": {"calories": 900}})
    assert out.startswith("ok: edited meal"), out

    row = _row(Meal, mid)
    assert row.calories == 900 and row.protein_g == 40  # partial: protein untouched
    assert row.edits and row.edits[-1]["field"] == "calories"
    assert row.edits[-1]["old"] == 1250 and row.edits[-1]["new"] == 900  # audit trail
    # totals recomputed from source (not delta-patched)
    assert _row(User, user.id).calories_today == 900


def test_event_edit_start_time_local_and_reflected_in_context(db):
    from tests.factories import make_user
    from agent_tools import handle_log_event, handle_manage_log
    from agent_loop import build_loop_context
    from events import todays_events
    from models import get_session, Event, User

    user = make_user(db)  # America/Los_Angeles
    handle_log_event(user.id, {"description": "founder summit", "starts_at": "12:00", "ends_at": "14:30"})
    eid = todays_events(user.id)[0].id

    out = handle_manage_log(user.id, {"action": "edit", "entity": "event",
                                      "id": eid, "fields": {"starts_at": "13:00"}})
    assert out.startswith("ok: edited event"), out

    # stored naive-UTC start moved forward one local hour; context shows the new local time
    row = _row(Event, eid)
    assert row.edits[-1]["field"] == "starts_at"
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "1:00 PM" in ctx and "12:00 PM" not in ctx, ctx

    # soft-delete removes it from context + the event floor
    handle_manage_log(user.id, {"action": "delete", "entity": "event", "id": eid})
    assert all("founder summit" not in (e.raw_text or "") for e in todays_events(user.id))


def test_event_edit_keeps_the_day(db):
    """Editing only the clock time must not shift the event to a different day."""
    from tests.factories import make_user
    from agent_tools import handle_log_event, handle_manage_log
    from events import todays_events

    user = make_user(db)
    handle_log_event(user.id, {"description": "lab", "starts_at": "09:00"})
    eid = todays_events(user.id)[0].id
    before_day = todays_events(user.id)[0].occurred_at.date()

    handle_manage_log(user.id, {"action": "edit", "entity": "event", "id": eid,
                                "fields": {"starts_at": "20:00"}})
    # still surfaces in TODAY's events — same local day, later clock time
    ev = [e for e in todays_events(user.id) if e.id == eid]
    assert ev, "editing the time dropped the event from today"
    assert ev[0].occurred_at.date() == before_day or ev[0].occurred_at.date() == (before_day + timedelta(days=1))


def test_partial_update_leaves_other_fields(db):
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal

    user = make_user(db)
    s = get_session()
    try:
        m = Meal(user_id=user.id, description="bowl", calories=500, protein_g=35,
                 carbs_g=60, eaten_at=_naive_utcnow(), source="text", log_type="user_reported")
        s.add(m); s.commit(); mid = m.id
    finally:
        s.close()
    handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                "fields": {"protein_g": 45}})
    row = _row(Meal, mid)
    assert (row.protein_g, row.calories, row.carbs_g, row.description) == (45, 500, 60, "bowl")


def test_edit_errors_never_false_success(db):
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal

    user = make_user(db)
    other = make_user(db)
    s = get_session()
    try:
        m = Meal(user_id=other.id, description="theirs", calories=100,
                 eaten_at=_naive_utcnow(), source="text", log_type="user_reported")
        s.add(m); s.commit(); other_mid = m.id
        d = Meal(user_id=user.id, description="gone", calories=100, eaten_at=_naive_utcnow(),
                 source="text", log_type="user_reported", deleted_at=_naive_utcnow())
        s.add(d); s.commit(); deleted_mid = d.id
    finally:
        s.close()

    # invalid id, cross-user id, already-deleted id -> explicit error, never "ok"
    for bad in (999999, other_mid, deleted_mid):
        out = handle_manage_log(user.id, {"action": "edit", "entity": "meal",
                                          "id": bad, "fields": {"calories": 1}})
        assert out.startswith("error"), (bad, out)

    # a non-editable field name -> error, not silent success
    s = get_session()
    try:
        m = Meal(user_id=user.id, description="mine", calories=200, eaten_at=_naive_utcnow(),
                 source="text", log_type="user_reported")
        s.add(m); s.commit(); mine = m.id
    finally:
        s.close()
    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mine,
                                      "fields": {"bogus": 1}})
    assert out.startswith("error") and _row(Meal, mine).calories == 200
