"""Per-user nutrition-day reset (founder 2026-09-16: 'clean slate after I sleep').
Default stays midnight; a user who explicitly asks gets a shifted rollover so a
post-midnight meal lands on the prior day. timefmt.local_day_bounds is the one reader."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest


def _at_pdt(y, mo, d, h, mi=0):
    """A naive-UTC instant for the given America/Los_Angeles wall time (PDT = UTC-7)."""
    from zoneinfo import ZoneInfo
    local = datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/Los_Angeles"))
    return local.astimezone(timezone.utc).replace(tzinfo=None)


class _U:
    user_timezone = "America/Los_Angeles"
    def __init__(self, reset=0):
        self.day_reset_hour = reset


def test_default_is_midnight_window():
    from timefmt import local_day_bounds
    now = _at_pdt(2026, 9, 18, 14, 0)   # 2pm PDT on the 18th
    start, end = local_day_bounds(_U(0), now=now.replace(tzinfo=timezone.utc))
    # window = 18th 00:00 → 19th 00:00 local
    assert start == _at_pdt(2026, 9, 18, 0, 0)
    assert end == _at_pdt(2026, 9, 19, 0, 0)


def test_reset_4am_pushes_post_midnight_meal_to_prior_day():
    from timefmt import local_day_bounds
    # It's 1am PDT on the 16th. With a 4am reset we're still in the window that
    # STARTED at 4am on the 15th — so a 12:20am meal counts for the 15th.
    now = _at_pdt(2026, 9, 16, 1, 0)
    start, end = local_day_bounds(_U(4), now=now.replace(tzinfo=timezone.utc))
    assert start == _at_pdt(2026, 9, 15, 4, 0)
    assert end == _at_pdt(2026, 9, 16, 4, 0)
    meal_1220am = _at_pdt(2026, 9, 16, 0, 20)
    assert start <= meal_1220am < end   # the 12:20am meal is in the 15th's window


def test_reset_4am_after_rollover_is_current_day():
    from timefmt import local_day_bounds
    now = _at_pdt(2026, 9, 16, 10, 0)   # 10am PDT, past the 4am rollover
    start, end = local_day_bounds(_U(4), now=now.replace(tzinfo=timezone.utc))
    assert start == _at_pdt(2026, 9, 16, 4, 0)
    assert end == _at_pdt(2026, 9, 17, 4, 0)


def test_day_reset_hour_clamps_out_of_range():
    from timefmt import day_reset_hour
    assert day_reset_hour(_U(0)) == 0
    assert day_reset_hour(_U(4)) == 4
    assert day_reset_hour(_U(20)) == 0    # out of 0–11 → default
    assert day_reset_hour(_U(-3)) == 0


def test_recompute_totals_honors_reset(db):
    """A 1am meal with a 4am reset must NOT count toward the day that just started."""
    from tests.factories import make_user
    from models import get_session, User, Meal, recompute_daily_totals
    from timefmt import local_day_bounds
    user = make_user(db, day_reset_hour=4)
    # eat at 1am today (before the 4am rollover) → belongs to yesterday's window
    now = datetime.now(timezone.utc)
    from zoneinfo import ZoneInfo
    local_now = now.astimezone(ZoneInfo("America/Los_Angeles"))
    one_am = local_now.replace(hour=1, minute=0, second=0, microsecond=0)
    if local_now.hour < 1:
        one_am -= timedelta(days=1)
    eaten = one_am.astimezone(timezone.utc).replace(tzinfo=None)
    s = get_session()
    try:
        s.add(Meal(user_id=user.id, description="1am snack", calories=500, protein_g=20,
                   eaten_at=eaten, source="text", log_type="user_reported"))
        s.commit()
    finally:
        s.close()
    recompute_daily_totals(user.id)
    s = get_session()
    try:
        u = s.get(User, user.id)
        # if it's currently past 4am local, the 1am meal is in the PRIOR window → today 0
        # if it's currently before 4am local, the 1am meal is in THIS window → today 500
        start, end = local_day_bounds(u)
        in_today = start <= eaten < end
        assert u.calories_today == (500 if in_today else 0)
    finally:
        s.close()


def test_tool_sets_reset_and_clamps(db):
    from tests.factories import make_user
    from agent_tools import handle_set_day_reset
    from models import get_session, User
    user = make_user(db)
    out = handle_set_day_reset(user.id, {"hour": 4})
    assert out.startswith("ok") and "4am" in out
    s = get_session()
    try:
        assert s.get(User, user.id).day_reset_hour == 4
    finally:
        s.close()
    # back to midnight
    out0 = handle_set_day_reset(user.id, {"hour": 0})
    assert "midnight" in out0
    # reject out of range
    assert handle_set_day_reset(user.id, {"hour": 20}).startswith("error")
    assert handle_set_day_reset(user.id, {"hour": "x"}).startswith("error")
