"""An outright schedule question gets the rundown built in CODE before the model turn.

Live 2026-10-06 05:44 PT (user 48): "Send me my week" → "deadlines - wed 4pm cs70 hw - fri
4pm cs70 quiz", while the events table held ten classes/meetings that week. The
schedule_rundown tool existed; the model didn't use it. Now the question itself triggers
the build and the block rides the context (the tool stays for everything else)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize("text, asked", [
    ("What's my week look like", True),
    ("Send me my week", True),
    ("rest of the week?", True),
    ("what do i have friday", True),
    ("whats due", True),
    ("anything due this week", True),
    ("what's on my calendar", True),
    ("what's the schedule", True),
    ("next week is gonna be rough", True),
    ("ate a burger", False),
    ("what's a good push day", False),
    ("weekend was good", False),
    ("what time is it", False),
    ("", False),
])
def test_week_ask_regex(text, asked):
    from agent_loop import _week_asked
    assert _week_asked(text) is asked


def _connected_gcal(uid):
    from models import get_session, Integration
    s = get_session()
    try:
        s.add(Integration(user_id=uid, provider="gcal", status="connected", meta={}))
        s.commit()
    finally:
        s.close()


def _event(uid, title, start_local, hours=1, *, source="gcal", all_day=False):
    from events import upsert_external_event
    st = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    en = (start_local + timedelta(hours=hours)).astimezone(timezone.utc).replace(tzinfo=None)
    upsert_external_event(uid, source=source, external_id=title, title=title, occurred_at=st,
                          ends_at=None if all_day else en, all_day=all_day)


def _seen_text(calls) -> str:
    out = []
    for c in calls:
        sysm = c.get("system")
        out.append(sysm if isinstance(sysm, str) else "".join(b.get("text", "") for b in (sysm or [])))
        for m in c.get("messages") or []:
            out.append(str(m.get("content")))
    return "\n".join(out)


def test_week_question_carries_the_full_rundown_in_context(db, anthropic_stub, monkeypatch):
    import config, agent_loop
    monkeypatch.setattr(config, "GCAL_ENABLED", True)
    u = make_user(db)
    _connected_gcal(u.id)
    now = datetime.now(PT)
    tomorrow = (now + timedelta(days=1)).replace(hour=11, minute=0, second=0, microsecond=0)
    _event(u.id, "CS 70 Lecture", tomorrow)
    _event(u.id, "ENGIN 183 Seminar", tomorrow.replace(hour=18), 2)
    _event(u.id, "due: Homework 4 [CS70]", tomorrow.replace(hour=16), source="bcourses")
    anthropic_stub.reply_with(lambda kw: "here's ur week")
    agent_loop.run_agent_loop(u, "What's my week look like", "freeform")
    seen = _seen_text(anthropic_stub.calls)
    assert "## SCHEDULE RUNDOWN (built in code for THIS question" in seen
    assert "CS 70 Lecture" in seen and "ENGIN 183 Seminar" in seen and "Homework 4" in seen
    assert "do NOT trim it to deadlines" in seen


def test_non_schedule_turns_and_flag_off_add_nothing(db, anthropic_stub, monkeypatch):
    import config, agent_loop
    u = make_user(db)
    anthropic_stub.reply_with(lambda kw: "nice")
    agent_loop.run_agent_loop(u, "ate a burger", "freeform")
    assert "## SCHEDULE RUNDOWN" not in _seen_text(anthropic_stub.calls)
    anthropic_stub.calls.clear()
    monkeypatch.setattr(config, "WEEK_ASK_RUNDOWN_IN_CONTEXT_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: "nice")
    agent_loop.run_agent_loop(u, "what's my week", "freeform")
    assert "## SCHEDULE RUNDOWN" not in _seen_text(anthropic_stub.calls)


def test_block_is_honest_when_nothing_is_connected(db, anthropic_stub):
    import agent_loop
    u = make_user(db)
    anthropic_stub.reply_with(lambda kw: "can't see it")
    agent_loop.run_agent_loop(u, "send me my week", "freeform")
    seen = _seen_text(anthropic_stub.calls)
    assert "## SCHEDULE RUNDOWN" in seen and "calendar isn't connected yet" in seen
