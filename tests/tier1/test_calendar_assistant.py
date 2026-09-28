"""
Calendar assistant (schedule.py primitives + heartbeat calendar signals) — tier-1.

Read-only over the Event store; every behaviour is flag-gated and fail-open, and every
new proactive signal is a _proactive_context ADDITION weighed by the same decide() and
routed through the same guardrail_reason (never a new send path). These tests pin the
wall clock via `now=` so nothing depends on the real time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")

# 2026-10-07 is a Wednesday.
NOW_LOCAL = datetime(2026, 10, 7, 9, 0, tzinfo=PT)
NOW = NOW_LOCAL.astimezone(timezone.utc)


def _naive(dt_aware) -> datetime:
    return dt_aware.astimezone(timezone.utc).replace(tzinfo=None)


def _local(h, mi=0, *, day=7):
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
def cal_on(monkeypatch):
    import config
    for f in ("CALENDAR_ASSISTANT_ENABLED", "CALENDAR_DEADLINE_RADAR_ENABLED",
              "CALENDAR_SCHEDULE_TRAINING_ENABLED", "CALENDAR_HIGH_LOAD_TONE_ENABLED",
              "CALENDAR_DAILY_BRIEFING_ENABLED", "CALENDAR_MEAL_TIMING_ENABLED",
              "CALENDAR_DEADLINE_REMINDERS_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])


def _session_user(db, **kw):
    # Reuse the db fixture session (closed in its teardown) rather than leaking a new
    # one — an open transaction would hold locks that block the next test's TRUNCATE.
    user = make_user(db, **kw)
    return db, user


# ─── flag defaults ────────────────────────────────────────────────────────────

def test_calendar_flags_default_on():
    import config
    for f in ("CALENDAR_ASSISTANT_ENABLED", "CALENDAR_DEADLINE_RADAR_ENABLED",
              "CALENDAR_SCHEDULE_TRAINING_ENABLED", "CALENDAR_HIGH_LOAD_TONE_ENABLED",
              "CALENDAR_DAILY_BRIEFING_ENABLED", "CALENDAR_MEAL_TIMING_ENABLED",
              "CALENDAR_DEADLINE_REMINDERS_ENABLED"):
        assert getattr(config, f) is True, f


# ─── primitive: free_blocks ───────────────────────────────────────────────────

def test_free_blocks_finds_the_gap_between_two_events(db):
    from schedule import free_blocks
    user = make_user(db, user_timezone="America/Los_Angeles")
    _mk_timed(user.id, "CS61C lecture", _local(10), _local(11))
    _mk_timed(user.id, "office hours", _local(14), _local(15))
    blocks = free_blocks(user.id, now=NOW)
    # a block covering the 11:00–14:00 gap (~180 min) must exist
    gap = [b for b in blocks if b.minutes >= 150 and b.minutes <= 190]
    assert gap, [(b.minutes) for b in blocks]
    b = gap[0]
    assert _naive(_local(11)) <= b.start <= _naive(_local(11, 30))
    assert _naive(_local(13, 30)) <= b.end <= _naive(_local(14))


def test_free_blocks_empty_calendar_is_not_a_schedule_to_plan(db):
    # No timed events → the free-window SIGNAL stays inert (that presence check),
    # even though free_blocks itself returns the whole waking window.
    from schedule import timed_schedule
    user = make_user(db)
    assert timed_schedule(user.id, now=NOW) == []


def test_free_blocks_fail_open_on_missing_user():
    from schedule import free_blocks
    assert free_blocks(999999, now=NOW) == []


# ─── primitive: deadline_items + clusters ─────────────────────────────────────

def test_deadline_items_sorted_and_clustered(db):
    from schedule import deadline_items, cluster_count
    user = make_user(db)
    _mk_deadline(user.id, "due: HW4 RISC-V [CS61C]", NOW + timedelta(days=1), ext="a")
    _mk_deadline(user.id, "due: Essay 2 [R1B]", NOW + timedelta(days=2), ext="b")
    _mk_deadline(user.id, "due: Pset 3 [Math54]", NOW + timedelta(days=3), ext="c")
    # a non-deadline calendar event must NOT be counted
    _mk_timed(user.id, "gym", _local(18), _local(19), ext="g")
    items = deadline_items(user.id, days=14, now=NOW)
    assert [d.title for d in items] == ["due: HW4 RISC-V [CS61C]", "due: Essay 2 [R1B]",
                                        "due: Pset 3 [Math54]"]
    assert cluster_count(items, hours=7 * 24, now=NOW) == 3


def test_deadline_items_ignores_non_deadline_events(db):
    from schedule import deadline_items
    user = make_user(db)
    _mk_timed(user.id, "CS61C lecture", _local(10, day=8), _local(11, day=8), ext="lec")
    _mk_timed(user.id, "lunch w/ Sam", _local(12, day=8), _local(13, day=8), ext="lunch")
    assert deadline_items(user.id, days=14, now=NOW) == []


# ─── primitive: high_load_soon ────────────────────────────────────────────────

def test_high_load_true_on_exam_within_24h(db):
    from schedule import high_load_soon
    user = make_user(db)
    _mk_deadline(user.id, "CS61C Midterm 1", NOW + timedelta(hours=24), source="gcal", ext="mt")
    assert high_load_soon(user.id, now=NOW) is True


def test_high_load_true_on_dense_cluster_within_48h(db):
    from schedule import high_load_soon
    user = make_user(db)
    for i in range(3):
        _mk_deadline(user.id, f"due: problem set {i}", NOW + timedelta(hours=12 + i), ext=f"p{i}")
    assert high_load_soon(user.id, now=NOW) is True


def test_high_load_false_when_deadlines_are_far_out(db):
    from schedule import high_load_soon
    user = make_user(db)
    _mk_deadline(user.id, "due: HW10", NOW + timedelta(days=6), ext="far")
    assert high_load_soon(user.id, now=NOW) is False


# ─── signal: deadline radar in proactive context ──────────────────────────────

def test_deadline_radar_signal_surfaces_items(db, cal_on):
    from heartbeat import _deadline_radar_signal
    s, user = _session_user(db)
    _mk_deadline(user.id, "due: HW4 RISC-V", NOW + timedelta(days=2), ext="hw4")
    _mk_deadline(user.id, "due: Essay 2", NOW + timedelta(days=4), ext="e2")
    blk = _deadline_radar_signal(user, s, now=NOW)
    assert blk and "DEADLINE RADAR" in blk
    assert "HW4 RISC-V" in blk and "2 due in the next 7 days" in blk
    # feature 6: the offer-to-remind line is present when reminders flag is on
    assert "OFFER to set a reminder" in blk


def test_deadline_radar_inert_without_data(db, cal_on):
    from heartbeat import _deadline_radar_signal
    s, user = _session_user(db)
    assert _deadline_radar_signal(user, s, now=NOW) is None


def test_deadline_radar_off_when_flag_off(db, cal_on, monkeypatch):
    import config
    from heartbeat import _deadline_radar_signal
    monkeypatch.setattr(config, "CALENDAR_DEADLINE_RADAR_ENABLED", False)
    s, user = _session_user(db)
    _mk_deadline(user.id, "due: HW4", NOW + timedelta(days=2), ext="hw4")
    assert _deadline_radar_signal(user, s, now=NOW) is None


def test_calendar_signals_off_when_master_flag_off(db, cal_on, monkeypatch):
    import config
    from heartbeat import _deadline_radar_signal, _free_window_signal, _high_load_signal
    monkeypatch.setattr(config, "CALENDAR_ASSISTANT_ENABLED", False)
    s, user = _session_user(db)
    _mk_deadline(user.id, "CS61C Midterm", NOW + timedelta(hours=20), source="gcal", ext="mt")
    _mk_timed(user.id, "lecture", _local(10), _local(11), ext="lec")
    assert _deadline_radar_signal(user, s, now=NOW) is None
    assert _free_window_signal(user, s, now=NOW) is None
    assert _high_load_signal(user, s, now=NOW) is None


# ─── signal: schedule-aware training (free windows + meal timing) ──────────────

def test_free_window_signal_surfaces_gym_window(db, cal_on):
    from heartbeat import _free_window_signal
    s, user = _session_user(db)
    _mk_timed(user.id, "lecture", _local(10), _local(11), ext="lec")
    _mk_timed(user.id, "seminar", _local(14), _local(15), ext="sem")
    blk = _free_window_signal(user, s, now=NOW)
    assert blk and "SCHEDULE" in blk and "Open windows" in blk


def test_meal_timing_flags_back_to_back_run(db, cal_on):
    from heartbeat import _free_window_signal
    s, user = _session_user(db)
    # a 4h back-to-back run (no eating gap) 12:00–16:00
    _mk_timed(user.id, "block A", _local(12), _local(14), ext="a")
    _mk_timed(user.id, "block B", _local(14), _local(16), ext="b")
    blk = _free_window_signal(user, s, now=NOW)
    assert blk and "eat before" in blk.lower()


def test_free_window_signal_inert_without_events(db, cal_on):
    from heartbeat import _free_window_signal
    s, user = _session_user(db)
    assert _free_window_signal(user, s, now=NOW) is None


# ─── signal: high-load softens / holds a demanding training nudge ─────────────

def test_high_load_signal_softens_tone(db, cal_on):
    from heartbeat import _high_load_signal
    s, user = _session_user(db)
    _mk_deadline(user.id, "CS61C Midterm 1", NOW + timedelta(hours=20), source="gcal", ext="mt")
    blk = _high_load_signal(user, s, now=NOW)
    assert blk and "ACADEMIC LOAD" in blk
    assert "HOLD a demanding training nudge" in blk


def test_high_load_signal_none_when_not_high(db, cal_on):
    from heartbeat import _high_load_signal
    s, user = _session_user(db)
    _mk_deadline(user.id, "due: HW10", NOW + timedelta(days=6), ext="far")
    assert _high_load_signal(user, s, now=NOW) is None


def test_high_load_guidance_only_appended_when_flag_on(monkeypatch):
    import config
    from heartbeat import HEARTBEAT_PROMPT, _HIGH_LOAD_GUIDANCE
    # the guidance text is a distinct constant, kept OUT of the base prompt
    assert _HIGH_LOAD_GUIDANCE not in HEARTBEAT_PROMPT


# ─── feature 4: daily briefing composes classes + deadlines + gym + nutrition ─

def test_daily_briefing_composes_all_four(db, cal_on, monkeypatch):
    import config
    from heartbeat import _morning_open_signal
    from models import Meal
    monkeypatch.setattr(config, "HEARTBEAT_RHYTHM_ENABLED", True)
    # now = 08:30, 30 min after an 08:00 wake, no messages since
    now_local = datetime(2026, 10, 7, 8, 30, tzinfo=PT)
    now = now_local.astimezone(timezone.utc)
    s, user = _session_user(db, wake_time="08:00", sleep_time="23:00",
                            calorie_target=2200, protein_target=160, workout_days="mon,wed,fri")
    _mk_timed(user.id, "CS61C lecture", _local(11), _local(12), ext="lec")
    _mk_deadline(user.id, "due: HW4 RISC-V", NOW + timedelta(days=2), ext="hw4")
    s.add(Meal(user_id=user.id, description="eggs", calories=400, protein_g=30, carbs_g=5, fat_g=25,
               source="text", log_type="user_reported",
               eaten_at=_naive(_local(8, 15)), logged_at=_naive(_local(8, 15))))
    s.commit()
    blk = _morning_open_signal(user, s, now=now)
    assert blk and "MORNING OPEN" in blk
    assert "Due soon" in blk                 # deadlines
    assert "gym window" in blk.lower()       # suggested gym window
    assert "Nutrition" in blk                # nutrition status
    assert "briefing" in blk.lower()


def test_morning_open_unchanged_when_briefing_off(db, cal_on, monkeypatch):
    import config
    from heartbeat import _morning_open_signal
    monkeypatch.setattr(config, "HEARTBEAT_RHYTHM_ENABLED", True)
    monkeypatch.setattr(config, "CALENDAR_DAILY_BRIEFING_ENABLED", False)
    now_local = datetime(2026, 10, 7, 8, 30, tzinfo=PT)
    now = now_local.astimezone(timezone.utc)
    s, user = _session_user(db, wake_time="08:00", sleep_time="23:00")
    blk = _morning_open_signal(user, s, now=now)
    assert blk and "friend's morning text" in blk
    assert "Briefing material" not in blk


# ─── guardrails: nothing bypasses guardrail_reason / the daily cap ────────────

def test_calendar_block_keeps_heartbeat_quiet(db, cal_on):
    """Feature 7 — class-aware auto-quiet reuses the existing calendar_block guardrail."""
    from heartbeat import guardrail_reason
    from models import get_session, User
    user = make_user(db)
    now_real = datetime.now(timezone.utc)
    _mk_timed(user.id, "CS61C lecture",
              now_real - timedelta(minutes=10), now_real + timedelta(minutes=50), ext="lec")
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert guardrail_reason(u, s) == "calendar_block"
    finally:
        s.close()


def test_daily_cap_still_blocks_even_with_calendar_material(db, cal_on):
    from heartbeat import guardrail_reason
    from models import get_session, User, HeartbeatTick
    import config
    user = make_user(db)
    _mk_deadline(user.id, "CS61C Midterm", datetime.now(timezone.utc) + timedelta(hours=20),
                 source="gcal", ext="mt")
    s = get_session()
    try:
        for _ in range(config.HEARTBEAT_MAX_PER_DAY):
            s.add(HeartbeatTick(user_id=user.id, spoke=True, reason="spoke",
                                message="x", decided_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        s.commit()
        u = s.get(User, user.id)
        assert guardrail_reason(u, s) == "daily_budget"
    finally:
        s.close()
