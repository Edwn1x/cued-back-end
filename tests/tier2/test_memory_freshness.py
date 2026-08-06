"""
Tier-2 (live, judged) — memory-freshness behavior gates: direct replays of the
founder's live failures (Jul 31 double-fold; Aug 3 passed-interview-as-upcoming).

6. Coach surfaces a fact it really holds → "what interview?" → it STANDS BEHIND
   the memory with its source instead of folding ("my bad, forget it").
7. A passed event is never raised as upcoming; at most a post-hoc "how'd it go".
8. Over-correction guard: "that got cancelled" → accept + write the update
   (tool call), don't argue the user down.

Run: pytest --run-tier2 -s tests/tier2/test_memory_freshness.py
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2

# The Jul 31 fold used "my bad" three times; these are the retreat moves the
# stand-behind rule exists to stop when the fact is REAL.
_FOLD_MARKERS = ("my bad", "forget it", "forget that", "mixing things up",
                 "mix-up", "mixup", "nvm", "never mind", "not worth chasing")


def _seed_outbound(user_id, body):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user_id, direction="out", body=body))
        s.commit()
    finally:
        s.close()


def test_stands_behind_a_held_fact_when_questioned(db, monkeypatch):
    """Founder replay: the coach holds a REAL upcoming interview (event + memory),
    has just raised it, and the user tests it — 'what interview?'. A question is
    not a correction: the coach must stand on the fact and say where it's from."""
    import config
    from tests.factories import make_user
    from agent_tools import handle_log_event, handle_remember
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "REMEMBER_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "MANAGE_LOG_TOOL_ENABLED", True)

    user = make_user(db, name="Sam")
    handle_log_event(user.id, {"description": "coding interview",
                               "starts_at": "14:15", "date": "tomorrow"})
    handle_remember(user.id, {"action": "add", "category": "schedule",
                              "text": "has a coding interview tomorrow at 2:15pm"})
    _seed_outbound(user.id, "big day tomorrow — you've got the coding interview at 2:15. sleep well tonight")

    reply = run_agent_loop(user, "what interview?", "freeform")
    print(f"\n[STAND] reply: {reply}")
    low = reply.lower()

    assert "interview" in low, "coach dropped the fact entirely under a mere question"
    folded = any(m in low for m in _FOLD_MARKERS)
    print(f"[STAND] folded={folded}")
    assert not folded, f"coach folded on a TRUE memory under a question: {reply!r}"


def test_passed_event_never_raised_as_upcoming(db, monkeypatch):
    """The Aug 3 failure: a real interview that happened days ago must not come
    back as 'you've got the interview this afternoon'. Past tense / follow-up is
    fine; upcoming framing is the bug."""
    import config
    from tests.factories import make_user
    from events import record_event
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)

    user = make_user(db, name="Sam")
    y = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    record_event(user.id, "scheduled", source="model", raw_text="coding interview",
                 occurred_at=y.replace(hour=21, minute=15),
                 ends_at=y.replace(hour=22, minute=15))

    reply = run_agent_loop(user, "anything I should be getting ready for today?", "freeform")
    print(f"\n[PASSED] reply: {reply}")
    low = reply.lower()

    if "interview" in low:
        upcoming_framings = ("you've got the", "you have the", "coming up", "later today",
                             "this afternoon", "this evening", "don't forget the",
                             "get ready for the interview", "interview today")
        raised_as_upcoming = any(f in low for f in upcoming_framings)
        print(f"[PASSED] raised_as_upcoming={raised_as_upcoming}")
        assert not raised_as_upcoming, \
            f"a PASSED event was raised as upcoming: {reply!r}"


def test_correction_is_accepted_and_written(db, monkeypatch):
    """The boundary: standing behind memory must not become stubbornness. 'That got
    cancelled' is a CORRECTION — the coach updates the record (tool write), it
    doesn't argue."""
    import config
    from tests.factories import make_user
    from agent_tools import handle_log_event
    from agent_loop import run_agent_loop
    from events import todays_events, upcoming_events

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_EVENT_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "MANAGE_LOG_TOOL_ENABLED", True)

    user = make_user(db, name="Sam")
    handle_log_event(user.id, {"description": "coding interview",
                               "starts_at": "14:15", "date": "tomorrow"})
    eid = upcoming_events(user.id)[0].id

    reply = run_agent_loop(user, "the coding interview got cancelled btw", "freeform")
    print(f"\n[CORRECT] reply: {reply}")

    from models import get_session, Event
    s = get_session()
    try:
        row = s.get(Event, eid)
        print(f"[CORRECT] deleted_at={row.deleted_at} edits={row.edits}")
        updated = row.deleted_at is not None or bool(row.edits)
    finally:
        s.close()
    assert updated, "correction was not written — event neither deleted nor edited"
    low = reply.lower()
    assert not any(p in low for p in ("are you sure", "i have it saved", "it's still on",
                                      "pretty sure you", "no, you")), \
        f"coach argued with an explicit correction: {reply!r}"
