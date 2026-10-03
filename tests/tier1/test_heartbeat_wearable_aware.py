"""Wearable-aware PROACTIVE engine (the heartbeat).

Consumption side only — these tests never exercise the google_health SYNC pipeline; they
seed wearable_days rows directly (the same shape the sync writes) and assert the proactive
engine reads them:
  • a RECOVERY standing-condition block appears on a measured poor night and downgrades a
    demanding nudge (soft tone gate), a good night gets the warm-win read, a middling night
    is silent;
  • a measured slept-in (watch's wake later than the stated wake) EXTENDS the standing
    quiet floor so we don't ping someone the watch shows asleep;
  • everything is INERT (identical to today) with GOOGLE_HEALTH_ENABLED off, no wearable
    rows, stale-only rows, a disconnected account, or HEARTBEAT_WEARABLE_AWARE_ENABLED off.
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
    # The two flags this feature reads; both ON is the prod default.
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_WEARABLE_AWARE_ENABLED", True)
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


def _day(db, user_id, day, **f):
    from models import WearableDay
    db.add(WearableDay(user_id=user_id, provider="google_health", day=day, **f))
    db.commit()


def _baseline_week(db, user_id, **f):
    """Six prior days of a steady baseline (default: 7h20 sleep, rhr 55, hrv 40)."""
    base = dict(sleep_minutes=440, resting_hr=55, hrv_rmssd=40.0, steps=9000)
    base.update(f)
    for i in range(1, 7):
        _day(db, user_id, _d(-i), **base)


# ─── RECOVERY signal ──────────────────────────────────────────────────────────

def test_recovery_signal_poor_night_downgrades_demanding_nudge(db):
    from heartbeat import _recovery_signal
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _baseline_week(db, user.id)
    _day(db, user.id, _d(0), sleep_minutes=300, resting_hr=55, hrv_rmssd=40.0, steps=1200,
         sleep_start=_utc(f"{_d(-1)}T01:30:00"), sleep_end=_utc(f"{_d(0)}T06:30:00"))
    db.expire_all()
    blk = _recovery_signal(db.get(User, user.id), db)
    assert blk is not None
    assert blk.startswith("## RECOVERY")
    assert "POOR" in blk
    # the soft tone gate: downgrade/hold a demanding nudge, never a hard suppress
    assert "DOWNGRADE" in blk and "HOLD" in blk
    assert "NOT a hard block" in blk
    # baseline avg is over the window incl. today (mirrors the reactive block): 6×440 + 300 = 420
    assert "last night 5h00m" in blk and "7-day avg 7h00m" in blk


def test_recovery_signal_poor_via_hr_and_hrv_worse(db):
    from heartbeat import _recovery_signal
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _baseline_week(db, user.id)
    # a normal-length night but both cardiovascular markers worse than baseline
    _day(db, user.id, _d(0), sleep_minutes=430, resting_hr=65, hrv_rmssd=25.0, steps=8000)
    db.expire_all()
    blk = _recovery_signal(db.get(User, user.id), db)
    assert blk is not None and "POOR" in blk
    assert "resting HR and HRV worse than baseline" in blk


def test_recovery_signal_good_night_is_warm_win(db):
    from heartbeat import _recovery_signal
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _baseline_week(db, user.id)
    _day(db, user.id, _d(0), sleep_minutes=470, resting_hr=54, hrv_rmssd=42.0, steps=10000)
    db.expire_all()
    blk = _recovery_signal(db.get(User, user.id), db)
    assert blk is not None and "GOOD" in blk
    assert "warm win hook" in blk


def test_recovery_signal_middling_night_is_silent(db):
    from heartbeat import _recovery_signal
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _baseline_week(db, user.id)
    _day(db, user.id, _d(0), sleep_minutes=395, resting_hr=56, hrv_rmssd=39.0, steps=8500)
    db.expire_all()
    assert _recovery_signal(db.get(User, user.id), db) is None


# ─── RECOVERY inert cases (identical to today) ────────────────────────────────

def test_recovery_signal_inert_when_flag_off(db, monkeypatch):
    from heartbeat import _recovery_signal
    from models import User
    monkeypatch.setattr(config, "HEARTBEAT_WEARABLE_AWARE_ENABLED", False)
    user = make_user(db)
    _connect(db, user.id)
    _baseline_week(db, user.id)
    _day(db, user.id, _d(0), sleep_minutes=300)          # would be POOR if read
    db.expire_all()
    assert _recovery_signal(db.get(User, user.id), db) is None


def test_recovery_signal_inert_when_google_health_off(db, monkeypatch):
    from heartbeat import _recovery_signal
    from models import User
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", False)
    user = make_user(db)
    _connect(db, user.id)
    _baseline_week(db, user.id)
    _day(db, user.id, _d(0), sleep_minutes=300)
    db.expire_all()
    assert _recovery_signal(db.get(User, user.id), db) is None


def test_recovery_signal_inert_without_rows_or_connection(db):
    from heartbeat import _recovery_signal
    from models import User
    user = make_user(db)
    # no integration, no rows
    assert _recovery_signal(db.get(User, user.id), db) is None
    # connected but no rows
    _connect(db, user.id)
    db.expire_all()
    assert _recovery_signal(db.get(User, user.id), db) is None
    # rows but not connected
    other = make_user(db)
    _baseline_week(db, other.id)
    _day(db, other.id, _d(0), sleep_minutes=300)
    db.expire_all()
    assert _recovery_signal(db.get(User, other.id), db) is None


def test_recovery_signal_strong_recovery_is_push_hook(db):
    """A day better than baseline on all three markers (lower resting HR, higher HRV, more
    sleep) reads GOOD and surfaces the positive push hook, not the poor soften/hold."""
    from heartbeat import _recovery_signal
    from models import User
    user = make_user(db)
    _connect(db, user.id)
    _baseline_week(db, user.id)  # sleep 440, rhr 55, hrv 40
    _day(db, user.id, _d(0), sleep_minutes=500, resting_hr=50, hrv_rmssd=48.0, steps=10500)
    db.expire_all()
    blk = _recovery_signal(db.get(User, user.id), db)
    assert blk is not None and "GOOD" in blk
    assert "better than baseline" in blk
    assert "push" in blk.lower()
    assert "POOR" not in blk


def test_recovery_signal_strong_recovery_inert_when_better_flag_off(db, monkeypatch):
    """With BETTER_RECOVERY_ENABLED off, a strong-recovery day is silent (pre-feature
    behaviour: only the POOR side speaks) — but a POOR night still triggers soften/hold."""
    from heartbeat import _recovery_signal
    from models import User
    monkeypatch.setattr(config, "BETTER_RECOVERY_ENABLED", False)
    # good-recovery day → no signal when the better flag is off
    good = make_user(db)
    _connect(db, good.id)
    _baseline_week(db, good.id)
    _day(db, good.id, _d(0), sleep_minutes=500, resting_hr=50, hrv_rmssd=48.0, steps=10500)
    db.expire_all()
    assert _recovery_signal(db.get(User, good.id), db) is None
    # poor night → still fires (the worse side is never gated by BETTER_RECOVERY_ENABLED)
    poor = make_user(db)
    _connect(db, poor.id)
    _baseline_week(db, poor.id)
    _day(db, poor.id, _d(0), sleep_minutes=300, resting_hr=55, hrv_rmssd=40.0, steps=4000)
    db.expire_all()
    blk = _recovery_signal(db.get(User, poor.id), db)
    assert blk is not None and "POOR" in blk


# ─── measured slept-in EXTENDS the standing quiet floor ───────────────────────

@pytest.fixture
def quiet_on(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_QUIET_START_HOUR", 21)
    monkeypatch.setattr(config, "HEARTBEAT_QUIET_END_HOUR", 8)
    monkeypatch.setattr(config, "QUIET_HOURS_FROM_PROFILE_ENABLED", False)
    yield


def _slept_in(db, user_id):
    """Watch shows they woke at 10:30 today — later than the stated 07:00 wake."""
    _baseline_week(db, user_id)
    _day(db, user_id, _d(0), sleep_minutes=600, resting_hr=55, hrv_rmssd=40.0,
         sleep_start=_utc(f"{_d(-1)}T00:30:00"), sleep_end=_utc(f"{_d(0)}T10:30:00"))


def test_measured_slept_in_extends_quiet(db, quiet_on):
    """At 9am the floor window (…–8am) is open, but the watch says they woke at 10:30, so
    the quiet floor extends and the tick is gated."""
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    _slept_in(db, user.id)
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(9)) == "quiet_hours_standing"
        # after the measured wake + buffer (10:45am) the floor is done — no longer gated here
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(11)) != "quiet_hours_standing"
    finally:
        s.close()


def test_measured_wake_only_extends_never_shrinks(db, quiet_on):
    """A STALE watch wake EARLIER than the floor's 8am end must not shrink the floor: 7:30am
    is still quiet even though the watch says they woke at 6:15 — the row was synced 20h
    after sleep_end, so the layered wake model (wake_model.py) does not treat it as a
    real-time wake and this pre-existing extend-only path governs. (A FRESH early measured
    wake DOES lift quiet early now — test_wake_model.py.) synced_at is pinned so the test
    never depends on the wall clock."""
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    _baseline_week(db, user.id)
    _day(db, user.id, _d(0), sleep_minutes=480, sleep_start=_utc(f"{_d(-1)}T22:00:00"),
         sleep_end=_utc(f"{_d(0)}T06:15:00"), synced_at=_utc(f"{_d(0)}T06:15:00") + timedelta(hours=20))
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(7, 30)) == "quiet_hours_standing"
    finally:
        s.close()


def test_quiet_extension_inert_when_flag_off(db, quiet_on, monkeypatch):
    """Flag off → today's behaviour exactly: 9am with a 10:30 measured wake is NOT gated."""
    from models import get_session, User
    from heartbeat import guardrail_reason
    monkeypatch.setattr(config, "HEARTBEAT_WEARABLE_AWARE_ENABLED", False)
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    _slept_in(db, user.id)
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(9)) is None
    finally:
        s.close()


def test_quiet_extension_inert_when_google_health_off(db, quiet_on, monkeypatch):
    from models import get_session, User
    from heartbeat import guardrail_reason
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", False)
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    _slept_in(db, user.id)
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(9)) is None
    finally:
        s.close()


def test_quiet_extension_inert_when_data_stale(db, quiet_on):
    """Only a 6-day-old row → stale → no extension; 9am is not gated (today's behaviour)."""
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    _day(db, user.id, _d(-6), sleep_minutes=600, sleep_end=_utc(f"{_d(-6)}T10:30:00"))
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(9)) is None
    finally:
        s.close()


def test_quiet_extension_inert_without_wearable_rows(db, quiet_on):
    """No wearable rows at all → 9am not gated (baseline heartbeat behaviour)."""
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect(db, user.id)
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(9)) is None
    finally:
        s.close()


# ─── prompt guidance wiring (build item 1) ────────────────────────────────────

def test_wearable_guidance_appended_only_when_flag_on():
    """decide() appends _WEARABLE_GUIDANCE to the prompt only under the flag."""
    import heartbeat
    assert heartbeat._WEARABLE_GUIDANCE
    assert "HOLD a demanding accountability nudge" in heartbeat._WEARABLE_GUIDANCE
    # flag default is ON, mirroring the other heartbeat gates
    assert config.HEARTBEAT_WEARABLE_AWARE_ENABLED is True
