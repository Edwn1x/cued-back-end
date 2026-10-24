"""
Live 2026-10-24 (Saturday) 13:07 PT, user 51: the briefing was "67° and clear out, good day
to get outside" + "gym's wide open all afternoon … nothing logged yet" while the parts carried
CS 70 HW due Wed 4pm (BRIEFING_DROPPED_ITEM item=due_soon). Nothing was due that day, so the
radar WAS the academic content — and it vanished. Code now appends dropped due-soon items
(within BRIEFING_DUE_SOON_RADAR_DAYS, at most two) as one bubble when nothing is due today.
A due-TODAY day already carries its own appended line and never gets both.
"""
from datetime import timedelta

import pytest


def _parts(**over):
    base = {"weather": "67° and clear", "due_today": [], "due_today_times": {},
            "due_soon": [("CS 70 HW Due", 4.1), ("cs70 Discussion (friday = quiz)", 6.1)],
            "due_soon_when": {"CS 70 HW Due": "wed by 4:00pm", "cs70 Discussion (friday = quiz)": "fri by 4:00pm"},
            "gym_window": None, "nutrition": None}
    base.update(over)
    return base


LIVE = "67° and clear out, good day to get outside\n---\ngym's wide open all afternoon if u wanna train. nothing logged yet, grab some protein when ur up"


def test_live_saturday_briefing_gets_the_near_due_soon_item_appended(caplog, monkeypatch):
    import logging, config, heartbeat
    monkeypatch.setattr(config, "BRIEFING_DUE_SOON_RADAR_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "BRIEFING_DUE_SOON_RADAR_DAYS", 5, raising=False)
    with caplog.at_level(logging.INFO):
        out = heartbeat._enforce_briefing(LIVE, _parts(), 51)
    assert out == LIVE + "\n---\nheads up: cs 70 hw due wed by 4:00pm"
    assert "BRIEFING_DUE_SOON_APPENDED" in caplog.text
    # the Friday quiz (6d out) sits beyond the radar horizon — not appended


def test_mentioned_due_soon_item_is_left_alone():
    import heartbeat
    msg = "67° and clear out. cs70 hw due wed, nothing today — gym's open all afternoon"
    assert heartbeat._enforce_briefing(msg, _parts(), 51) == msg


def test_a_due_today_day_gets_only_the_due_today_line_never_both():
    import heartbeat
    parts = _parts(due_today=["Homework 4 [CS61C]"], due_today_times={"Homework 4 [CS61C]": "11:59pm"})
    out = heartbeat._enforce_briefing("67° and clear out", parts, 51)
    assert out.endswith("also: homework 4 [cs61c] due today by 11:59pm")
    assert "heads up" not in out


def test_horizon_and_cap(monkeypatch):
    import config, heartbeat
    monkeypatch.setattr(config, "BRIEFING_DUE_SOON_RADAR_DAYS", 5, raising=False)
    parts = _parts(due_soon=[("Math 54 HW Due", 1.2), ("Bio 1A Lab Report Due", 2.5), ("Essay 2 [R1B] Due", 3.0)],
                   due_soon_when={"Math 54 HW Due": "sun", "Bio 1A Lab Report Due": "mon by 9:00am", "Essay 2 [R1B] Due": "tue"})
    out = heartbeat._enforce_briefing("67° and clear out", parts, 51)
    assert out.endswith("heads up: math 54 hw due sun + bio 1a lab report due mon by 9:00am"), out    # at most two
    parts = _parts(due_soon=[("Physics 7B Pset Due", 6.5)], due_soon_when={"Physics 7B Pset Due": "fri"})
    msg = "67° and clear out"
    assert heartbeat._enforce_briefing(msg, parts, 51) == msg                 # beyond the horizon → silent


def test_flag_off_leaves_the_drop_alone(monkeypatch):
    import config, heartbeat
    monkeypatch.setattr(config, "BRIEFING_DUE_SOON_RADAR_ENABLED", False, raising=False)
    msg = "67° and clear out"
    assert heartbeat._enforce_briefing(msg, _parts(), 51) == msg


def test_missing_when_falls_back_to_days():
    import heartbeat
    parts = _parts(due_soon=[("CS 70 HW Due", 4.1)], due_soon_when={})
    out = heartbeat._enforce_briefing("67° and clear out", parts, 51)
    assert out.endswith("heads up: cs 70 hw due in 4d")


def test_parts_carry_the_weekday_and_time_for_each_due_soon_item(db, monkeypatch):
    """_daily_briefing_parts fills due_soon_when from the event's local instant."""
    from tests.factories import make_user
    from tests.tier1.test_briefing_completeness import _mk_timed_due, _utc, DAY, PT
    import heartbeat
    monkeypatch.setattr(heartbeat.config, "WEATHER_ENABLED", False, raising=False)
    user = make_user(db, user_timezone="America/Los_Angeles")
    wed = (2026, 10, 7)                                   # the Wednesday after the live Friday DAY
    _mk_timed_due(user.id, "CS 70 HW Due", _utc(16, 0, day=wed).astimezone(PT))
    parts = heartbeat._daily_briefing_parts(user, db, now=_utc(13, 7))
    assert parts["due_today"] == []
    titles = [t for t, _ in parts["due_soon"]]
    assert "CS 70 HW Due" in titles
    assert parts["due_soon_when"]["CS 70 HW Due"] == "wed by 4:00pm"


def test_flags_default():
    import config
    assert config.BRIEFING_DUE_SOON_RADAR_ENABLED is True and config.BRIEFING_DUE_SOON_RADAR_DAYS == 5
