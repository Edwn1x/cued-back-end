"""A deliverable they name must not vanish (live 2026-10-09, user 48: "finish project 2b for
61c" at 2pm → nothing written; at 5:49pm "nothing left today tho right?" → "I literally told
you earlier"; even "I need to get it done today" wrote nothing)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.factories import make_user


@pytest.mark.parametrize("text, what", [
    ("And then finish project 2b for 61c", "project 2b for 61c"),
    ("No bro I haven't even started on 2b", "on 2b"),
    ("pset 4 due friday", "pset 4"),
    ("gotta submit my lab report tonight", "my lab report tonight"),
    ("I have to finish my cs project", "my cs project"),
    ("ate a burger", None),
    ("gym can wait", None),
    ("what's my week", None),
])
def test_deliverable_mentions(text, what):
    from agent_loop import _deliverable_mention
    assert _deliverable_mention(text) == what


def _ev(uid, title, start_utc_naive, source="bcourses"):
    from events import upsert_external_event
    upsert_external_event(uid, source=source, external_id=title, title=title, occurred_at=start_utc_naive,
                          ends_at=start_utc_naive + timedelta(hours=1), all_day=False)


def test_unmatched_deliverable_gets_the_log_it_block_and_a_turn_note(db):
    import agent_loop
    from agent_tools import begin_turn
    from models import get_session, User
    u = make_user(db)
    _ev(u.id, "CS 61C Lecture", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=20), source="gcal")
    s = get_session()
    try:
        row = s.get(User, u.id)
        begin_turn(u.id)
        blk = agent_loop._deliverable_block(row, "And then finish project 2b for 61c", s)
        assert blk.startswith('## DELIVERABLE NOT ON THE CALENDAR: "project 2b for 61c"')
        assert "They didn't say when" in blk and "when's it due?" in blk
        assert agent_loop._DELIVERABLE_NOTES[u.id] == {"what": "project 2b for 61c", "when_said": False}
        begin_turn(u.id)
        blk2 = agent_loop._deliverable_block(row, "gotta submit my lab report tonight", s)
        assert "They said WHEN: call log_event NOW" in blk2
        assert agent_loop._DELIVERABLE_NOTES[u.id]["when_said"] is True
    finally:
        s.close()


def test_matched_deliverable_points_at_the_calendar_item(db):
    import agent_loop
    from agent_tools import begin_turn
    from models import get_session, User
    u = make_user(db)
    _ev(u.id, "due: Project 2B: CS61C", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=2))
    s = get_session()
    try:
        row = s.get(User, u.id)
        begin_turn(u.id)
        blk = agent_loop._deliverable_block(row, "haven't started on 2b", s)
        assert blk.startswith('## DELIVERABLE THEY MENTIONED: "on 2b" — it IS on their calendar') and "Project 2B" in blk
        assert agent_loop._DELIVERABLE_NOTES[u.id] is None
        # a generic noun alone never "matches" a calendar item that merely contains it
        _ev(u.id, "CS 61C Lecture", datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=5), source="gcal")
        begin_turn(u.id)
        blk2 = agent_loop._deliverable_block(row, "need to finish my cs project", s)
        assert blk2.startswith('## DELIVERABLE THEY MENTIONED: "my cs project" — POSSIBLY this calendar item') and "Project 2B" in blk2
        assert "Lecture" not in blk2 and agent_loop._DELIVERABLE_NOTES[u.id] is None
    finally:
        s.close()


def test_block_is_silent_without_a_deliverable_or_with_the_flag_off(db, monkeypatch):
    import agent_loop, config
    from models import get_session, User
    u = make_user(db)
    s = get_session()
    try:
        row = s.get(User, u.id)
        assert agent_loop._deliverable_block(row, "ate a burger", s) is None
        monkeypatch.setattr(config, "DELIVERABLE_CAPTURE_ENABLED", False)
        assert agent_loop._deliverable_block(row, "finish project 2b tonight", s) is None
    finally:
        s.close()


def _seen(calls):
    out = []
    for c in calls:
        sysm = c.get("system")
        out.append(sysm if isinstance(sysm, str) else "".join(b.get("text", "") for b in (sysm or [])))
        out.extend(str(m.get("content")) for m in c["messages"])
    return "\n".join(out)


def test_nudge_forces_log_event_when_they_said_when_and_the_reply_skipped_it(db, anthropic_stub, monkeypatch):
    import agent_loop, config
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)
    u = make_user(db)
    seen = []

    def _h(kw):
        seen.append(kw["messages"][-1]["content"])
        return "head down then" if len(seen) == 1 else "logged it, head down"
    anthropic_stub.reply_with(_h)
    out = agent_loop.run_agent_loop(u, "gotta submit my lab report tonight", "freeform")
    assert out == "logged it, head down" and len(seen) == 2
    assert "Call log_event NOW" in str(seen[1]) and "lab report" in str(seen[1])
    assert "## DELIVERABLE NOT ON THE CALENDAR" in _seen(anthropic_stub.calls)


def test_no_nudge_without_a_day_or_after_a_write(db, anthropic_stub, monkeypatch):
    import agent_loop, config
    from tests._fake_anthropic import ToolUse
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)
    u = make_user(db)
    anthropic_stub.reply_with(lambda kw: "when's it due?")
    assert agent_loop.run_agent_loop(u, "And then finish project 2b for 61c", "freeform") == "when's it due?"
    assert len(anthropic_stub.calls) == 1
    anthropic_stub.calls.clear()
    calls = []

    def _h(kw):
        calls.append(1)
        return ToolUse("log_event", {"description": "cs61c lab report", "date": "today"}) if len(calls) == 1 else "logged it"
    anthropic_stub.reply_with(_h)
    assert agent_loop.run_agent_loop(u, "gotta submit my lab report tonight", "freeform") == "logged it"
    assert len(calls) == 2                                   # tool + reply, no nudge turn



def test_the_write_is_a_real_row_on_cueds_radar_not_a_claim(db, anthropic_stub, monkeypatch):
    """The founder asked: is it actually adding it, or just saying so? The nudge makes the model
    call log_event; that call writes an Event (source='model') that the briefing / week rundown /
    deadline radar read. It is NOT a Google Calendar write — the block says so."""
    import agent_loop, config
    from tests._fake_anthropic import ToolUse
    from models import get_session, Event
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)
    u = make_user(db)
    calls = []

    def _h(kw):
        calls.append(kw)
        if len(calls) == 1:
            return "head down then"                                  # skipped the write → nudge
        if len(calls) == 2:
            return ToolUse("log_event", {"description": "cs61c lab report", "date": "today"})
        return "my bad — lab report due today, it's on my radar now"
    anthropic_stub.reply_with(_h)
    out = agent_loop.run_agent_loop(u, "gotta submit my lab report tonight", "freeform")
    assert out == "my bad — lab report due today, it's on my radar now"
    s = get_session()
    try:
        rows = s.query(Event).filter(Event.user_id == u.id, Event.source == "model").all()
        assert len(rows) == 1 and "cs61c lab report" in (rows[0].raw_text or "")
        today = datetime.now(timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).date()
        assert rows[0].occurred_at.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).date() == today
    finally:
        s.close()
    seen = _seen(anthropic_stub.calls)
    assert "NOT their Google Calendar" in seen and "never \"it's on ur calendar\"" in seen
