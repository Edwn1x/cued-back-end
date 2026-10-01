"""
/waitlist takes the objective stats from the form (founder, 2026-10-01): height,
weight, days, time, injuries, diet, restrictions, apps, steps — all optional, all
validated, never truncated. The old conversational onboarding then has nothing to ask
about any column the form filled (_get_missing_fields reads the row).

Live reason: user 47 texted "50 kg and 168 cm" twice to an extractor that only knew
feet; height stayed null and onboarding never closed. On the form, that's a number box.
"""

from __future__ import annotations

import json

import pytest


def _waitlist(client, phone, **extra):
    body = dict(name="Pat Lee", phone=phone, sms_consent=True, age="22", gender="female",
                goal=["muscle_building"], experience="none", equipment="full_gym", source="hero")
    body.update(extra)
    r = client.post("/waitlist", data=json.dumps(body), content_type="application/json")
    return r.status_code, r.get_json()


@pytest.fixture(autouse=True)
def _no_photon(monkeypatch):
    import photon
    monkeypatch.setattr(photon, "provision_user", lambda uid: False)


def test_metric_form_fills_the_imperial_columns(db, client):
    from models import User
    code, data = _waitlist(client, "5105550901", height_cm=168, weight_kg=50)
    assert code == 200 and data["status"] == "ok"
    u = db.query(User).filter(User.phone == "+15105550901").one()
    assert (u.height_ft, u.height_in, u.weight_lbs) == (5, 6, 110.0)


def test_full_form_fills_every_column_and_leaves_the_coach_nothing_objective_to_ask(db, client):
    from models import User
    from onboarding_agent import _get_missing_fields
    code, _ = _waitlist(client, "5105550902", height_ft="5", height_in="6", weight_lbs="120",
                        workout_days=["Mon", "Tue", "Thu"], workout_time="morning", injuries="",
                        diet="none", restrictions=["peanuts", "shellfish"],
                        existing_tools=["Apple Watch", "Strava"], avg_steps="6000")
    assert code == 200
    u = db.query(User).filter(User.phone == "+15105550902").one()
    assert (u.height_ft, u.height_in, u.weight_lbs) == (5, 6, 120.0)
    assert (u.workout_days, u.workout_time, u.injuries) == ("mon,tue,thu", "08:00", "none")
    assert (u.diet, u.restrictions) == ("omnivore", "peanuts; shellfish")
    assert (u.existing_tools, u.tools_decision, u.avg_steps) == ("apple_watch,strava", "acknowledged", 6000)
    # what the OLD conversational intake still wants from this person: only the
    # context fields — occupation, activity, split (experience=none → auto 'none'), cooking, wake/sleep
    left = [f for f, _ in _get_missing_fields(u)]
    assert "height_weight" not in left and "workout_days" not in left and "workout_time" not in left
    assert "diet" not in left and "existing_tools" not in left and "avg_steps" not in left and "injuries" not in left
    assert set(left) <= {"occupation", "activity_level", "cooking_situation", "wake_sleep"}


def test_no_tools_is_an_answer(db, client):
    from models import User
    _waitlist(client, "5105550903", existing_tools="none")
    u = db.query(User).filter(User.phone == "+15105550903").one()
    assert (u.existing_tools, u.tools_decision) == ("none", "none")


def test_numeric_days_and_clock_time(db, client):
    from models import User
    _waitlist(client, "5105550904", workout_days=4, workout_time="17:30")
    u = db.query(User).filter(User.phone == "+15105550904").one()
    assert (u.workout_days, u.workout_time) == ("4", "17:30")


def test_form_without_stats_is_unchanged(db, client):
    from models import User
    code, _ = _waitlist(client, "5105550905")
    assert code == 200
    u = db.query(User).filter(User.phone == "+15105550905").one()
    assert all(getattr(u, c) is None for c in ("height_ft", "weight_lbs", "workout_days", "workout_time",
                                               "injuries", "diet", "restrictions", "existing_tools", "avg_steps"))


@pytest.mark.parametrize("bad, msg", [
    ({"height_cm": 20}, "That height doesn't look right."),
    ({"height_ft": 9}, "That height doesn't look right."),
    ({"height_ft": 5, "height_in": 14}, "That height doesn't look right."),
    ({"weight_kg": 5}, "That weight doesn't look right."),
    ({"weight_lbs": "heavy"}, "That weight lbs doesn't look right."),
    ({"workout_days": "9"}, "Those training days don't look right."),
    ({"workout_days": "mon,funday"}, "Those training days don't look right."),
    ({"workout_time": "25:00"}, "That training time doesn't look right."),
    ({"workout_time": "whenever"}, "That training time doesn't look right."),
    ({"diet": "carnivore"}, "That diet doesn't look right."),
    ({"avg_steps": 99999}, "That step count doesn't look right."),
    ({"injuries": "x" * 501}, "That injuries note is too long."),
])
def test_nonsense_is_rejected_never_truncated(db, client, bad, msg):
    from models import User
    code, data = _waitlist(client, "5105550906", **bad)
    assert code == 400 and data["message"] == msg
    assert db.query(User).filter(User.phone == "+15105550906").first() is None


@pytest.mark.parametrize("cm, want", [(168, (5, 6)), (152, (5, 0)), (183, (6, 0)), (200, (6, 7)), (120, (None, None)), ("175,5", (5, 9))])
def test_cm_to_ft_in(cm, want):
    from signup_stats import cm_to_ft_in
    assert cm_to_ft_in(cm) == want


@pytest.mark.parametrize("kg, want", [(50, 110.0), (70, 154.0), (100, 220.0), (5, None), ("62.5", 138.0)])
def test_kg_to_lbs(kg, want):
    from signup_stats import kg_to_lbs
    assert kg_to_lbs(kg) == want
