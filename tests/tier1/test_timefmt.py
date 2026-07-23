"""
Burn-in item 1 — user-local time rendering. Stored timestamps are naive UTC; the model
must see them in the user's zone plus an explicit local "now" anchor, or it mis-resolves
relative time ("tomorrow/later"), worst after 5pm PT where UTC is already the next day.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


class _U:
    def __init__(self, tz="America/Los_Angeles", uid=1):
        self.user_timezone = tz
        self.id = uid


def _naive_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_naive_utc_renders_local_and_belongs_to_local_day():
    from timefmt import render_time, local_day_bounds, to_local

    u = _U()
    dt = datetime(2026, 7, 22, 4, 51)  # naive UTC == 9:51 PM PDT on Jul 21
    assert render_time(dt, u, relative=False) == "9:51 PM PDT"
    assert to_local(dt, u).date().isoformat() == "2026-07-21"
    start, end = local_day_bounds(u, now=datetime(2026, 7, 22, 4, 51, tzinfo=timezone.utc))
    assert start <= dt < end, "instant must fall in its own local-day window"


def test_now_anchor_names_local_date_weekday_tz():
    from timefmt import now_anchor

    s = now_anchor(_U(), now=datetime(2026, 7, 21, 21, 11, tzinfo=timezone.utc))
    assert s == "Right now: Tuesday, July 21, 2026, 2:11 PM PDT (America/Los_Angeles)"


def test_dst_boundary_renders_pdt_then_pst():
    from timefmt import render_time

    u = _U()
    assert render_time(datetime(2026, 10, 15, 20, 0), u, relative=False).endswith("PDT")
    assert render_time(datetime(2026, 12, 15, 20, 0), u, relative=False).endswith("PST")


def test_non_default_timezone_is_three_hours_later():
    from timefmt import render_time

    dt = datetime(2026, 7, 21, 19, 0)  # naive UTC
    assert render_time(dt, _U("America/Los_Angeles"), relative=False) == "12:00 PM PDT"
    assert render_time(dt, _U("America/New_York"), relative=False) == "3:00 PM EDT"


def test_default_tz_used_and_logged_when_null():
    import logging
    from timefmt import resolve_tz

    logger = logging.getLogger("cued.timefmt")
    records = []
    handler = logging.Handler()
    handler.emit = lambda r: records.append(r.getMessage())
    logger.addHandler(handler)
    try:
        tz = resolve_tz(_U(tz=None))
    finally:
        logger.removeHandler(handler)
    assert tz.key == "America/Los_Angeles"
    assert any("TIMEFMT_DEFAULT_TZ" in m for m in records)


def test_log_event_local_time_round_trips(db):
    """A local time given to log_event stores a value that renders back to the same
    local time (the write side already converts; this pins the round-trip)."""
    from tests.factories import make_user
    from agent_tools import _parse_local_dt
    from timefmt import render_time

    user = make_user(db)  # America/Los_Angeles
    stored = _parse_local_dt("America/Los_Angeles", "today", "09:00")  # naive UTC
    assert render_time(stored, user, relative=False).startswith("9:00 AM")


def test_assembled_context_has_no_bare_utc_timestamp(db, monkeypatch):
    """Guard: a future reader that forgets timefmt would leak a '14:51Z'-style stamp."""
    import config
    from tests.factories import make_user
    from agent_loop import build_loop_context
    from events import record_event
    from models import get_session, Meal, Workout, User

    monkeypatch.setattr(config, "CONTEXT_LOCAL_TIME_ENABLED", True)
    user = make_user(db, calorie_target=2000, protein_target=150)
    s = get_session()
    try:
        s.add(Meal(user_id=user.id, description="eggs", calories=300, protein_g=20,
                   eaten_at=_naive_utcnow(), source="text", log_type="user_reported"))
        s.add(Workout(user_id=user.id, workout_type="push", completed=True))
        s.commit()
    finally:
        s.close()
    record_event(user.id, "scheduled", ends_at=None, source="model",
                 raw_text="founder summit", occurred_at=_naive_utcnow())

    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()

    assert "founder summit" in ctx and "TODAY'S TOTALS" in ctx  # render sites fired
    assert not re.search(r"\d{2}:\d{2}\s*Z", ctx), f"bare UTC timestamp leaked:\n{ctx}"
    assert "PDT" in ctx or "PST" in ctx, "expected a local tz label in context"
