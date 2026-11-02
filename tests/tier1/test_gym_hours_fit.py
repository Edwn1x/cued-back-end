"""
Live Sat 2026-10-31 (user 51): the heartbeat asked "6pm still the plan?" for a pull; the RSF
closes at 6pm on Saturdays ("they closed"); the coach then re-armed "pull day, rsf" for Sunday
6pm through create_reminder, with no hours check anywhere. Now: a gym reminder for someone who
trains at the RSF is fitted inside opening hours per occurrence (latest start = close −
GYM_SESSION_MIN_BEFORE_CLOSE_MIN), the tool result carries the note, the heartbeat / reactive
context carry today's + tomorrow's hours, and the briefing's suggested gym window is clipped.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

TZ = ZoneInfo("America/Los_Angeles")
CAMPUS = dict(user_timezone="America/Los_Angeles", equipment="full_gym", occupation="student",
              current_split="ppl", split_pointer_day="push")


def _local(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=TZ)


def _naive(dt_aware):
    return dt_aware.astimezone(timezone.utc).replace(tzinfo=None)


def _to_local(naive_utc):
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(TZ)


@pytest.fixture
def default_hours(monkeypatch):
    from integrations import rsf
    monkeypatch.setitem(rsf._state, "hours", dict(rsf.DEFAULT_HOURS))


# ─── 1. the fit itself ───────────────────────────────────────────────────────

def test_fit_session_start_pulls_a_closing_time_back_and_skips_closed_dates(default_hours):
    from integrations import rsf
    sat_6pm = _local(2026, 10, 31, 18)                       # Sat: 8am–6pm
    assert rsf.fit_session_start(sat_6pm, 90) == _local(2026, 10, 31, 16, 30)
    assert rsf.fit_session_start(_local(2026, 10, 31, 15), 90) == _local(2026, 10, 31, 15)   # fine as is
    assert rsf.fit_session_start(_local(2026, 11, 2, 6), 90) == _local(2026, 11, 2, 7)       # Mon opens 7
    assert rsf.fit_session_start(_local(2026, 11, 1, 18), 90) == _local(2026, 11, 1, 18)     # Sun closes 11pm
    assert rsf.fit_session_start(_local(2026, 12, 25, 10), 90) is None                       # closed
    assert rsf.hours_text(5) == "8am–6pm" and rsf.closing_text(5) == "6pm"
    # naive-UTC in, aware-local out
    assert rsf.fit_session_start(_naive(sat_6pm), 90) == _local(2026, 10, 31, 16, 30)


# ─── 2. reminders: model one-off, re-arm, program weekly ─────────────────────

def test_a_saturday_6pm_gym_reminder_lands_at_4_30_with_a_note(db, default_hours, monkeypatch):
    import reminders
    from models import Reminder
    fri_1730 = _local(2026, 10, 30, 17, 30)
    monkeypatch.setattr(reminders, "_naive_utcnow", lambda: _naive(fri_1730))
    u = make_user(db, **CAMPUS)
    r = reminders.create_reminder(u.id, "pull day, rsf", "18:00", date_str="saturday")
    assert "error" not in r
    assert _to_local(r["fire_at"]) == _local(2026, 10, 31, 16, 30)
    assert r["note"] == "rsf closes at 6pm sat, so it's set for 4:30pm"
    db.expire_all()
    row = db.get(Reminder, r["id"])
    assert row.local_time == "18:00"            # what they asked for stays on the row
    # the tool tells the coach to say so
    from agent_tools import handle_set_reminder
    out = handle_set_reminder(u.id, {"text": "pull day, rsf", "time": "18:00", "date": "saturday"})
    assert out.startswith("ok:") and "rsf closes at 6pm sat" in out and "tell them" in out


def test_the_rearm_for_tomorrow_fits_too_and_a_fine_time_is_untouched(db, default_hours, monkeypatch):
    import reminders
    fri_2300 = _local(2026, 10, 30, 23)
    monkeypatch.setattr(reminders, "_naive_utcnow", lambda: _naive(fri_2300))
    u = make_user(db, **CAMPUS)
    r = reminders.create_reminder(u.id, "pull day, rsf", "18:00", date_str="tomorrow", source="rearm")
    assert _to_local(r["fire_at"]) == _local(2026, 10, 31, 16, 30) and r["note"]
    r2 = reminders.create_reminder(u.id, "pull day, rsf", "15:00", date_str="tomorrow", source="rearm")
    assert _to_local(r2["fire_at"]) == _local(2026, 10, 31, 15) and "note" not in r2


def test_non_gym_text_home_gym_and_other_gym_users_keep_their_time(db, default_hours, monkeypatch):
    import reminders
    fri_1730 = _local(2026, 10, 30, 17, 30)
    monkeypatch.setattr(reminders, "_naive_utcnow", lambda: _naive(fri_1730))
    campus = make_user(db, **CAMPUS)
    r = reminders.create_reminder(campus.id, "call mom", "18:00", date_str="saturday")
    assert _to_local(r["fire_at"]) == _local(2026, 10, 31, 18) and "note" not in r
    home = make_user(db, **dict(CAMPUS, equipment="home_gym"), phone="+15550003001")
    r = reminders.create_reminder(home.id, "pull day", "18:00", date_str="saturday")
    assert _to_local(r["fire_at"]) == _local(2026, 10, 31, 18) and "note" not in r
    other = make_user(db, **dict(CAMPUS, which_gym="24 hour fitness"), phone="+15550003002")
    r = reminders.create_reminder(other.id, "gym", "18:00", date_str="saturday")
    assert _to_local(r["fire_at"]) == _local(2026, 10, 31, 18) and "note" not in r


def test_an_explicit_closed_date_is_an_error_not_a_silent_slide(db, default_hours, monkeypatch):
    import reminders
    monkeypatch.setattr(reminders, "_naive_utcnow", lambda: _naive(_local(2026, 12, 20, 10)))
    u = make_user(db, **CAMPUS)
    r = reminders.create_reminder(u.id, "legs, rsf", "10:00", date_str="2026-12-25")
    assert "error" in r and "closed" in r["error"] and "fri" in r["error"]


def test_the_flag_off_restores_the_old_behaviour(db, default_hours, monkeypatch):
    import reminders, config
    monkeypatch.setattr(config, "GYM_REMINDER_HOURS_GATE_ENABLED", False)
    monkeypatch.setattr(reminders, "_naive_utcnow", lambda: _naive(_local(2026, 10, 30, 17, 30)))
    u = make_user(db, **CAMPUS)
    r = reminders.create_reminder(u.id, "pull day, rsf", "18:00", date_str="saturday")
    assert _to_local(r["fire_at"]) == _local(2026, 10, 31, 18) and "note" not in r


def test_the_programs_weekly_gym_ping_fits_each_occurrence(db, default_hours, monkeypatch):
    """mon–sat at 6pm: Saturday's occurrence fires 4:30, the Monday one stays 6pm — and once
    Saturday 4:30 has passed the row re-arms to Monday 6pm, not Saturday 6pm."""
    import reminders
    from models import Reminder, User
    monkeypatch.setattr(reminders, "_naive_utcnow", lambda: _naive(_local(2026, 10, 30, 19)))   # Fri 7pm
    u = make_user(db, **CAMPUS)
    r = reminders.create_reminder(u.id, "gym", "18:00", days="mon,tue,wed,thu,fri,sat", source="program")
    assert _to_local(r["fire_at"]) == _local(2026, 10, 31, 16, 30)
    db.expire_all()
    row = db.get(Reminder, r["id"])
    user = db.get(User, u.id)
    nxt = reminders._next_for_row(user, row, TZ, after=_naive(_local(2026, 10, 31, 17)))     # Sat 5pm
    assert _to_local(nxt) == _local(2026, 11, 2, 18)


# ─── 3. the coach's context ──────────────────────────────────────────────────

def test_rsf_hours_line_for_campus_users_only(db, default_hours):
    from gym_beats import rsf_hours_line
    u = make_user(db, **CAMPUS)
    line = rsf_hours_line(u, _local(2026, 10, 31, 15))
    assert line.startswith("## RSF HOURS") and "today (sat): 8am–6pm, closes in 3h 00m" in line
    assert "tomorrow (sun): 8am–11pm" in line and "90 min before close" in line
    late = rsf_hours_line(u, _local(2026, 10, 31, 19))
    assert "closed right now" in late
    home = make_user(db, **dict(CAMPUS, equipment="home_gym"), phone="+15550003003")
    assert rsf_hours_line(home, _local(2026, 10, 31, 15)) is None


def test_heartbeat_context_carries_the_hours(db, default_hours, monkeypatch):
    import heartbeat
    from models import get_session, User
    monkeypatch.setattr(heartbeat, "_now_aware", lambda: _local(2026, 10, 31, 15).astimezone(timezone.utc))
    u = make_user(db, **CAMPUS)
    s = get_session()
    try:
        ctx = heartbeat._proactive_context(s.get(User, u.id), s)
    finally:
        s.close()
    assert "## RSF HOURS" in ctx and "today (sat): 8am–6pm" in ctx


def test_briefing_gym_window_is_clipped_to_the_hours(db, default_hours):
    from heartbeat import _clip_blocks_to_gym_hours
    from schedule import FreeBlock
    u = make_user(db, **CAMPUS)
    sat = lambda h, mi=0: _naive(_local(2026, 10, 31, h, mi))
    # 2pm–8pm free on a 6pm-close day → 2pm–6pm
    out = _clip_blocks_to_gym_hours(u, [FreeBlock(start=sat(14), end=sat(20))])
    assert [(_to_local(b.start).hour, _to_local(b.end).hour) for b in out] == [(14, 18)]
    # 5pm–8pm: the hour before close isn't a session (latest start 4:30) → dropped, next block wins
    out = _clip_blocks_to_gym_hours(u, [FreeBlock(start=sat(17), end=sat(20)), FreeBlock(start=sat(9), end=sat(11))])
    assert len(out) == 1 and _to_local(out[0].start).hour == 9
    # home gym: untouched
    home = make_user(db, **dict(CAMPUS, equipment="home_gym"), phone="+15550003004")
    blocks = [FreeBlock(start=sat(17), end=sat(20))]
    assert _clip_blocks_to_gym_hours(home, blocks) == blocks
