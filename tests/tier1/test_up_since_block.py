"""UP SINCE — the daytime mirror of the late/evening sleep signals.

Live 2026-10-08 12:33 PT (user 48): 44 minutes after a MEASURED 11:50 wake, in class, the
reply to "What?" ended "but that's later. go sleep rn". No code block said late; the 5am
"go sleep" exchange carried forward because nothing marked the sleep in between. The block
only appears when today's wake is KNOWN (watch sleep end, or their first activity) — a
profile/typical wake is a guess, not evidence they're up."""
from __future__ import annotations

from datetime import timedelta

import pytest

import config
from tests.tier1.test_wake_model import (_local, _utc, _connect_health, _today_sleep, _text_in,
                                         _fresh, _late_riser, model_on)  # noqa: F401  (fixture)

HDR = "## UP SINCE"


def test_measured_wake_marks_them_up_and_last_nights_sleep_talk_stale(db, model_on):
    import agent_loop
    u = _late_riser(db)
    _connect_health(db, u.id)
    _today_sleep(db, u.id, wake_h=11, wake_m=50, bed_h=5, bed_m=14, sleep_minutes=392)
    s, row = _fresh(db, u)
    try:
        blk = agent_loop._up_since_block(row, s, now=_utc(12, 34))
        assert blk and blk.startswith("## UP SINCE 11:50am (today — the watch's sleep end)"), blk
        assert "They've been up 44 min" in blk and "was LAST NIGHT" in blk and "do not repeat it" in blk
        # and the two sleep signals stay off in the daytime
        assert agent_loop._is_late_hour(row, now=_utc(12, 34), session=s) is False
        assert agent_loop._evening_not_late_block(row, s, now=_utc(12, 34)) is None
    finally:
        s.close()


def test_activity_wake_counts_and_hours_render(db, model_on):
    import agent_loop
    u = _late_riser(db)
    _text_in(db, u.id, 10, 5, body="morning")
    s, row = _fresh(db, u)
    try:
        blk = agent_loop._up_since_block(row, s, now=_utc(13, 20))
        assert blk and "UP SINCE 10:05am (today — their first activity (text at 10:05))" in blk and "up 3h15" in blk
    finally:
        s.close()


def test_profile_wake_is_not_evidence_and_too_soon_or_evening_is_silent(db, model_on):
    import agent_loop
    u = _late_riser(db)                        # profile wake 12:00, nothing measured, no activity
    s, row = _fresh(db, u)
    try:
        assert agent_loop._up_since_block(row, s, now=_utc(14, 0)) is None     # profile only → no block
    finally:
        s.close()
    _connect_health(db, u.id)
    _today_sleep(db, u.id, wake_h=11, wake_m=50, bed_h=5, bed_m=14, sleep_minutes=392)
    s, row = _fresh(db, u)
    try:
        assert agent_loop._up_since_block(row, s, now=_utc(11, 55)) is None     # 5 min: not yet
        assert agent_loop._up_since_block(row, s, now=_utc(21, 30)) is None     # evening: the other signals own it
    finally:
        s.close()


def test_flag_off_and_no_session_are_inert(db, model_on, monkeypatch):
    import agent_loop
    u = _late_riser(db)
    _connect_health(db, u.id)
    _today_sleep(db, u.id, wake_h=11, wake_m=50, bed_h=5, bed_m=14, sleep_minutes=392)
    s, row = _fresh(db, u)
    try:
        assert agent_loop._up_since_block(row, None, now=_utc(12, 34)) is None
        monkeypatch.setattr(config, "UP_SINCE_SIGNAL_ENABLED", False)
        assert agent_loop._up_since_block(row, s, now=_utc(12, 34)) is None
    finally:
        s.close()


def test_block_rides_the_loop_context(db, model_on, monkeypatch):
    """Through build_loop_context with the real clock: a measured wake one hour ago."""
    import agent_loop
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from models import WearableDay, get_session, User
    u = _late_riser(db)
    _connect_health(db, u.id)
    now = datetime.now(timezone.utc)
    local_now = now.astimezone(ZoneInfo("America/Los_Angeles"))
    if not (6 <= local_now.hour < 20):
        pytest.skip("real-clock case only meaningful in the daytime; the pinned tests cover the rest")
    wake = now - timedelta(hours=1)
    db.add(WearableDay(user_id=u.id, provider="google_health", day=local_now.date().isoformat(),
                       sleep_minutes=400, resting_hr=55, hrv_rmssd=40.0, steps=100,
                       sleep_start=(wake - timedelta(hours=7)).replace(tzinfo=None),
                       sleep_end=wake.replace(tzinfo=None), synced_at=(wake + timedelta(minutes=20)).replace(tzinfo=None)))
    db.commit()
    s = get_session()
    try:
        ctx = agent_loop.build_loop_context(s.get(User, u.id), s)
        assert HDR in ctx and "was LAST NIGHT" in ctx
    finally:
        s.close()
