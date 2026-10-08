"""WHICH CLASS? — an academic task mentioned without a course gets this week's classes and
the code's own match (live 2026-10-08, user 48: "readings for the weekly warmup quiz" +
"discussion today" → the coach pinned the readings to the CS70 Friday quiz; they were for
the Data C104 discussion sitting on the calendar at 12:30 the same day)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")
THU = datetime(2026, 10, 8, 5, 3, tzinfo=PT)          # the live moment, Thursday 05:03


def _seed(uid):
    from events import upsert_external_event

    def ev(title, start, hours=1.5, ext=None):
        st = start.astimezone(timezone.utc).replace(tzinfo=None)
        en = (start + timedelta(hours=hours)).astimezone(timezone.utc).replace(tzinfo=None)
        upsert_external_event(uid, source="gcal", external_id=ext or title + str(start), title=title,
                              occurred_at=st, ends_at=en, all_day=False)
    ev("data 104c Discussion 119", THU.replace(hour=12, minute=30))
    ev("CS 70 Lecture", THU.replace(hour=15, minute=30))
    ev("ENGIN 183 Seminar", THU.replace(hour=18, minute=0), 2)
    ev("cs70 Discussion (friday = quiz)", (THU + timedelta(days=1)).replace(hour=16, minute=0), 1)
    ev("CS 61C Lecture", (THU + timedelta(days=1)).replace(hour=11, minute=0), 1)


@pytest.mark.parametrize("text, applies", [
    ("I lwk need to study and do the readings for the weekly warmup quiz", True),
    ("i have a pset due and no idea when", True),
    ("readings for cs70 are brutal", False),             # course named
    ("data c104 quiz tmrw", False),
    ("ate a burger", False),
    ("what's my week", False),
])
def test_block_applies_only_to_a_task_without_a_course(db, text, applies):
    import agent_loop
    from models import get_session, User
    u = make_user(db)
    _seed(u.id)
    s = get_session()
    try:
        blk = agent_loop._which_class_block(s.get(User, u.id), text, s, now=THU.astimezone(timezone.utc))
        assert (blk is not None) is applies, blk
    finally:
        s.close()


def test_discussion_today_is_matched_to_the_one_discussion_on_the_calendar(db):
    import agent_loop
    from models import get_session, User
    u = make_user(db)
    _seed(u.id)
    s = get_session()
    try:
        row = s.get(User, u.id)
        text = "I lwk need to study and do the readings for the weekly warmup quiz\nI think I have discussion today no?"
        blk = agent_loop._which_class_block(row, text, s, now=THU.astimezone(timezone.utc))
        assert blk.startswith("## WHICH CLASS?")
        assert "Code match: they pointed at a discussion today" in blk and "data 104c Discussion 119 (Thu 12:30 PM–2:00 PM)" in blk
        assert "Never pair the task with a DIFFERENT course" in blk
        assert "Thu (today) 12:30 PM–2:00 PM — data 104c Discussion 119" in blk and "Fri 4:00 PM–5:00 PM — cs70 Discussion (friday = quiz)" in blk
        # no kind named → no match claimed, but the classes are still listed
        blk2 = agent_loop._which_class_block(row, "gotta do the warmup readings", s, now=THU.astimezone(timezone.utc))
        assert "No course named and nothing they said pins it" in blk2 and "CS 70 Lecture" in blk2
    finally:
        s.close()


def test_no_classes_on_the_calendar_or_flag_off_is_silent(db, monkeypatch):
    import agent_loop, config
    from models import get_session, User
    u = make_user(db)
    s = get_session()
    try:
        row = s.get(User, u.id)
        assert agent_loop._which_class_block(row, "readings for the warmup quiz", s, now=THU.astimezone(timezone.utc)) is None
        _seed(u.id)
        monkeypatch.setattr(config, "ACADEMIC_COURSE_MATCH_ENABLED", False)
        assert agent_loop._which_class_block(row, "readings for the warmup quiz", s, now=THU.astimezone(timezone.utc)) is None
    finally:
        s.close()


def test_block_rides_the_turn_context(db, anthropic_stub):
    import agent_loop
    from datetime import datetime as _dt
    u = make_user(db)
    # seed relative to the real clock so the 7-day window catches it
    from events import upsert_external_event
    now = _dt.now(timezone.utc)
    st = (now + timedelta(hours=3)).replace(tzinfo=None)
    upsert_external_event(u.id, source="gcal", external_id="d1", title="data 104c Discussion 119",
                          occurred_at=st, ends_at=st + timedelta(hours=1), all_day=False)
    anthropic_stub.reply_with(lambda kw: "ok")
    agent_loop.run_agent_loop(u, "need to do the readings for the warmup quiz before discussion", "freeform")
    seen = "\n".join(str(m.get("content")) for c in anthropic_stub.calls for m in c["messages"]) + \
           "\n".join("".join(b.get("text", "") for b in (c.get("system") or [])) if not isinstance(c.get("system"), str) else c["system"] for c in anthropic_stub.calls)
    assert "## WHICH CLASS?" in seen and "data 104c Discussion 119" in seen
