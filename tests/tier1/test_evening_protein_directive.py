"""
The evening meal is where the protein gap gets said — and the tool result now says which
log that is.

Live 2026-10-28 18:16 PT (user 77, Alexey): dinner photo logged at 1345 cal / 59g, the day at
1960 / 88g against 2400 / 132g — 44g short with the last meal of the day on the plate. Both
reply bubbles gave calories only. voice.md already says the gap belongs "at the evening meal",
and the DAY TOTAL NOW suffix already carries "44g protein left" with "for … the evening
meal" — but nothing told the model THIS was the evening meal, so the once-a-day line never
came. Code knows the local hour; it now says so.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")


def _at(h, mi=0):
    """Today (PT) at h:mi, naive UTC — today so the stored day totals are the ones read."""
    d = datetime.now(PT).date()
    return datetime(d.year, d.month, d.day, h, mi, tzinfo=PT).astimezone(timezone.utc).replace(tzinfo=None)


def _log(user_id, cal, pro):
    from agent_tools import handle_log_meal
    return handle_log_meal(user_id, {"description": "kofta plate", "calories": cal, "protein_g": pro,
                                     "carbs_g": 100, "fat_g": 40})


def test_knob_defaults():
    assert config.EVENING_PROTEIN_NUDGE_ENABLED is True
    assert config.EVENING_MEAL_LOCAL_HOUR == 17
    assert config.EVENING_PROTEIN_GAP_MIN_G == 20


def test_dinner_log_with_a_real_gap_carries_the_directive(db):
    from agent_tools import _day_total_suffix
    user = make_user(db, calorie_target=2400, protein_target=132)
    _log(user.id, 615, 29)
    _log(user.id, 1345, 59)
    out = _day_total_suffix(user.id, now=_at(18, 16))
    assert "DAY TOTAL NOW: 1960 cal, 88g protein, 44g protein left of 132" in out, out
    assert "EVENING MEAL" in out and "44g short" in out, out
    assert "say the gap now" in out and "one concrete way" in out.lower(), out


def test_lunch_log_stays_quiet(db):
    from agent_tools import _day_total_suffix
    user = make_user(db, protein_target=132)
    _log(user.id, 615, 29)
    out = _day_total_suffix(user.id, now=_at(12, 24))
    assert "103g protein left of 132" in out and "EVENING MEAL" not in out, out
    assert "not a line after every log" in out


def test_a_small_gap_at_dinner_stays_quiet(db):
    from agent_tools import _day_total_suffix
    user = make_user(db, protein_target=132)
    _log(user.id, 2000, 120)
    out = _day_total_suffix(user.id, now=_at(19, 0))
    assert "12g protein left" in out and "EVENING MEAL" not in out, out


def test_gap_threshold_scales_with_the_target(db, monkeypatch):
    """20g is the floor; on a big target the bar is 15% of it (a 220g target: 33g)."""
    from agent_tools import _day_total_suffix
    a = make_user(db, protein_target=220)
    _log(a.id, 2000, 195)                                      # 25g left: above 20g, under 33g
    assert "EVENING MEAL" not in _day_total_suffix(a.id, now=_at(19, 0))
    b = make_user(db, protein_target=220)
    _log(b.id, 2000, 185)                                      # 35g left
    assert "EVENING MEAL" in _day_total_suffix(b.id, now=_at(19, 0))


def test_the_hour_is_the_users_local_hour(db):
    from agent_tools import _day_total_suffix
    tokyo = make_user(db, user_timezone="Asia/Tokyo", protein_target=132)
    _log(tokyo.id, 600, 30)
    # 18:16 PT is 10:16 Tokyo the next morning — not their evening
    assert "EVENING MEAL" not in _day_total_suffix(tokyo.id, now=_at(18, 16))
    # 02:16 PT = 18:16 Tokyo
    assert "EVENING MEAL" in _day_total_suffix(tokyo.id, now=_at(2, 16))


def test_no_target_or_flag_off_is_unchanged(db, monkeypatch):
    from agent_tools import _day_total_suffix
    user = make_user(db, protein_target=None)
    _log(user.id, 600, 30)
    out = _day_total_suffix(user.id, now=_at(19, 0))
    assert out.endswith("use this exact number") and "EVENING MEAL" not in out, out
    monkeypatch.setattr(config, "EVENING_PROTEIN_NUDGE_ENABLED", False)
    user2 = make_user(db, protein_target=132)
    _log(user2.id, 600, 30)
    out2 = _day_total_suffix(user2.id, now=_at(19, 0))
    assert "102g protein left of 132" in out2 and "EVENING MEAL" not in out2, out2


def test_the_log_tool_result_itself_carries_it(db, monkeypatch):
    """End to end through handle_log_meal: the suffix the model reads after the dinner log."""
    import agent_tools
    monkeypatch.setattr(agent_tools, "_naive_utcnow", lambda: _at(18, 16))
    user = make_user(db, protein_target=132)
    _log(user.id, 615, 29)
    out = _log(user.id, 1345, 59)
    assert "EVENING MEAL" in out and "44g short" in out, out


def test_past_day_totals_never_carry_it(db):
    from datetime import date, timedelta
    from agent_tools import _day_total_suffix
    user = make_user(db, protein_target=132)
    y = datetime.now(PT).date() - timedelta(days=1)
    assert "EVENING MEAL" not in _day_total_suffix(user.id, y, now=_at(19, 0))
