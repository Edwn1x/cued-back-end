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


# ---------------------------------------------------------------------------
# Auto day boundary from an after-midnight bedtime (2026-10-06, user 48: "980 for the
# day" at 12:22am → "no add it to yesterday"). The rhythm was already in the profile.
# ---------------------------------------------------------------------------

class _S:
    """A user with a profile bedtime; day_reset_hour unset unless given."""
    user_timezone = "America/Los_Angeles"
    def __init__(self, sleep=None, reset=0):
        self.sleep_time = sleep
        self.day_reset_hour = reset


@pytest.mark.parametrize("sleep, expect", [
    ("03:00", 4),     # the founder: bed ~3am → day rolls at 4am
    ("00:30", 1),
    ("10:00", 11),    # capped at 11
    ("11:00", 0),     # 11am is not an after-midnight bedtime
    ("23:30", 0),     # evening bedtime → standard day, exactly as before
    ("21:00", 0),
    ("around 3", 0),  # a phrase is not a clock → no derivation
    (None, 0),
])
def test_auto_reset_derives_from_an_after_midnight_bedtime(sleep, expect):
    from timefmt import day_reset_hour, auto_day_reset_hour
    assert auto_day_reset_hour(_S(sleep)) == expect
    assert day_reset_hour(_S(sleep)) == expect


def test_explicit_reset_beats_the_derived_hour():
    from timefmt import day_reset_hour, day_reset_source
    assert day_reset_hour(_S("03:00", reset=2)) == 2
    assert day_reset_source(_S("03:00", reset=2)) == "explicit"


def test_explicit_midnight_sentinel_pins_the_standard_day():
    from timefmt import day_reset_hour, day_reset_source, EXPLICIT_MIDNIGHT
    assert day_reset_hour(_S("03:00", reset=EXPLICIT_MIDNIGHT)) == 0
    assert day_reset_source(_S("03:00", reset=EXPLICIT_MIDNIGHT)) == "explicit"
    assert day_reset_source(_S("03:00")) == "auto"
    assert day_reset_source(_S("23:00")) == "default"


def test_auto_reset_flag_off_keeps_midnight(monkeypatch):
    import config
    from timefmt import day_reset_hour
    monkeypatch.setattr(config, "NUTRITION_DAY_AUTO_RESET_ENABLED", False)
    assert day_reset_hour(_S("03:00")) == 0


def test_describe_day_reset_names_the_rollover_and_why():
    from timefmt import describe_day_reset, EXPLICIT_MIDNIGHT
    assert describe_day_reset(_S("23:00")) == "local midnight"
    auto = describe_day_reset(_S("03:00"))
    assert auto.startswith("4am local") and "4am→4am" in auto and "3:00 bedtime" in auto
    assert "they asked" in describe_day_reset(_S("03:00", reset=5))
    assert describe_day_reset(_S("03:00", reset=EXPLICIT_MIDNIGHT)) == "local midnight"


def test_founder_scenario_post_midnight_burger_counts_for_the_evening_before():
    """Sun 18:37 burger+fries, Mon 00:11 cheeseburger, bed ~3am: at 00:22 Monday the
    running day is still Sunday and holds both — no 'add it to yesterday' needed."""
    from timefmt import local_day_bounds
    from agent_tools import _nutrition_day_of
    from datetime import date
    u = _S("03:00")
    sunday_dinner = _at_pdt(2026, 10, 5, 18, 37)
    monday_0011 = _at_pdt(2026, 10, 6, 0, 11)
    now = _at_pdt(2026, 10, 6, 0, 22).replace(tzinfo=timezone.utc)
    start, end = local_day_bounds(u, now=now)
    assert start == _at_pdt(2026, 10, 5, 4, 0) and end == _at_pdt(2026, 10, 6, 4, 0)
    assert start <= sunday_dinner < end and start <= monday_0011 < end
    assert _nutrition_day_of(u, monday_0011) == date(2026, 10, 5)
    # and by 9am Monday a new day has started
    s2, _ = local_day_bounds(u, now=_at_pdt(2026, 10, 6, 9, 0).replace(tzinfo=timezone.utc))
    assert s2 == _at_pdt(2026, 10, 6, 4, 0)


def test_tool_zero_pins_midnight_against_the_derived_hour(db):
    from tests.factories import make_user
    from agent_tools import handle_set_day_reset
    from models import get_session, User
    from timefmt import day_reset_hour, EXPLICIT_MIDNIGHT
    user = make_user(db, sleep_time="03:00")
    s = get_session()
    try:
        assert day_reset_hour(s.get(User, user.id)) == 4          # derived
    finally:
        s.close()
    out = handle_set_day_reset(user.id, {"hour": 0})
    assert "midnight" in out and "pinned" in out and "4am" in out
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert u.day_reset_hour == EXPLICIT_MIDNIGHT and day_reset_hour(u) == 0
    finally:
        s.close()
    assert handle_set_day_reset(user.id, {"hour": 6}).startswith("ok")
    s = get_session()
    try:
        assert day_reset_hour(s.get(User, user.id)) == 6
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Re-dating a small-hours meal to an earlier day lands just before that day's rollover,
# not a full 24h earlier (live: the 12:11am burger was stamped 12:11am the day BEFORE).
# ---------------------------------------------------------------------------

def test_small_hours_row_moved_to_yesterday_lands_at_2359_on_a_midnight_day():
    from agent_tools import _moved_eaten_at
    from zoneinfo import ZoneInfo
    from datetime import date
    tz = ZoneInfo("America/Los_Angeles")
    u = _S("23:00")                                  # standard midnight day
    old = _at_pdt(2026, 10, 6, 0, 11)
    assert _moved_eaten_at(old, tz, date(2026, 10, 5), u) == _at_pdt(2026, 10, 5, 23, 59)


def test_row_already_in_the_target_window_is_left_alone_under_a_shifted_day():
    from agent_tools import _moved_eaten_at
    from zoneinfo import ZoneInfo
    from datetime import date
    tz = ZoneInfo("America/Los_Angeles")
    u = _S("03:00")                                  # auto 4am day: 00:11 Mon is already Sunday
    old = _at_pdt(2026, 10, 6, 0, 11)
    assert _moved_eaten_at(old, tz, date(2026, 10, 5), u) == old


def test_daytime_row_keeps_its_clock_when_re_dated():
    from agent_tools import _moved_eaten_at
    from zoneinfo import ZoneInfo
    from datetime import date
    tz = ZoneInfo("America/Los_Angeles")
    old = _at_pdt(2026, 10, 6, 13, 0)
    assert _moved_eaten_at(old, tz, date(2026, 10, 5), _S("23:00")) == _at_pdt(2026, 10, 5, 13, 0)
    assert _moved_eaten_at(old, tz, date(2026, 10, 5), None) == _at_pdt(2026, 10, 5, 13, 0)


def test_manage_log_move_to_yesterday_uses_the_rule(db, monkeypatch):
    """End to end through manage_log: a 00:11 row on a midnight-day user moved to
    'yesterday' lands at 23:59 yesterday and yesterday's total is reported."""
    import config
    from tests.factories import make_user
    from models import get_session, Meal
    from agent_tools import handle_manage_log
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(config, "MEAL_DAY_MOVE_ENABLED", True)
    user = make_user(db, sleep_time="23:00")
    tz = ZoneInfo("America/Los_Angeles")
    now_local = datetime.now(timezone.utc).astimezone(tz)
    # a row at 00:11 local TODAY
    row_local = now_local.replace(hour=0, minute=11, second=0, microsecond=0)
    eaten = row_local.astimezone(timezone.utc).replace(tzinfo=None)
    s = get_session()
    try:
        m = Meal(user_id=user.id, description="cheeseburger", calories=600, protein_g=30,
                 eaten_at=eaten, source="photo", log_type="user_reported")
        s.add(m); s.commit(); mid = m.id
    finally:
        s.close()
    out = handle_manage_log(user.id, {"entity": "meal", "action": "edit", "id": mid,
                                      "fields": {"date": "yesterday"}})
    assert out.startswith("ok"), out
    s = get_session()
    try:
        moved = s.get(Meal, mid).eaten_at.replace(tzinfo=timezone.utc).astimezone(tz)
        assert (moved.hour, moved.minute) == (23, 59)
        assert moved.date() == (row_local.date() - timedelta(days=1))
    finally:
        s.close()
