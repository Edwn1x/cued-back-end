"""
Late-hour / near-sleep priority flip for REACTIVE nutrition nudging (LATE_HOUR_SLEEP_FIRST).

Live 2026-09-28 ~4am (founder, user 31): late-night snack logs + a low protein day → the
coach kept pushing "eat some actual protein bro" / "it's 4am… eat some of that beef" toward
the daily target, ignoring the hour. The reasoning to say "go to sleep, protein can wait"
existed (the coach agreed when asked) but wasn't applied PROACTIVELY. build_loop_context
now surfaces a compact "prioritize sleep over macro-completion" signal in the user's small
hours so the coach nudges sleep and frames remaining protein as a tomorrow thing. It stays
advisory: logging still works and a direct food/macro question is still answered honestly.

The clock is FROZEN (agent_loop._late_clock patched, or now= injected) — never real now,
per the date-fragility lessons in this repo.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config
import agent_loop


SIGNAL_HEADER = "LATE / PAST SLEEP WINDOW"


def _pdt(y, mo, d, h, mi=0):
    """Aware-UTC instant for an America/Los_Angeles wall time (freezes the clock)."""
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(timezone.utc)


def _freeze(monkeypatch, instant):
    monkeypatch.setattr(agent_loop, "_late_clock", lambda: instant)


class _U:
    """A minimal user for the detection unit tests (no DB)."""
    user_timezone = "America/Los_Angeles"

    def __init__(self, sleep_time=None, wake_time=None):
        self.sleep_time = sleep_time
        self.wake_time = wake_time


# --------------------------------------------------------------------------------------
# Detection (now= injected, no DB) — the late/near-sleep boundary.
# --------------------------------------------------------------------------------------

def test_default_small_hours_window_flags_4am_not_evening_or_afternoon():
    u = _U()  # no sleep/wake pattern → 1am–6am small-hours default
    assert agent_loop._is_late_hour(u, now=_pdt(2026, 9, 28, 4, 0)) is True
    assert agent_loop._is_late_hour(u, now=_pdt(2026, 9, 28, 22, 0)) is False  # 10pm dinner
    assert agent_loop._is_late_hour(u, now=_pdt(2026, 9, 28, 14, 0)) is False  # 2pm


def test_late_sleeper_shifts_start_and_evening_sleep_time_is_ignored():
    # sleeps 2am → 1am is not yet "past sleep"; 3am is.
    u = _U(sleep_time="02:00", wake_time="09:00")
    assert agent_loop._is_late_hour(u, now=_pdt(2026, 9, 28, 1, 0)) is False
    assert agent_loop._is_late_hour(u, now=_pdt(2026, 9, 28, 3, 0)) is True
    # an EVENING sleep_time must NOT make 10pm "too late to eat dinner" — floor holds.
    u2 = _U(sleep_time="22:00", wake_time="06:00")
    assert agent_loop._is_late_hour(u2, now=_pdt(2026, 9, 28, 22, 0)) is False


def test_wake_time_tightens_end_but_late_waker_stays_capped():
    # early waker: window ends at their 5am wake → 5:30am is no longer small hours.
    u = _U(sleep_time="01:00", wake_time="05:00")
    assert agent_loop._is_late_hour(u, now=_pdt(2026, 9, 28, 5, 30)) is False
    # late waker (11am): end stays capped at 6am so we never tell a 10am texter to sleep.
    u2 = _U(sleep_time="01:00", wake_time="11:00")
    assert agent_loop._is_late_hour(u2, now=_pdt(2026, 9, 28, 10, 0)) is False
    assert agent_loop._is_late_hour(u2, now=_pdt(2026, 9, 28, 4, 0)) is True


def test_missing_sleep_data_falls_back_to_default_window():
    # unparseable free-text sleep phrases → fail-open to the 1am–6am default.
    u = _U(sleep_time="late", wake_time="whenever")
    assert agent_loop._is_late_hour(u, now=_pdt(2026, 9, 28, 4, 0)) is True
    assert agent_loop._is_late_hour(u, now=_pdt(2026, 9, 28, 12, 0)) is False


def test_flag_off_detection_is_inert(monkeypatch):
    monkeypatch.setattr(config, "LATE_HOUR_SLEEP_FIRST_ENABLED", False)
    assert agent_loop._is_late_hour(_U(), now=_pdt(2026, 9, 28, 4, 0)) is False


# --------------------------------------------------------------------------------------
# Context (build_loop_context) — the signal the model actually reads.
# --------------------------------------------------------------------------------------

def _meal(user_id, cal, pro, *, at, desc="snack"):
    from models import Meal
    return Meal(user_id=user_id, description=desc, calories=cal, protein_g=pro,
                eaten_at=at, source="text", log_type="user_reported")


def _ctx(user_id):
    from models import get_session, User
    s = get_session()
    try:
        return agent_loop.build_loop_context(s.get(User, user_id), s)
    finally:
        s.close()


def _now_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_small_hours_unmet_protein_surfaces_sleep_first_signal(db, monkeypatch):
    from tests.factories import make_user
    from models import get_session

    user = make_user(db, calorie_target=2600, protein_target=180)
    s = get_session()
    try:
        s.add(_meal(user.id, 500, 40, at=_now_naive(), desc="late night cereal"))
        s.commit()
    finally:
        s.close()

    _freeze(monkeypatch, _pdt(2026, 9, 28, 4, 0))   # 4am local
    ctx = _ctx(user.id)

    assert SIGNAL_HEADER in ctx, ctx
    assert "PRIORITIZE SLEEP OVER MACRO-COMPLETION" in ctx
    assert "protein can wait" in ctx
    assert "Do NOT harp" in ctx
    # unmet protein (180 - 40 = 140g short) is named as a tomorrow thing, not a push.
    assert "short of today's target" in ctx
    # advisory, not a block: logging still works, direct questions still answered.
    assert "never refuse a log" in ctx


def test_daytime_behavior_unchanged_no_sleep_first_signal(db, monkeypatch):
    """Normal daytime hours: no sleep-first flip, macro nudging data intact as today."""
    from tests.factories import make_user
    from models import get_session

    user = make_user(db, calorie_target=2600, protein_target=180)
    s = get_session()
    try:
        s.add(_meal(user.id, 500, 40, at=_now_naive(), desc="lunch"))
        s.commit()
    finally:
        s.close()

    _freeze(monkeypatch, _pdt(2026, 9, 28, 14, 0))   # 2pm local
    ctx = _ctx(user.id)

    assert SIGNAL_HEADER not in ctx, "sleep-first signal leaked into daytime context"
    # the ordinary macro-nudging surface is still there unchanged.
    assert "protein remaining vs target: 140g" in ctx, ctx


def test_logging_still_works_late_at_night(db, monkeypatch):
    """The priority-flip must not suppress the log: a meal reported at night still renders
    in context (totals + the row), alongside the sleep-first signal."""
    from tests.factories import make_user
    from models import get_session

    user = make_user(db, calorie_target=2600, protein_target=180)
    s = get_session()
    try:
        s.add(_meal(user.id, 300, 30, at=_now_naive(), desc="2am protein shake"))
        s.commit()
    finally:
        s.close()

    _freeze(monkeypatch, _pdt(2026, 9, 28, 4, 0))
    ctx = _ctx(user.id)

    assert SIGNAL_HEADER in ctx
    assert "2am protein shake" in ctx, "the logged meal was dropped from context at night"
    assert "calories: 300 | protein: 30g" in ctx, "totals not computed at night"


def test_flag_off_context_is_inert(db, monkeypatch):
    from tests.factories import make_user
    from models import get_session

    monkeypatch.setattr(config, "LATE_HOUR_SLEEP_FIRST_ENABLED", False)
    user = make_user(db, calorie_target=2600, protein_target=180)
    s = get_session()
    try:
        s.add(_meal(user.id, 500, 40, at=_now_naive(), desc="late night cereal"))
        s.commit()
    finally:
        s.close()

    _freeze(monkeypatch, _pdt(2026, 9, 28, 4, 0))
    ctx = _ctx(user.id)

    assert SIGNAL_HEADER not in ctx, "signal present with the flag OFF"
