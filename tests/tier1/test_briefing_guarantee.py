"""
Morning-briefing guarantee + narrowed calendar hard gate — tier-1.

Live 2026-10-01 (founder, user 31): no morning briefing all day. Every tick 12:04→18:04
was guardrail:calendar_block because the heartbeat used calendar_block_soon's 90-min
default as a HARD gate — on a college day with classes every ~90 min every free gap is
"within 90 min of a class". The 12:04 tick (26 min before anything) and the 14:20 tick
(inside a 90-min free gap) were blocked, and the briefing's natural window was eaten.

Fix A: (A1) the heartbeat's calendar gate is CALENDAR_HARD_BLOCK_MINUTES ("class is about
to start"), not 90; (A2) while a MORNING OPEN is still owed (_morning_open_signal is
non-None — the once-per-day mechanism) the "class SOON" gate does not block; (A3) the
morning window is anchored at max(profile wake, measured wake). All pinned via `now=`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")
DAY = (2026, 10, 7)   # a Wednesday


def _naive(dt_aware) -> datetime:
    return dt_aware.astimezone(timezone.utc).replace(tzinfo=None)


def _local(h, mi=0):
    return datetime(*DAY, h, mi, tzinfo=PT)


def _utc(h, mi=0):
    return _local(h, mi).astimezone(timezone.utc)


def _mk_timed(user_id, title, start_aware, end_aware, *, ext=None):
    from events import upsert_external_event
    upsert_external_event(user_id, source="gcal", external_id=ext or title, title=title,
                          occurred_at=_naive(start_aware), ends_at=_naive(end_aware), all_day=False)


def _fresh(db, user):
    from models import get_session, User
    s = get_session()
    return s, s.get(User, user.id)


@pytest.fixture
def fix_a(monkeypatch):
    import config
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    monkeypatch.setattr(config, "HEARTBEAT_RHYTHM_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", True)
    monkeypatch.setattr(config, "QUIET_HOURS_FROM_PROFILE_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_ASSISTANT_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_DAILY_BRIEFING_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_BRIEFING_GUARANTEE_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_HARD_BLOCK_MINUTES", 20)
    yield


# ─── knob defaults ────────────────────────────────────────────────────────────

def test_fix_a_knob_defaults():
    import config
    assert config.CALENDAR_HARD_BLOCK_MINUTES == 20
    assert config.CALENDAR_BRIEFING_GUARANTEE_ENABLED is True


def test_calendar_block_soon_own_default_unchanged():
    """reminders.py relies on the function's 90-min default — only the heartbeat's call
    site narrowed."""
    import inspect
    from events import calendar_block_soon
    assert inspect.signature(calendar_block_soon).parameters["minutes"].default == 90


# ─── A1: the heartbeat's calendar gate is "class is about to start", not 90 min ────
# User wakes 07:00; now = 09:00 is OUTSIDE the morning window, so no briefing is pending
# and these exercise the narrowed gate alone.

def test_a1_event_30_min_out_no_longer_blocks(db, fix_a):
    from heartbeat import guardrail_reason
    from events import calendar_block_soon
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _mk_timed(user.id, "CS61C lecture", _local(9, 30), _local(10, 30))
    s, u = _fresh(db, user)
    try:
        assert calendar_block_soon(u.id, now=_utc(9)) is True      # the OLD 90-min gate blocked
        assert guardrail_reason(u, s, now=_utc(9)) is None         # the heartbeat no longer does
    finally:
        s.close()


def test_a1_event_10_min_out_still_blocks(db, fix_a):
    from heartbeat import guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _mk_timed(user.id, "CS61C lecture", _local(9, 10), _local(10, 10))
    s, u = _fresh(db, user)
    try:
        assert guardrail_reason(u, s, now=_utc(9)) == "calendar_block"
    finally:
        s.close()


def test_a1_ongoing_event_still_blocks(db, fix_a):
    from heartbeat import guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _mk_timed(user.id, "CS61C lecture", _local(8, 30), _local(10, 0))
    s, u = _fresh(db, user)
    try:
        assert guardrail_reason(u, s, now=_utc(9)) == "calendar_block"
    finally:
        s.close()


def test_a1_knob_is_read_from_config(db, fix_a, monkeypatch):
    import config
    from heartbeat import guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _mk_timed(user.id, "CS61C lecture", _local(9, 30), _local(10, 30))
    s, u = _fresh(db, user)
    try:
        monkeypatch.setattr(config, "CALENDAR_HARD_BLOCK_MINUTES", 45)
        assert guardrail_reason(u, s, now=_utc(9)) == "calendar_block"
    finally:
        s.close()


# ─── A2: the morning briefing bypasses "class SOON" exactly once ───────────────
# User wakes 11:30; now = 12:04 is inside the morning window (11:30–13:00).

def _pending_user(db, **kw):
    kw.setdefault("wake_time", "11:30")
    kw.setdefault("sleep_time", "02:00")
    return make_user(db, **kw)


def test_a2_pending_briefing_bypasses_class_soon(db, fix_a):
    from heartbeat import guardrail_reason, _morning_open_signal
    from events import calendar_block_soon
    user = _pending_user(db)
    _mk_timed(user.id, "Discussion", _local(12, 14), _local(13, 30))   # 10 min out
    s, u = _fresh(db, user)
    try:
        now = _utc(12, 4)
        assert calendar_block_soon(u.id, minutes=20, now=now) is True   # the gate WOULD block
        assert _morning_open_signal(u, s, now=now) is not None          # ...but a brief is owed
        assert guardrail_reason(u, s, now=now) is None
    finally:
        s.close()


def test_a2_no_bypass_once_briefed(db, fix_a):
    """The once-per-day mechanism: any non-reaction message since wake ends the pending
    signal, so the calendar gate applies normally again."""
    from heartbeat import guardrail_reason, _morning_open_signal
    from models import Message
    user = _pending_user(db)
    _mk_timed(user.id, "Discussion", _local(12, 14), _local(13, 30))
    db.add(Message(user_id=user.id, direction="out", body="morning — discussion at 12:30, then lecture",
                   message_type=None, created_at=_naive(_local(11, 40))))
    db.commit()
    s, u = _fresh(db, user)
    try:
        now = _utc(12, 4)
        assert _morning_open_signal(u, s, now=now) is None
        assert guardrail_reason(u, s, now=now) == "calendar_block"
    finally:
        s.close()


def test_a2_never_overrides_in_class(db, fix_a):
    """Provably mid-class (regex floor in_class, ends in the future) stays in_class — the
    guarantee only bypasses 'class SOON', never 'in class NOW'."""
    from heartbeat import guardrail_reason, _morning_open_signal
    from events import record_event
    user = _pending_user(db)
    _mk_timed(user.id, "Discussion", _local(12, 14), _local(13, 30))
    # in_class_now reads the wall clock (todays_events) — seed an event live right now.
    real_future = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    record_event(user.id, "in_class", ends_at=real_future, source="regex", raw_text="in class till 2")
    s, u = _fresh(db, user)
    try:
        now = _utc(12, 4)
        assert _morning_open_signal(u, s, now=now) is not None   # a brief IS owed...
        assert guardrail_reason(u, s, now=now) == "in_class"      # ...and still never mid-class
    finally:
        s.close()


def test_a2_earlier_gate_daily_budget_still_wins(db, fix_a):
    import config
    from heartbeat import guardrail_reason, _morning_open_signal
    from models import HeartbeatTick
    user = _pending_user(db)
    _mk_timed(user.id, "Discussion", _local(12, 14), _local(13, 30))
    for _ in range(config.HEARTBEAT_MAX_PER_DAY):
        db.add(HeartbeatTick(user_id=user.id, spoke=True, reason="spoke", message="x",
                             decided_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.commit()
    s, u = _fresh(db, user)
    try:
        now = _utc(12, 4)
        assert _morning_open_signal(u, s, now=now) is not None
        assert guardrail_reason(u, s, now=now) == "daily_budget"
    finally:
        s.close()


def test_a2_earlier_gate_active_conversation_still_wins(db, fix_a):
    from heartbeat import guardrail_reason
    from models import Message
    user = _pending_user(db)
    _mk_timed(user.id, "Discussion", _local(12, 14), _local(13, 30))
    # active_conversation reads the wall clock — an inbound right now.
    db.add(Message(user_id=user.id, direction="in", body="hey", message_type=None,
                   created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    db.commit()
    s, u = _fresh(db, user)
    try:
        assert guardrail_reason(u, s, now=_utc(12, 4)) == "active_conversation"
    finally:
        s.close()


def test_a2_quiet_hours_still_win_before_wake(db, fix_a):
    from heartbeat import guardrail_reason
    user = _pending_user(db)
    _mk_timed(user.id, "Discussion", _local(11, 30), _local(13, 0))
    s, u = _fresh(db, user)
    try:
        # 11:20 is inside sleep−30 .. wake+15 (01:30 .. 11:45): still quiet.
        assert guardrail_reason(u, s, now=_utc(11, 20)) == "quiet_hours_standing"
    finally:
        s.close()


def test_a2_flag_off_calendar_block_applies(db, fix_a, monkeypatch):
    import config
    from heartbeat import guardrail_reason, _morning_open_signal
    monkeypatch.setattr(config, "CALENDAR_BRIEFING_GUARANTEE_ENABLED", False)
    user = _pending_user(db)
    _mk_timed(user.id, "Discussion", _local(12, 14), _local(13, 30))
    s, u = _fresh(db, user)
    try:
        now = _utc(12, 4)
        assert _morning_open_signal(u, s, now=now) is not None
        assert guardrail_reason(u, s, now=now) == "calendar_block"
    finally:
        s.close()


def test_a2_fail_open_to_plain_block_when_signal_raises(db, fix_a, monkeypatch):
    import heartbeat
    user = _pending_user(db)
    _mk_timed(user.id, "Discussion", _local(12, 14), _local(13, 30))

    def boom(*a, **k):
        raise RuntimeError("signal exploded")
    monkeypatch.setattr(heartbeat, "_morning_open_signal", boom)
    s, u = _fresh(db, user)
    try:
        assert heartbeat.guardrail_reason(u, s, now=_utc(12, 4)) == "calendar_block"
    finally:
        s.close()


def test_a2_bypass_is_logged(db, fix_a, caplog):
    import logging
    from heartbeat import guardrail_reason
    user = _pending_user(db)
    _mk_timed(user.id, "Discussion", _local(12, 14), _local(13, 30))
    s, u = _fresh(db, user)
    try:
        with caplog.at_level(logging.INFO, logger="cued.heartbeat"):
            assert guardrail_reason(u, s, now=_utc(12, 4)) is None
        assert any("HEARTBEAT_BRIEFING_BYPASS_CALENDAR_BLOCK" in r.getMessage() for r in caplog.records)
    finally:
        s.close()


# ─── the live 2026-10-01 day, reproduced ──────────────────────────────────────

def _founder_day(user_id):
    _mk_timed(user_id, "Discussion", _local(12, 30), _local(14, 0), ext="disc")
    _mk_timed(user_id, "Lecture", _local(15, 30), _local(17, 0), ext="lec")
    _mk_timed(user_id, "dinner", _local(17, 0), _local(19, 0), ext="din")
    _mk_timed(user_id, "Seminar", _local(18, 0), _local(20, 0), ext="sem")


def test_live_1204_tick_now_reaches_the_model(db, fix_a):
    """Wake 11:30, tick at 12:04, Discussion at 12:30 (26 min out), nothing since wake.
    Under the OLD 90-min hard gate this was guardrail:calendar_block all day."""
    from heartbeat import guardrail_reason, _morning_open_signal
    from events import calendar_block_soon
    user = _pending_user(db)
    _founder_day(user.id)
    s, u = _fresh(db, user)
    try:
        now = _utc(12, 4)
        assert calendar_block_soon(u.id, now=now) is True            # old gate (90) blocked
        assert calendar_block_soon(u.id, minutes=20, now=now) is False  # new gate: 26 min out is fine
        sig = _morning_open_signal(u, s, now=now)
        assert sig and "MORNING OPEN" in sig
        assert guardrail_reason(u, s, now=now) is None
    finally:
        s.close()


def test_live_1420_free_gap_tick_now_reaches_the_model(db, fix_a):
    """14:20 sits inside the 90-min free gap (Discussion ended 14:00, Lecture 15:30). The
    morning window has closed by then — this is purely the narrowed gate working."""
    from heartbeat import guardrail_reason, _morning_open_signal
    user = _pending_user(db)
    _founder_day(user.id)
    s, u = _fresh(db, user)
    try:
        now = _utc(14, 20)
        assert _morning_open_signal(u, s, now=now) is None
        assert guardrail_reason(u, s, now=now) is None
    finally:
        s.close()


def test_live_1215_right_before_discussion_blocks_unless_brief_owed(db, fix_a):
    """15 min before Discussion: 'class is about to start' blocks — unless the brief is
    still owed, in which case this is the last chance and it goes through."""
    from heartbeat import guardrail_reason
    from models import Message
    user = _pending_user(db)
    _founder_day(user.id)
    s, u = _fresh(db, user)
    try:
        assert guardrail_reason(u, s, now=_utc(12, 15)) is None    # brief owed → through
    finally:
        s.close()
    db.add(Message(user_id=user.id, direction="out", body="morning", message_type=None,
                   created_at=_naive(_local(12, 5))))
    db.commit()
    s, u = _fresh(db, user)
    try:
        assert guardrail_reason(u, s, now=_utc(12, 15)) == "calendar_block"   # briefed → gate applies
    finally:
        s.close()


# ─── A3: the morning window follows a measured sleep-in ───────────────────────

def _connect_health(db, user_id):
    from models import Integration
    db.add(Integration(user_id=user_id, provider="google_health", status="connected",
                       external_id="EXT", meta={}))
    db.commit()


def _seed_nights(db, user_id, *, today_wake_local, bed_hm=(23, 30), days=4, synced_after=timedelta(minutes=20)):
    """Prior nights at a steady bed/wake; TODAY's sleep ended at `today_wake_local`.
    synced_at is pinned relative to each sleep_end (default: 20 min after = a FRESH,
    real-time read for the layered wake model) so these tests never depend on the wall
    clock — the column default is the real now."""
    from models import WearableDay
    base = datetime(*DAY, tzinfo=PT).date()
    for i in range(1, days):
        d = base - timedelta(days=i)
        bed = datetime(d.year, d.month, d.day, *bed_hm, tzinfo=PT) - timedelta(days=1)
        wake = datetime(d.year, d.month, d.day, 7, 0, tzinfo=PT)
        db.add(WearableDay(user_id=user_id, provider="google_health", day=d.isoformat(),
                           sleep_minutes=450, resting_hr=55, hrv_rmssd=40.0, steps=9000,
                           sleep_start=_naive(bed), sleep_end=_naive(wake),
                           synced_at=_naive(wake) + synced_after))
    bed = datetime(*DAY, *bed_hm, tzinfo=PT) - timedelta(days=1)
    db.add(WearableDay(user_id=user_id, provider="google_health", day=base.isoformat(),
                       sleep_minutes=600, resting_hr=55, hrv_rmssd=40.0, steps=100,
                       sleep_start=_naive(bed), sleep_end=_naive(today_wake_local),
                       synced_at=_naive(today_wake_local) + synced_after))
    db.commit()


@pytest.fixture
def wearable_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "GOOGLE_HEALTH_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_WEARABLE_AWARE_ENABLED", True)
    monkeypatch.setattr(config, "MEASURED_SLEEP_WINDOW_ENABLED", True)
    yield


def test_a3_slept_in_moves_the_morning_window(db, fix_a, wearable_on):
    """Profile wake 07:00 but the watch says they woke 10:30 today. The profile-only
    window (07:00–08:30) would have closed while quiet hours still held (until 10:45) —
    the briefing was lost. Anchored at the measured wake it is owed 10:30–12:00."""
    from heartbeat import _morning_open_signal, guardrail_reason
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect_health(db, user.id)
    _seed_nights(db, user.id, today_wake_local=_local(10, 30))
    s, u = _fresh(db, user)
    try:
        assert _morning_open_signal(u, s, now=_utc(8, 0)) is None            # watch: still asleep
        assert guardrail_reason(u, s, now=_utc(8, 0)) == "quiet_hours_standing"
        sig = _morning_open_signal(u, s, now=_utc(11, 0))                    # 30 min after measured wake
        assert sig and "10:30am wake" in sig
        assert guardrail_reason(u, s, now=_utc(11, 0)) is None
        assert _morning_open_signal(u, s, now=_utc(12, 30)) is None          # window closed (10:30+90)
    finally:
        s.close()


def test_a3_measured_earlier_never_shrinks_the_window(db, fix_a, wearable_on):
    """Watch wake 06:00 EARLIER than the 07:00 profile wake, but the row is STALE (synced
    20h after sleep_end — not a real-time read): the layered wake model ignores it and the
    #156 fallback keeps the anchor at the profile wake (max), so the window is still
    07:00–08:30. The FRESH early-wake case moves the anchor to 06:00 — see
    test_wake_model.py (both directions is the point of the layered model)."""
    from heartbeat import _morning_open_signal
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect_health(db, user.id)
    _seed_nights(db, user.id, today_wake_local=_local(6, 0), synced_after=timedelta(hours=20))
    s, u = _fresh(db, user)
    try:
        assert _morning_open_signal(u, s, now=_utc(6, 30)) is None
        sig = _morning_open_signal(u, s, now=_utc(7, 20))
        assert sig and "7:00am wake" in sig
    finally:
        s.close()


def test_a3_talked_since_profile_wake_still_counts(db, fix_a, wearable_on):
    """They texted at 08:00 (after the profile wake, before the watch's 10:30 wake) — they
    were plainly up; no second morning text at 11:00."""
    from heartbeat import _morning_open_signal
    from models import Message
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    _connect_health(db, user.id)
    _seed_nights(db, user.id, today_wake_local=_local(10, 30))
    db.add(Message(user_id=user.id, direction="in", body="up early today", message_type=None,
                   created_at=_naive(_local(8, 0))))
    db.commit()
    s, u = _fresh(db, user)
    try:
        assert _morning_open_signal(u, s, now=_utc(11, 0)) is None
    finally:
        s.close()


def test_a3_profile_only_without_wearable(db, fix_a):
    """No wearable → exactly today's behaviour (profile wake)."""
    from heartbeat import _morning_open_signal
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    s, u = _fresh(db, user)
    try:
        assert _morning_open_signal(u, s, now=_utc(7, 20)) is not None
        assert _morning_open_signal(u, s, now=_utc(11, 0)) is None
    finally:
        s.close()


def test_a3_anchor_fails_open_to_profile_wake(db, fix_a, wearable_on, monkeypatch):
    import heartbeat
    user = make_user(db, wake_time="07:00", sleep_time="23:00")

    def boom(*a, **k):
        raise RuntimeError("wearable read exploded")
    monkeypatch.setattr(heartbeat, "_measured_sw_hours", boom)
    s, u = _fresh(db, user)
    try:
        assert heartbeat._morning_open_signal(u, s, now=_utc(7, 20)) is not None
    finally:
        s.close()


# ─── A4: code-sent setup steps are not the morning open ───────────────────────
# Live 2026-10-10 (user 49, day one): the 11:19 water offer + calendar offer/link (setup
# sweeps) counted as "talked since they woke" → no briefing owed → the 11:55 tick let
# them breathe while the sweeps kept pinging.

def test_a4_setup_steps_do_not_count_as_the_morning_open(db, fix_a):
    from heartbeat import _morning_open_signal
    from models import Message
    user = make_user(db, wake_time="11:00", sleep_time="02:00")
    for i, (t, body) in enumerate([("water_offer", "one more thing: want me to ping u to drink water"),
                                   ("connect_offer", "want me on ur google calendar?"),
                                   ("connect_link", "https://app.cued.fit/c/gcal/x"),
                                   ("card_setup", "before i build ur first card, what do u bench")]):
        db.add(Message(user_id=user.id, direction="out", body=body, message_type=t,
                       created_at=_naive(_local(11, 19 + i))))
    db.commit()
    s, u = _fresh(db, user)
    try:
        assert _morning_open_signal(u, s, now=_utc(11, 30)) is not None, "setup steps are code's, not the coach's morning line"
    finally:
        s.close()


def test_a4_a_real_coach_line_or_their_text_still_counts(db, fix_a):
    from heartbeat import _morning_open_signal
    from models import Message
    a = make_user(db, wake_time="11:00", sleep_time="02:00")
    db.add(Message(user_id=a.id, direction="out", body="morning. 58° out", message_type="heartbeat",
                   created_at=_naive(_local(11, 5))))
    b = make_user(db, phone="+15550100042", wake_time="11:00", sleep_time="02:00")
    db.add(Message(user_id=b.id, direction="in", body="yes", message_type="water_offer",
                   created_at=_naive(_local(11, 21))))
    db.commit()
    for user in (a, b):
        s, u = _fresh(db, user)
        try:
            assert _morning_open_signal(u, s, now=_utc(11, 30)) is None
        finally:
            s.close()
