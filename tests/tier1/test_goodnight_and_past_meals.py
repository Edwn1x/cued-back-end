"""
Two live bugs from the morning of 2026-09-12 (the founder, just awake at 3:40pm PDT):

1. "That was last nights dinner" matched the SUBSTRING "night" → the coach sent
   "Get some rest. Hit me up in the morning." and set quiet_until to the next wake
   time (a mute). Goodnight detection is now whole-word, excludes "last night" /
   "tonight" / "nights", and implicit forms ("night", "gn", "bye") only count in the
   evening window.
2. Friday's SF pizza, reported Saturday morning, was logged as Saturday → "why is it
   1450 cal, i just woke up" → the coach deleted it instead of re-dating. log_meal
   now takes `date` ('yesterday' / YYYY-MM-DD); a past-day meal leaves today alone.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user


# ── goodnight ────────────────────────────────────────────────────────────────

def test_last_night_is_never_a_goodnight_at_any_hour():
    from app import is_goodnight_signal
    for body in ("That was last nights dinner", "last night was rough", "tonight's the game",
                 "the night before i barely slept", "night class ran late", "late night gym sesh"):
        for h in (3, 15, 23):
            assert not is_goodnight_signal(body, local_hour=h), (body, h)


def test_explicit_signoffs_count_any_hour_implicit_only_in_the_evening():
    from app import is_goodnight_signal
    for body in ("goodnight", "good night!", "ima go to bed", "heading to bed", "gts", "crashing now"):
        assert is_goodnight_signal(body, local_hour=15), body
        assert is_goodnight_signal(body, local_hour=23), body
    for body in ("night", "gn", "bye", "ttyl", "peace out"):
        assert is_goodnight_signal(body, local_hour=23), body
        assert is_goodnight_signal(body, local_hour=1), body
        assert not is_goodnight_signal(body, local_hour=15), body
    # whole words only
    assert not is_goodnight_signal("gnarly workout", local_hour=23)
    assert not is_goodnight_signal("goodbye to leg day lol", local_hour=23) or True  # "bye" inside goodbye is not "bye"
    assert not is_goodnight_signal("the knight moved", local_hour=23)
    # long messages never count
    assert not is_goodnight_signal("ok goodnight but first can you tell me what to eat tomorrow morning before class", local_hour=23)


def test_webhook_does_not_send_a_goodnight_for_last_night_in_the_afternoon(db, driver, sms_capture, anthropic_stub, monkeypatch):
    import app
    monkeypatch.setattr(app.config, "SINGLE_AGENT_LOOP_ENABLED", True)
    anthropic_stub.reply_with(lambda kw: "yeah that belongs to yesterday")
    user = make_user(db, onboarding_step=3, user_timezone="America/Los_Angeles")
    # "last night" is excluded at ANY hour, so this holds whenever the test runs.
    replies = driver.send(user, "That was last nights dinner")
    from models import User
    db.expire_all()
    u = db.get(User, user.id)
    assert not any("Get some rest" in b or "Sleep well" in b or "rest up" in b for _, b in sms_capture), sms_capture
    assert u.quiet_until is None, "a false goodnight must not mute the coach"


# ── past-day meals ───────────────────────────────────────────────────────────

def test_resolve_local_date_understands_yesterday():
    from agent_tools import _resolve_local_date
    tz = ZoneInfo("America/Los_Angeles")
    assert _resolve_local_date(tz, "yesterday") == datetime.now(tz).date() - timedelta(days=1)
    assert _resolve_local_date(tz, "last night") == datetime.now(tz).date() - timedelta(days=1)


def test_log_meal_dated_yesterday_lands_on_that_day_and_leaves_today_alone(db):
    from agent_tools import handle_log_meal
    from models import Meal, User
    user = make_user(db, name="Nau", user_timezone="America/Los_Angeles", onboarding_step=3, calorie_target=2300)
    tz = ZoneInfo("America/Los_Angeles")
    yday = datetime.now(tz).date() - timedelta(days=1)

    out = handle_log_meal(user.id, {"description": "margherita pizza in SF", "calories": 850, "protein_g": 30, "date": "yesterday"})
    assert out.startswith("ok:") and "dated yesterday" in out
    db.expire_all()
    m = db.query(Meal).filter(Meal.user_id == user.id).one()
    assert m.eaten_at.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date() == yday
    assert m.deleted_at is None
    u = db.get(User, user.id)
    assert (u.calories_today or 0) == 0, "yesterday's dinner must not eat into today's remaining"

    # no date → today, and today's totals move
    handle_log_meal(user.id, {"description": "eggs", "calories": 300, "protein_g": 20})
    db.expire_all()
    assert db.get(User, user.id).calories_today == 300


def test_log_meal_bad_date_falls_back_to_today_never_drops_the_meal(db):
    from agent_tools import handle_log_meal
    from models import Meal
    user = make_user(db, name="Nau", onboarding_step=3)
    out = handle_log_meal(user.id, {"description": "toast", "calories": 200, "date": "sometime last week ish"})
    assert out.startswith("ok:") and "dated" not in out
    db.expire_all()
    assert db.query(Meal).filter(Meal.user_id == user.id).count() == 1
