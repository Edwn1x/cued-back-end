"""Measured wearable sleep/wake window + recent-activity awareness.

The coach should reason about sleep/quiet hours from the user's ACTUAL rhythm (a MEDIAN of
recent wearable sleep_start/sleep_end) instead of the static onboarding sleep_time/wake_time
whenever the watch knows better, and should acknowledge real movement (today's steps/active
minutes) rather than imply someone's been sedentary.

Consumption side only — these tests never touch the google_health SYNC pipeline; they seed
wearable_days rows directly (the same shape the sync writes) and assert the readers +
prefer/fallback wiring:
  • measured_sleep_window returns a MEDIAN bed/wake window from seeded nights and None with
    too few nights;
  • the heartbeat quiet-hours gate uses the measured window when present and the static
    profile times when absent (no regression);
  • the reactive sleep-first window (_is_late_hour) uses the measured bedtime;
  • recent_activity surfaces steps/active_minutes + the "notably active" flag, and
    activity_context renders the advisory block;
  • everything is INERT (identical to today) with the flags off or no wearable data.

The clock is FROZEN throughout (now= injected / _at_local) — never real now, per the
date-fragility lessons in this repo.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user

TZ = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    # Prod defaults for the flags this feature reads.
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    monkeypatch.setattr(config, "MEASURED_SLEEP_WINDOW_ENABLED", True)
    monkeypatch.setattr(config, "WEARABLE_ACTIVITY_CONTEXT_ENABLED", True)
    yield


def _today() -> date:
    return datetime.now(TZ).date()


def _d(offset: int) -> str:
    return (_today() + timedelta(days=offset)).isoformat()


def _utc(local_iso: str) -> datetime:
    """A local wall-clock iso → naive UTC (wearable timestamps are naive UTC)."""
    return datetime.fromisoformat(local_iso).replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)


def _at_local(hour, minute=0, day=None):
    """An aware-UTC instant for today's (or given) local wall-clock time — for `now=`."""
    d = day or _today()
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=TZ).astimezone(timezone.utc)


def _connect(db, user_id, *, status="connected"):
    from models import Integration
    db.add(Integration(user_id=user_id, provider="google_health", status=status,
                       external_id="EXT", meta={}))
    db.commit()


def _night(db, user_id, wake_day_offset, *, bed_local, wake_local, **f):
    """Seed one night keyed by the morning it ENDS (wake_day_offset). bed_local/wake_local
    are 'HH:MM' local wall-clock strings; the bed is on the PRIOR calendar day."""
    from models import WearableDay
    wake_day = _today() + timedelta(days=wake_day_offset)
    bed_day = wake_day - timedelta(days=1)
    row = dict(sleep_minutes=440, steps=8000, resting_hr=55, hrv_rmssd=40.0)
    row.update(f)
    db.add(WearableDay(
        user_id=user_id, provider="google_health", day=wake_day.isoformat(),
        sleep_start=_utc(f"{bed_day.isoformat()}T{bed_local}:00"),
        sleep_end=_utc(f"{wake_day.isoformat()}T{wake_local}:00"),
        **row))
    db.commit()


def _day_row(db, user_id, day_offset, **f):
    from models import WearableDay
    db.add(WearableDay(user_id=user_id, provider="google_health",
                       day=(_today() + timedelta(days=day_offset)).isoformat(), **f))
    db.commit()


# ─── measured_sleep_window: median derivation ─────────────────────────────────

def test_measured_window_is_median_of_recent_nights(db):
    from wearable_read import measured_sleep_window
    from models import User
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    # Irregular bedtimes clustered ~23:00 (incl. one after-midnight) and wakes ~07:00.
    beds = ["22:00", "23:00", "00:30", "23:30", "22:30"]   # median (evening-anchored) = 23:00
    wakes = ["06:30", "07:00", "08:30", "07:30", "06:45"]  # median = 07:00
    for i, (b, w) in enumerate(zip(beds, wakes), start=1):
        _night(db, user.id, -i, bed_local=b, wake_local=w)
    db.expire_all()
    win = measured_sleep_window(db.get(User, user.id), db)
    assert win is not None
    assert win.nights == 5
    assert win.bed_hm[0] == 23          # median bedtime hour
    assert win.wake_hm[0] == 7          # median wake hour
    # most-recent measured wake (last actual wake) is exposed for the morning brief
    assert win.last_wake_local is not None
    assert win.last_wake_local.tzinfo is not None


def test_measured_window_after_midnight_bedtimes_dont_collapse_to_noon(db):
    """A cluster straddling midnight (23:30 / 00:30) must not median down to ~noon."""
    from wearable_read import measured_sleep_window
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    for i, b in enumerate(["23:30", "00:00", "00:30"], start=1):
        _night(db, user.id, -i, bed_local=b, wake_local="08:00")
    db.expire_all()
    win = measured_sleep_window(db.get(User, user.id), db)
    assert win is not None
    # median of {23:30, 00:00, 00:30} on the evening→dawn axis is 00:00, NOT noon
    assert win.bed_hm[0] == 0 and win.bed_hm[1] == 0


def test_measured_window_none_with_too_few_nights(db):
    from wearable_read import measured_sleep_window
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _night(db, user.id, -1, bed_local="23:00", wake_local="07:00")
    _night(db, user.id, -2, bed_local="23:00", wake_local="07:00")   # only 2 nights (<3)
    db.expire_all()
    assert measured_sleep_window(db.get(User, user.id), db) is None


def test_measured_window_ignores_nights_without_sleep_times(db):
    """Rows with steps but no sleep_start/end don't count toward the min-nights."""
    from wearable_read import measured_sleep_window
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    for i in range(1, 5):
        _day_row(db, user.id, -i, steps=9000, sleep_minutes=430)   # no sleep_start/end
    db.expire_all()
    assert measured_sleep_window(db.get(User, user.id), db) is None


def test_measured_window_inert_when_flag_off(db, monkeypatch):
    from wearable_read import measured_sleep_window
    from models import User
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", False)
    user = make_user(db)
    _connect(db, user.id)
    for i in range(1, 5):
        _night(db, user.id, -i, bed_local="23:00", wake_local="07:00")
    db.expire_all()
    assert measured_sleep_window(db.get(User, user.id), db) is None


def test_measured_window_inert_without_connection(db):
    from wearable_read import measured_sleep_window
    from models import User
    user = make_user(db)
    for i in range(1, 5):                      # rows but no connected integration
        _night(db, user.id, -i, bed_local="23:00", wake_local="07:00")
    db.expire_all()
    assert measured_sleep_window(db.get(User, user.id), db) is None


# ─── quiet-hours gate prefers the measured window ─────────────────────────────

@pytest.fixture
def quiet_on(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_QUIET_START_HOUR", 21)
    monkeypatch.setattr(config, "HEARTBEAT_QUIET_END_HOUR", 8)
    monkeypatch.setattr(config, "QUIET_HOURS_FROM_PROFILE_ENABLED", False)
    # Isolate the TYPICAL-window path from the slept-in (#116) morning extension.
    monkeypatch.setattr(config, "HEARTBEAT_WEARABLE_AWARE_ENABLED", False)
    yield


def test_quiet_gate_uses_measured_wake(db, quiet_on):
    """Static wake is 07:00 (floor ends 8am → 9am open), but the watch shows they typically
    wake ~10:00, so the measured window extends the morning floor and 9am is gated."""
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    for i in range(1, 5):
        _night(db, user.id, -i, bed_local="01:00", wake_local="10:00", sleep_minutes=540)
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(9)) == "quiet_hours_standing"
        # past the measured wake (11am) the floor is done
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(11)) != "quiet_hours_standing"
    finally:
        s.close()


def test_quiet_gate_static_when_no_measured_window(db, quiet_on):
    """No wearable rows → static profile times exactly: wake 07:00 → 9am NOT gated."""
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(9)) is None
    finally:
        s.close()


def test_quiet_gate_static_when_measured_flag_off(db, quiet_on, monkeypatch):
    """Measured window flag off → static behaviour even with slept-in wearable rows."""
    from models import get_session, User
    from heartbeat import guardrail_reason
    monkeypatch.setattr(config, "MEASURED_SLEEP_WINDOW_ENABLED", False)
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    for i in range(1, 5):
        _night(db, user.id, -i, bed_local="01:00", wake_local="10:00", sleep_minutes=540)
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(9)) is None
    finally:
        s.close()


# ─── reactive sleep-first window prefers the measured bedtime ─────────────────

@pytest.fixture
def late_on(monkeypatch):
    monkeypatch.setattr(config, "LATE_HOUR_SLEEP_FIRST_ENABLED", True)
    yield


def test_late_hour_uses_measured_bedtime(db, late_on):
    """Static sleep_time 23:00 (evening → ignored, default 1am start), but the watch shows
    they typically go to bed ~02:00, so 1:30am is NOT yet 'past sleep' and 3am is."""
    from models import User
    from agent_loop import _is_late_hour
    user = make_user(db, wake_time="09:00", sleep_time="23:00")
    _connect(db, user.id)
    for i in range(1, 5):
        _night(db, user.id, -i, bed_local="02:00", wake_local="09:00", sleep_minutes=420)
    db.expire_all()
    u = db.get(User, user.id)
    assert _is_late_hour(u, now=_at_local(1, 30), session=db) is False
    assert _is_late_hour(u, now=_at_local(3, 0), session=db) is True


def test_late_hour_static_when_no_session_or_flag_off(db, late_on, monkeypatch):
    """No session → static path (default 1am start) → 1:30am IS late. Same when the
    measured flag is off even with a session + rows."""
    from models import User
    from agent_loop import _is_late_hour
    user = make_user(db, wake_time="09:00", sleep_time="23:00")
    _connect(db, user.id)
    for i in range(1, 5):
        _night(db, user.id, -i, bed_local="02:00", wake_local="09:00", sleep_minutes=420)
    db.expire_all()
    u = db.get(User, user.id)
    # no session → static default window (start 1am) → 1:30am is late
    assert _is_late_hour(u, now=_at_local(1, 30)) is True
    # session given but measured flag off → static again
    monkeypatch.setattr(config, "MEASURED_SLEEP_WINDOW_ENABLED", False)
    assert _is_late_hour(u, now=_at_local(1, 30), session=db) is True


# ─── recent-activity awareness ────────────────────────────────────────────────

def test_recent_activity_surfaces_steps_and_active_flag(db):
    from wearable_read import recent_activity
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _day_row(db, user.id, 0, steps=8200, active_minutes=40)
    db.expire_all()
    act = recent_activity(db.get(User, user.id), db)
    assert act is not None
    assert act.steps == 8200 and act.active_minutes == 40
    assert act.notably_active is True


def test_recent_activity_not_notable_when_low(db):
    from wearable_read import recent_activity
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _day_row(db, user.id, 0, steps=1500, active_minutes=5)
    db.expire_all()
    act = recent_activity(db.get(User, user.id), db)
    assert act is not None and act.notably_active is False


def test_activity_context_block_when_moving(db):
    from wearable_read import activity_context
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _day_row(db, user.id, 0, steps=8200, active_minutes=40)
    db.expire_all()
    blk = activity_context(db.get(User, user.id), db)
    assert blk is not None
    assert blk.startswith("## ACTIVITY TODAY")
    assert "8,200 steps" in blk and "40 active min" in blk
    assert "sedentary" in blk.lower()          # the don't-imply-sedentary guidance


def test_activity_context_inert_when_flag_off(db, monkeypatch):
    from wearable_read import activity_context
    from models import User
    monkeypatch.setattr(config, "WEARABLE_ACTIVITY_CONTEXT_ENABLED", False)
    user = make_user(db)
    _connect(db, user.id)
    _day_row(db, user.id, 0, steps=8200, active_minutes=40)
    db.expire_all()
    assert activity_context(db.get(User, user.id), db) is None


def test_recent_activity_inert_without_today_row(db):
    from wearable_read import recent_activity
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _day_row(db, user.id, -1, steps=9000, active_minutes=45)   # yesterday only
    db.expire_all()
    assert recent_activity(db.get(User, user.id), db) is None


def test_recent_activity_inert_when_google_health_off(db, monkeypatch):
    from wearable_read import recent_activity
    from models import User
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", False)
    user = make_user(db)
    _connect(db, user.id)
    _day_row(db, user.id, 0, steps=8200, active_minutes=40)
    db.expire_all()
    assert recent_activity(db.get(User, user.id), db) is None
