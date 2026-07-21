"""
Burn-in fix — log_event. Dated, day-scoped schedule items (calendar screenshots,
"lab till 2 today") must persist as EVENTS — dated, auto-expiring, local-day windowed
— not as semantic memory facts that the per-category soft cap evicts. This tool is the
agent's write path to Events (the `record_event(source="model")` the spec anticipated
but nothing ever called), and the context render surfaces them for the loop + heartbeat.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _todays_events(user_id):
    from events import todays_events
    return todays_events(user_id)


def test_handle_log_event_creates_dated_model_event(db):
    from tests.factories import make_user
    from agent_tools import handle_log_event

    user = make_user(db, user_timezone="America/Los_Angeles")
    out = handle_log_event(user.id, {
        "description": "founder summit", "starts_at": "12:00", "ends_at": "14:30",
        "date": "today",
    })
    assert out.startswith("ok"), out

    evs = _todays_events(user.id)
    assert len(evs) == 1
    e = evs[0]
    assert e.event_type == "scheduled" and e.source == "model"
    assert e.raw_text == "founder summit"
    assert e.occurred_at is not None and e.ends_at is not None
    # end after start
    assert e.ends_at > e.occurred_at


def test_log_event_requires_description(db):
    from tests.factories import make_user
    from agent_tools import handle_log_event

    user = make_user(db)
    out = handle_log_event(user.id, {"starts_at": "09:00"})
    assert out.startswith("error") and "description" in out
    assert _todays_events(user.id) == []


def test_log_event_without_time_still_lands_today(db):
    from tests.factories import make_user
    from agent_tools import handle_log_event

    user = make_user(db)
    out = handle_log_event(user.id, {"description": "lab all afternoon"})
    assert out.startswith("ok"), out
    evs = _todays_events(user.id)
    assert len(evs) == 1 and evs[0].raw_text == "lab all afternoon"


def test_log_event_surfaces_in_context_with_description(db):
    """The render must show WHAT it is + the window, not just an event_type label —
    otherwise the heartbeat can't say 'summit at noon'."""
    from tests.factories import make_user
    from agent_tools import handle_log_event
    from agent_loop import build_loop_context
    from models import get_session, User

    user = make_user(db)
    handle_log_event(user.id, {"description": "orgo midterm", "starts_at": "09:00", "date": "today"})

    s = get_session()
    try:
        u = s.get(User, user.id)
        ctx = build_loop_context(u, s)
    finally:
        s.close()
    assert "TODAY'S EVENTS" in ctx and "orgo midterm" in ctx


def test_log_event_via_loop_persists(db, driver, monkeypatch, anthropic_stub):
    """End-to-end: the model requests log_event → code writes the Event → a reply is sent."""
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)

    calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"  # classifier / extraction (no tools)
        calls.append(1)
        if len(calls) == 1:
            return ToolUse("log_event", {"description": "61A lecture", "starts_at": "17:00",
                                         "ends_at": "19:00", "date": "today"})
        return "noted — 61A lecture 5 to 7."

    anthropic_stub.reply_with(handler)

    user = make_user(db)
    replies = driver.send(user, "here's my schedule for today [screenshot]")

    evs = _todays_events(user.id)
    assert any(e.raw_text == "61A lecture" and e.source == "model" for e in evs), \
        "log_event tool call did not persist the dated event"
    assert any("61a" in r.lower() for r in replies)


def test_handle_log_event_batch_creates_all(db):
    """A calendar screenshot with several items → one call, one Event each."""
    from tests.factories import make_user
    from agent_tools import handle_log_event

    user = make_user(db)
    out = handle_log_event(user.id, {"events": [
        {"description": "lab", "ends_at": "14:00"},
        {"description": "founder summit", "starts_at": "12:00", "ends_at": "14:30"},
        {"description": "61A lecture", "starts_at": "17:00", "ends_at": "19:00"},
    ]})
    assert out.startswith("ok") and "3 events" in out, out
    evs = _todays_events(user.id)
    assert {e.raw_text for e in evs} >= {"lab", "founder summit", "61A lecture"}
    assert all(e.source == "model" and e.event_type == "scheduled" for e in evs)
