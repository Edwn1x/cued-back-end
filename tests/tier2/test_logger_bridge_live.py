"""
Tier-2 (live) anchors for the logger bridge (rewrite/logger-bridge/CHANGESPEC.md).
Binary, parametrized x3 per the founder rule. The fixture is a rendered
MyNetDiary-style breakfast (tests/fixtures/diary_breakfast.png): 8 printed lines,
total 551 / 53.5g / 44.4g / 16.7g.

  a1  fresh day + the screenshot → ONE log_meal with from_app, calories within the
      printed numbers (sum 551 ± 5), protein 53/54 (never a guess like 39), reply short.
  a2  the Sep 19 replay: a photo-estimated "chicken + egg sandwich 430" already logged at
      8am local, then the screenshot → one row remains, calories 551 on it, day total 551,
      reply quotes 551 (never ~980) and doesn't argue with the app.
  a3  coexisting user, empty day: 09:00 local tick → silent with a reason that names the
      other app (or speaks about something that isn't food); 21:00 local tick with no
      screenshot today → at most one short ask that mentions a screenshot / the app.
  a4  "ok im deleting mynetdiary, just using u now" → set_food_logger(status=switched).
Run: pytest tests/tier2/test_logger_bridge_live.py --run-tier2 -q -p no:cacheprovider
"""

from __future__ import annotations

import base64
import os
import re
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.tier2

_FIXDIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

AISLINN = dict(name="aislinn", onboarding_step=3, equipment="bodyweight", current_split="none",
               workout_days="3-4", workout_time="18:00", wake_time="06:50", sleep_time="00:00",
               height_ft=5, height_in=2, weight_lbs=172.6, age=16, gender="female", goal="fat_loss",
               avg_steps=4000, user_timezone="America/Los_Angeles",
               calorie_target=1750, protein_target=137, existing_tools="mynetdiary")


def _img(name) -> dict:
    with open(os.path.join(_FIXDIR, name), "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _enable(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "READ_IMAGE_ENABLED", "MEAL_ESTIMATION_PROMPT_ENABLED",
              "MEAL_ROUTING_PROMPT_ENABLED", "LOG_MEAL_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED",
              "REMEMBER_TOOL_ENABLED", "FOOD_LOGGER_BRIDGE_ENABLED", "SET_FOOD_LOGGER_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tz_at(local_hour: int) -> str:
    """A fixed-offset zone where the user's local time is ~local_hour now."""
    offset = (local_hour - datetime.now(timezone.utc).hour) % 24
    if offset > 14:
        offset -= 24
    return f"Etc/GMT-{offset}" if offset >= 0 else f"Etc/GMT+{-offset}"


def _meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).order_by(Meal.id).all()
    finally:
        s.close()


def _fresh(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


@pytest.mark.parametrize("run", [1, 2, 3])
def test_a1_screenshot_logs_printed_numbers_not_estimates(db, monkeypatch, run):
    from agent_loop import run_agent_loop
    from tests.factories import make_user
    _enable(monkeypatch)
    user = make_user(db, **dict(AISLINN, user_timezone=_tz_at(9)))
    reply = run_agent_loop(_fresh(user.id), "(the user sent an image)", "freeform", image_data=_img("diary_breakfast.png"))
    rows = _meals(user.id)
    assert rows, f"nothing logged — reply: {reply!r}"
    assert all(r.source == "app" for r in rows), f"not app-sourced: {[(r.description, r.source) for r in rows]}"
    total = sum(r.calories or 0 for r in rows)
    assert 546 <= total <= 556, f"calories not the printed 551: {total} — {[(r.description, r.calories) for r in rows]}"
    pro = sum(r.protein_g or 0 for r in rows)
    assert pro == 0 or 50 <= pro <= 56, f"protein is a guess, not the printed 53.5: {pro}"
    assert _fresh(user.id).food_logger_status == "coexist"
    low = (reply or "").lower()
    assert not re.search(r"\b39\b", low) and "551" in low.replace(",", ""), f"reply: {reply!r}"
    assert len(reply) < 320, f"reply is a recap, not a line: {reply!r}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_a2_sep19_replay_replaces_the_estimate(db, monkeypatch, run):
    from agent_loop import run_agent_loop
    from models import get_session, Meal, Message
    from timefmt import local_day_bounds, resolve_tz
    from tests.factories import make_user
    _enable(monkeypatch)
    user = make_user(db, **dict(AISLINN, user_timezone=_tz_at(13)))
    u = _fresh(user.id)
    start, _ = local_day_bounds(u)
    eight = (start.replace(tzinfo=timezone.utc).astimezone(resolve_tz(u)).replace(hour=8)
             .astimezone(timezone.utc).replace(tzinfo=None))
    s = get_session()
    try:
        s.add(Meal(user_id=user.id, description="chicken + egg sandwich: 2 slices whole grain toast, grilled chicken ~3oz, fried egg, cheese slice, lettuce",
                   calories=430, protein_g=43, carbs_g=30, fat_g=17, source="photo", log_type="user_reported",
                   eaten_at=eight, logged_at=eight))
        s.add(Message(user_id=user.id, direction="in", body="[image attached]", message_type="freeform", created_at=eight))
        s.add(Message(user_id=user.id, direction="out", body="logged it, ~430 cal 43g protein", message_type="food_photo",
                      created_at=eight + timedelta(minutes=1)))
        s.add(Message(user_id=user.id, direction="in", body="Can I connect my calorie tracking app?", message_type="freeform",
                      created_at=_now() - timedelta(minutes=3)))
        s.add(Message(user_id=user.id, direction="out", body="no connection, but screenshot ur day and i'll take it from there",
                      message_type="freeform", created_at=_now() - timedelta(minutes=2)))
        s.commit()
    finally:
        s.close()
    reply = run_agent_loop(_fresh(user.id), "(the user sent an image)", "freeform", image_data=_img("diary_breakfast.png"))
    rows = _meals(user.id)
    assert len(rows) == 1, f"double-logged: {[(r.id, r.description[:30], r.calories, r.source) for r in rows]} — reply: {reply!r}"
    assert rows[0].source == "app" and 546 <= (rows[0].calories or 0) <= 556, (rows[0].calories, rows[0].source)
    assert _fresh(user.id).calories_today in range(546, 557)
    low = (reply or "").lower().replace(",", "")
    assert not re.search(r"\b9[5-9]\d\b|\b1[0-1]\d\d\b", low), f"reply carries the double-counted total: {reply!r}"
    assert "551" in low, f"reply doesn't quote the app's number: {reply!r}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_a3_coexist_empty_day_morning_silent_evening_one_ask(db, monkeypatch, run):
    import heartbeat
    from food_logger import set_food_logger
    from tests.factories import make_user
    from models import get_session, Workout, WeightLog
    _enable(monkeypatch)

    def _trained_yesterday(uid):
        # remove the other standing conditions (training gap, weigh-in due) so the empty
        # food day is the ONLY material the tick has
        s = get_session()
        try:
            s.add(Workout(user_id=uid, workout_type="full_body", completed=True, date=_now() - timedelta(days=1)))
            s.add(WeightLog(user_id=uid, weighed_at=_now() - timedelta(hours=1), weight_lbs=172.6))
            s.commit()
        finally:
            s.close()
    # morning
    user = make_user(db, **dict(AISLINN, user_timezone=_tz_at(9)), created_at=_now() - timedelta(days=6))
    set_food_logger(user.id, "mynetdiary", "coexist")
    _trained_yesterday(user.id)
    spoke, payload, _ = heartbeat.decide(user.id)
    low = (payload or "").lower()
    _FOOD_NUDGE = r"what (did|have|r|are) (you|u) (eat|ate|had|having)|log (ur|your|it|that|breakfast|food|meal)|haven'?t logged|nothing logged|not logged|breakfast\?|eaten yet|eat yet"
    if spoke:
        assert not re.search(_FOOD_NUDGE, low), f"morning food nudge on a coexist user: {payload!r}"
    else:
        # silence is the right outcome; the only wrong silence frames the empty log as a gap
        assert not re.search(r"(haven'?t|hasn'?t|nothing|not|no meals?) (been )?logged|unlogged|empty (food|day|log)|no food (logged|yet)", low), \
            f"silent but treating the empty day as a gap: {payload!r}"
    # evening, no screenshot today
    user2 = make_user(db, **dict(AISLINN, user_timezone=_tz_at(21)), created_at=_now() - timedelta(days=6))
    set_food_logger(user2.id, "mynetdiary", "coexist")
    _trained_yesterday(user2.id)
    spoke2, payload2, _ = heartbeat.decide(user2.id)
    low2 = (payload2 or "").lower()
    if spoke2 and re.search(r"eat|ate|food|log|meal|protein|cal|dinner|lunch", low2):
        # a food-related evening text must be THE one allowed ask, never a re-type
        assert re.search(r"screenshot|mynetdiary|app|ur day|your day", low2), f"evening food text isn't the one allowed ask: {payload2!r}"
        assert len(payload2) < 200 and payload2.count("?") <= 1, payload2
        assert not re.search(r"what did you eat|what have you eaten|re-?type|type out", low2), payload2
    # silence in the evening is allowed too (quiet hours start at 21 in config) — never a re-type ask
    else:
        assert not re.search(r"what did you eat|what have you eaten", low2)


@pytest.mark.parametrize("run", [1, 2, 3])
def test_a4_deleting_the_app_switches_state(db, monkeypatch, run):
    from agent_loop import run_agent_loop
    from food_logger import set_food_logger
    from tests.factories import make_user
    _enable(monkeypatch)
    user = make_user(db, **dict(AISLINN, user_timezone=_tz_at(14)))
    set_food_logger(user.id, "mynetdiary", "coexist")
    reply = run_agent_loop(_fresh(user.id), "ok im deleting mynetdiary, just using u now", "freeform")
    u = _fresh(user.id)
    assert u.food_logger_status == "switched", f"state not switched — reply: {reply!r}"
    low = (reply or "").lower()
    assert len(reply) < 260 and not re.search(r"feature|here's what i can|i can also", low), f"gloat/feature list: {reply!r}"
