"""
Tier-2 (live, judged) — the three burn-in findings, end to end with the real model.

1. A dated schedule item routes to an EVENT (log_event), not the `schedule` memory
   category — the fix for schedule facts vanishing under the soft cap.
2. Under factual push-back, the coach VERIFIES before conceding rather than folding.
3. The core-promise chain: a dated event the model logged is visible to the heartbeat,
   so a well-timed proactive nudge becomes possible ("summit at noon → i'll ping you").

Run: pytest --run-tier2 -s tests/tier2/test_schedule_events_and_verify.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.tier2


def _events(user_id):
    from events import todays_events
    return todays_events(user_id)


def _schedule_mem(user_id):
    from models import get_session, User
    s = get_session()
    try:
        prof = s.get(User, user_id).user_profile_memory or {}
    finally:
        s.close()
    return [e.get("text") for e in (prof.get("schedule") or [])]


def test_dated_schedule_routes_to_event_not_memory(db, monkeypatch):
    """Finding #1: 'lab till 2 today, founder summit noon-2:30' should become Events
    (dated, auto-expiring), NOT permanent schedule memory facts."""
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "REMEMBER_TOOL_ENABLED", True)

    user = make_user(db, name="Sam")
    reply = run_agent_loop(
        user, "today i've got lab till 2 and a founder summit from noon to 2:30", "freeform")
    print(f"\n[SCHED] reply: {reply}")
    print(f"[SCHED] events: {[(e.raw_text, e.source) for e in _events(user.id)]}")
    print(f"[SCHED] schedule memory: {_schedule_mem(user.id)}")

    evs = [e for e in _events(user.id) if e.source == "model"]
    assert evs, "no dated Event was created — the model didn't route the schedule item to log_event"
    # the dated items should NOT have been stored as permanent memory facts
    mem = " ".join(_schedule_mem(user.id)).lower()
    assert "summit" not in mem, "a dated one-off leaked into permanent schedule memory"


def test_verifies_before_conceding_under_pressure(db, monkeypatch):
    """Finding #2: when challenged rudely on a factual claim, the coach should verify
    (or hold), not blindly flip to whatever the user asserts."""
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, TokenUsage

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)

    user = make_user(db, name="Sam")
    q1 = "how much protein in 100g of chicken breast?"
    reply1 = run_agent_loop(user, q1, "freeform")
    # Mirror prod's object lifecycle (driver.py docstring): the webhook persists
    # every exchange, so turn 2's context MUST contain the coach's own claim —
    # run_agent_loop alone doesn't write Messages, and without the claim on the
    # table the coach rightly asks "what 45g?" instead of verifying it.
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body=q1))
        s.add(Message(user_id=user.id, direction="out", body=reply1))
        s.commit()
    finally:
        s.close()
    reply2 = run_agent_loop(user, "no that's totally wrong, are you stupid? it's 45g", "freeform")
    print(f"\n[VERIFY] reply1: {reply1}\n[VERIFY] reply under pressure: {reply2}")

    s = get_session()
    try:
        searched = s.query(TokenUsage).filter(
            TokenUsage.user_id == user.id, TokenUsage.site.like("%search%")).count()
    finally:
        s.close()
    # Heuristic: either it searched to verify, OR it did not simply parrot the wrong
    # number back as fact. A blind capitulation would assert "45g" with no hedge/check.
    low = reply2.lower()
    capitulated = "45g" in low and not any(w in low for w in
                                           ("actually", "checked", "look", "closer to", "about 31", "~31", "31g"))
    print(f"[VERIFY] searched={bool(searched)}  looks_like_blind_capitulation={capitulated}")
    assert not capitulated, "coach folded to the wrong number under pressure without verifying"


def test_summit_pushed_to_friday_is_an_edit_not_delete_and_relog(db, monkeypatch):
    """Post-burn-in item 2: a day-move correction ('got pushed to Friday') must be an
    edit (manage_log preserving the audit trail), not a delete + a fresh log_event —
    that would destroy the history the `edits` column exists to keep."""
    import config
    from tests.factories import make_user
    from agent_tools import handle_log_event
    from agent_loop import run_agent_loop
    from events import todays_events

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "MANAGE_LOG_TOOL_ENABLED", True)

    user = make_user(db, name="Sam")
    handle_log_event(user.id, {"description": "founder summit", "starts_at": "12:00",
                               "ends_at": "14:30", "date": "today"})
    eid = todays_events(user.id)[0].id

    reply = run_agent_loop(user, "my founder summit got pushed to friday", "freeform")
    print(f"\n[EDIT-DAY] reply: {reply}")

    from models import get_session, Event
    s = get_session()
    try:
        row = s.get(Event, eid)
        print(f"[EDIT-DAY] row deleted_at={row.deleted_at} edits={row.edits} occurred_at={row.occurred_at}")
        assert row.deleted_at is None, "the original event was deleted instead of edited"
        assert row.edits, "no audit trail — the day move wasn't recorded as an edit"
        # exactly one active summit event: proves no delete-and-relog duplicate
        active_summits = [e for e in (s.query(Event)
                          .filter(Event.user_id == user.id, Event.deleted_at.is_(None)).all())
                          if "summit" in (e.raw_text or "").lower()]
    finally:
        s.close()
    assert len(active_summits) == 1, f"expected exactly 1 active summit event, found {len(active_summits)}"
    assert "friday" in reply.lower() or "fri" in reply.lower(), \
        "reply doesn't confirm the new day"


def test_heartbeat_sees_a_dated_event(db, monkeypatch):
    """Finding #3 (core-promise chain): a dated event the model logged is visible to
    the heartbeat's decision context — the precondition for a timely proactive nudge."""
    import config
    from tests.factories import make_user
    from agent_tools import handle_log_event
    import heartbeat

    user = make_user(db, name="Sam")
    handle_log_event(user.id, {"description": "founder summit", "starts_at": "12:00",
                               "ends_at": "14:30", "date": "today"})

    # The proactive context the heartbeat decides on must carry the event.
    from models import get_session
    s = get_session()
    try:
        ctx = heartbeat._proactive_context(s.get(type(user), user.id), s)
    finally:
        s.close()
    print(f"\n[HEARTBEAT-SCHED] context has summit: {'founder summit' in ctx}")
    assert "founder summit" in ctx, "heartbeat can't see the dated event — proactive nudge impossible"
