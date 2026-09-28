"""
Deterministic schedule rundown (schedule.build_rundown + schedule_rundown tool) — tier-1.

The 2026-09-28 bug (user 31): asked "rest of the week", the coach listed Tue/Wed/Thu,
said "that's the week", and DROPPED Friday — including a real "due: Homework 4: RISC-V
[CS61C]" deadline — plus the weekend. The data was all in context; the MODEL truncated
the rundown. These tests pin completeness in CODE: every deadline is enumerated and can
never be dropped, the full window is grouped by the user's LOCAL day, duplicate calendar
copies collapse, and empty windows are honest. The wall clock is frozen via `now=`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")

# 2026-10-05 is a Monday — a clean start-of-week anchor.
NOW_LOCAL = datetime(2026, 10, 5, 9, 0, tzinfo=PT)
NOW = NOW_LOCAL.astimezone(timezone.utc)


def _naive(dt_aware) -> datetime:
    return dt_aware.astimezone(timezone.utc).replace(tzinfo=None)


def _local(h, mi=0, *, day=5):
    return datetime(2026, 10, day, h, mi, tzinfo=PT)


def _mk_timed(user_id, title, start_aware, end_aware, *, source="gcal", ext=None):
    from events import upsert_external_event
    upsert_external_event(user_id, source=source, external_id=ext or title, title=title,
                          occurred_at=_naive(start_aware), ends_at=_naive(end_aware), all_day=False)


def _mk_deadline(user_id, title, when_aware, *, source="bcourses", all_day=False, ext=None):
    from events import upsert_external_event
    upsert_external_event(user_id, source=source, external_id=ext or title, title=title,
                          occurred_at=_naive(when_aware), all_day=all_day)


@pytest.fixture
def rundown_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "SCHEDULE_RUNDOWN_ENABLED", True)


# ─── flag default ───────────────────────────────────────────────────────────────

def test_flag_default_on():
    import config
    assert config.SCHEDULE_RUNDOWN_ENABLED is True


def test_inert_when_flag_off(db, monkeypatch):
    import config
    from schedule import build_rundown
    monkeypatch.setattr(config, "SCHEDULE_RUNDOWN_ENABLED", False)
    user = make_user(db)
    _mk_deadline(user.id, "due: HW4 RISC-V [CS61C]", NOW + timedelta(days=4), ext="hw4")
    assert build_rundown(user.id, "rest of the week", now=NOW) == ""


# ─── the core bug: the week must never drop the Friday deadline ─────────────────

def test_rest_of_week_keeps_friday_deadline_and_weekend(db, rundown_on):
    from schedule import build_rundown
    user = make_user(db)
    # Mon–Wed lectures (the days a naive summary would list) ...
    _mk_timed(user.id, "CS61C lecture", _local(10), _local(11), ext="lec-mon", source="gcal")
    _mk_timed(user.id, "seminar", _local(14, day=6), _local(15, day=6), ext="sem-tue")
    _mk_timed(user.id, "lab", _local(13, day=7), _local(15, day=7), ext="lab-wed")
    # ... the Friday deadline that got dropped ...
    _mk_deadline(user.id, "due: Homework 4: RISC-V [CS61C]", _local(23, 59, day=9), ext="hw4")
    # ... and a weekend event that also got dropped.
    _mk_timed(user.id, "intramural game", _local(12, day=11), _local(14, day=11), ext="game-sun")

    out = build_rundown(user.id, "rest of the week", now=NOW)
    # the Friday deadline is present AND in the guaranteed deadlines section
    assert "deadlines:" in out
    assert "Homework 4: RISC-V [CS61C]" in out
    assert "Fri Oct 9" in out
    # the weekend event survived too — the tail was not truncated
    assert "intramural game" in out and "Sun Oct 11" in out


def test_deadlines_never_dropped_even_with_a_huge_week(db, rundown_on, monkeypatch):
    import config
    from schedule import build_rundown
    monkeypatch.setattr(config, "SCHEDULE_RUNDOWN_MAX_EVENTS", 10)
    user = make_user(db)
    # 30 distinct one-off events (distinct titles so they don't collapse as recurring)
    for i in range(30):
        _mk_timed(user.id, f"meeting {i}", _local(8, day=7) + timedelta(minutes=i),
                  _local(8, day=7) + timedelta(minutes=i + 20), ext=f"m{i}")
    # deadlines scattered through the window, including one on the LAST day
    _mk_deadline(user.id, "due: Pset 3 [Math54]", _local(23, 59, day=6), ext="p3")
    _mk_deadline(user.id, "CS61C Midterm 1", _local(19, day=8), source="gcal", ext="mt")
    _mk_deadline(user.id, "due: Essay 2 [R1B]", _local(23, 59, day=11), ext="e2")

    out = build_rundown(user.id, "this week", now=NOW)
    # every deadline enumerated in full despite the routine cap
    assert "Pset 3 [Math54]" in out
    assert "CS61C Midterm 1" in out
    assert "Essay 2 [R1B]" in out
    # routine events were capped, not the deadlines
    assert "more)" in out


# ─── dedup across calendars ────────────────────────────────────────────────────

def test_dedup_collapses_duplicate_events(db, rundown_on):
    from schedule import build_rundown, collect_rundown
    user = make_user(db)
    # two gcal copies of the same dinner (same local start + title, different external id)
    _mk_timed(user.id, "All Scholar Dinner", _local(18, day=7), _local(20, day=7),
              ext="dinner-a", source="gcal")
    _mk_timed(user.id, "All Scholar Dinner", _local(18, day=7), _local(20, day=7),
              ext="dinner-b", source="gcal")
    lo = _naive(_local(0, day=5))
    hi = _naive(_local(0, day=12))
    evs = collect_rundown(user.id, lo=lo, hi=hi, now=NOW)
    assert sum(1 for e in evs if e.title == "All Scholar Dinner") == 1
    out = build_rundown(user.id, "this week", now=NOW)
    assert out.count("All Scholar Dinner") == 1


# ─── all-day deadline renders as a day, not a bogus clock time ──────────────────

def test_all_day_deadline_renders_as_due_day(db, rundown_on):
    from schedule import build_rundown
    user = make_user(db)
    _mk_deadline(user.id, "due: HW4 RISC-V [CS61C]", _local(0, day=9), all_day=True, ext="hw4")
    out = build_rundown(user.id, "this week", now=NOW)
    # find the deadline line and confirm it carries a day but no clock time
    line = next(ln for ln in out.splitlines() if "HW4 RISC-V" in ln)
    assert "Fri Oct 9" in line
    # no bogus clock time (a due:-prefixed title still has a colon, so match a real time)
    import re
    assert not re.search(r"\d{1,2}:\d{2}", line)
    assert "AM" not in line and "PM" not in line


# ─── empty / not-connected honesty ─────────────────────────────────────────────

def test_empty_window_when_connected_is_honest(db, rundown_on):
    from schedule import build_rundown
    from models import Integration
    user = make_user(db)
    db.add(Integration(user_id=user.id, provider="gcal", status="connected", meta={}))
    db.commit()
    out = build_rundown(user.id, "this week", now=NOW)
    assert "nothing on your calendar for this week" in out


def test_empty_window_when_not_connected_offers_link(db, rundown_on):
    from schedule import build_rundown
    user = make_user(db)
    out = build_rundown(user.id, "this week", now=NOW)
    assert "isn't connected" in out and "link" in out


def test_whats_due_only_lists_deadlines(db, rundown_on):
    from schedule import build_rundown
    user = make_user(db)
    _mk_timed(user.id, "CS61C lecture", _local(10, day=6), _local(11, day=6), ext="lec")
    _mk_deadline(user.id, "due: HW4 RISC-V", _local(23, 59, day=9), ext="hw4")
    out = build_rundown(user.id, "what's due", now=NOW)
    assert "deadlines:" in out and "HW4 RISC-V" in out
    # a plain lecture is not a deadline — the deadlines-only view omits it
    assert "CS61C lecture" not in out


# ─── local-day grouping across a UTC day boundary ──────────────────────────────

def test_local_day_grouping_across_utc_boundary(db, rundown_on):
    from schedule import build_rundown
    user = make_user(db)
    # 11pm PT Tuesday = 6am UTC Wednesday — must group under Tue (local), not Wed.
    _mk_timed(user.id, "late study block", _local(23, day=6), _local(23, 59, day=6), ext="late")
    out = build_rundown(user.id, "this week", now=NOW)
    assert "Tue Oct 6" in out
    assert "Wed Oct 7" not in out
    # the event sits under the Tuesday header
    lines = out.splitlines()
    tue_i = next(i for i, ln in enumerate(lines) if ln.startswith("Tue Oct 6"))
    assert any("late study block" in ln for ln in lines[tue_i:tue_i + 3])


def test_recurring_class_collapses(db, rundown_on):
    from schedule import build_rundown
    user = make_user(db)
    # same class Mon/Wed/Fri at 11 — one recurrence line, not three day entries
    for d in (5, 7, 9):
        _mk_timed(user.id, "CS61C lecture", _local(11, day=d), _local(12, day=d),
                  ext=f"lec-{d}", source="gcal")
    out = build_rundown(user.id, "this week", now=NOW)
    assert "regular:" in out
    line = next(ln for ln in out.splitlines() if "CS61C lecture" in ln)
    assert "Mon" in line and "Wed" in line and "Fri" in line
    assert out.count("CS61C lecture") == 1


# ─── tool wiring ────────────────────────────────────────────────────────────────

def test_tool_handler_relays_rundown(db, rundown_on):
    # the tool runs against the real clock (no `now=`), so anchor the fixture to it.
    from agent_tools import handle_schedule_rundown
    real_now = datetime.now(timezone.utc)
    user = make_user(db)
    _mk_deadline(user.id, "due: HW4 RISC-V", real_now + timedelta(days=3), ext="hw4")
    out = handle_schedule_rundown(user.id, {"window": "what's due", "days": 14})
    assert out.startswith("ok: relay this rundown")
    assert "HW4 RISC-V" in out


def test_tool_registered_and_assembled():
    from agent_tools import _HANDLERS, SCHEDULE_RUNDOWN_TOOL
    assert "schedule_rundown" in _HANDLERS
    assert SCHEDULE_RUNDOWN_TOOL["name"] == "schedule_rundown"
