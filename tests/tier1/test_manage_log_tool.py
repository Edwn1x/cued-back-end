"""
Phase 3 tool 3 — manage_log. list / edit / soft-delete by short stable id.
Deletes are soft; meal changes recompute today's totals. Handler returns 'ok:'
ONLY on real success — the deterministic half of the honesty invariant.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _naive_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_delete_meal_correction_round_trip(db):
    """The acceptance case: a duplicate meal is deleted → daily totals and the
    loop context both exclude it; the remaining meal stays."""
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, User, recompute_daily_totals
    from agent_loop import build_loop_context

    user = make_user(db)
    now = _naive_now()
    s = get_session()
    try:
        keep = Meal(user_id=user.id, description="grilled chicken bowl", calories=650,
                    protein_g=55, eaten_at=now)
        dup = Meal(user_id=user.id, description="duplicate pork chops", calories=650,
                   protein_g=72, eaten_at=now)
        s.add(keep); s.add(dup); s.commit()
        dup_id = dup.id
    finally:
        s.close()
    recompute_daily_totals(user.id)
    s = get_session()
    try:
        assert s.query(User).get(user.id).calories_today == 1300  # both counted
    finally:
        s.close()

    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": dup_id})
    assert out.startswith("ok"), out

    s = get_session()
    try:
        u = s.query(User).get(user.id)
        cals = u.calories_today
        ctx = build_loop_context(u, s)
    finally:
        s.close()
    assert cals == 650, f"totals didn't drop after delete: {cals}"
    assert "grilled chicken bowl" in ctx
    assert "duplicate pork chops" not in ctx, "deleted meal is a ghost in context"


def test_delete_nonexistent_is_honest_error(db):
    """Honesty: the handler returns 'error', never a false 'ok', so the agent has
    no success result to (truthfully) confirm."""
    from tests.factories import make_user
    from agent_tools import handle_manage_log

    user = make_user(db)
    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": 999999})
    assert out.startswith("error") and "no active meal" in out


def test_edit_meal_recomputes_totals(db):
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, User, recompute_daily_totals

    user = make_user(db)
    s = get_session()
    try:
        m = Meal(user_id=user.id, description="burrito", calories=900, protein_g=40,
                 eaten_at=_naive_now())
        s.add(m); s.commit()
        mid = m.id
    finally:
        s.close()
    recompute_daily_totals(user.id)

    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                      "fields": {"calories": 500, "protein_g": 35}})
    assert out.startswith("ok"), out
    s = get_session()
    try:
        u = s.query(User).get(user.id)
        assert u.calories_today == 500 and u.protein_today == 35
    finally:
        s.close()


def test_manage_log_delete_via_loop(db, driver, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from models import get_session, Meal, active

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "MANAGE_LOG_TOOL_ENABLED", True)

    user = make_user(db)
    s = get_session()
    try:
        m = Meal(user_id=user.id, description="dup shake", calories=300, protein_g=30,
                 eaten_at=_naive_now())
        s.add(m); s.commit()
        mid = m.id
    finally:
        s.close()

    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        loop_calls.append(1)
        if len(loop_calls) == 1:
            return ToolUse("manage_log", {"action": "delete", "entity": "meal", "id": mid})
        return "removed the duplicate shake, you're good"

    anthropic_stub.reply_with(handler)
    driver.send(user, "yo you logged that shake twice, kill one")

    s = get_session()
    try:
        remaining = active(s, Meal, user_id=user.id).count()
    finally:
        s.close()
    assert remaining == 0, "manage_log tool did not soft-delete the meal"


def test_manage_log_lists_and_deletes_events(db):
    """The deletion surface must follow the new write path: a model-logged Event
    shows in list WITH an id, deletes by id, and then disappears from the context
    the heartbeat reads (todays_events → build_loop_context)."""
    import re
    from tests.factories import make_user
    from agent_tools import handle_log_event, handle_manage_log
    from agent_loop import build_loop_context
    from events import todays_events
    from models import get_session, User

    user = make_user(db)
    handle_log_event(user.id, {"description": "founder summit", "starts_at": "12:00", "ends_at": "14:30"})

    # list surfaces the event with an id (delete/edit already accept entity=event)
    listing = handle_manage_log(user.id, {"action": "list"})
    assert "event [id" in listing and "founder summit" in listing, listing
    eid = int(re.search(r"event \[id (\d+)\]", listing).group(1))

    # and it's referenceable by id in the loop/heartbeat context
    s = get_session()
    try:
        ctx_before = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert f"[id {eid}]" in ctx_before and "founder summit" in ctx_before

    # delete by id → real deletion, honest ok
    out = handle_manage_log(user.id, {"action": "delete", "entity": "event", "id": eid})
    assert out.startswith("ok: deleted event"), out

    # gone from the event floor AND from the context the heartbeat sees
    assert all("founder summit" not in (e.raw_text or "") for e in todays_events(user.id))
    s = get_session()
    try:
        ctx_after = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "founder summit" not in ctx_after
