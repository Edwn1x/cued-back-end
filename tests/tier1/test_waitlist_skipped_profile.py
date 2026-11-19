"""
The site's sign-up form (2026-11-14) lets people skip everything but name, number,
occupation and consent. What they skipped must arrive NULL — not a default — and the
coach must ask for it over text (founder, 2026-11-15): a defaulted row read as a
full-gym beginner with a general-fitness goal and onboarding never asked.
"""

from __future__ import annotations

import json

import pytest


def _waitlist(client, phone, **extra):
    body = dict(name="Pat Lee", phone=phone, sms_consent=True, source="hero")
    body.update(extra)
    r = client.post("/waitlist", data=json.dumps(body), content_type="application/json")
    return r.status_code, r.get_json()


@pytest.fixture(autouse=True)
def _no_photon(monkeypatch):
    import photon
    monkeypatch.setattr(photon, "provision_user", lambda uid: False)
    import app as appmod
    from onboarding_agent import send_onboarding_hook
    monkeypatch.setattr(appmod, "start_onboarding",
                        lambda user, reason="start_onboarding": send_onboarding_hook(user.id, reason=reason))


def _user(db, phone):
    from models import User
    return db.query(User).filter(User.phone == phone).one()


def test_a_minimal_signup_stores_nulls_and_onboarding_asks_the_four_first(db, client):
    from onboarding_agent import _get_missing_fields
    code, data = _waitlist(client, "5105550921", occupation="nurse")
    assert code == 200 and data["status"] == "ok"
    u = _user(db, "+15105550921")
    assert (u.goal, u.experience, u.equipment, u.gender, u.age) == (None, None, None, None, None)
    names = [f for f, _ in _get_missing_fields(u)]
    assert names[:4] == ["goal", "experience", "equipment", "gender"]
    # the summary the prompts read never says "None"
    assert "None" not in u.profile_summary


def test_a_full_signup_is_not_asked_any_of_them(db, client):
    from onboarding_agent import _get_missing_fields
    code, _ = _waitlist(client, "5105550922", goal=["fat_loss"], experience="intermediate", equipment="home_gym",
                        gender="female", age="24", occupation="student at sjsu")
    assert code == 200
    u = _user(db, "+15105550922")
    names = [f for f, _ in _get_missing_fields(u)]
    assert not ({"goal", "experience", "equipment", "gender"} & set(names))


def test_the_extractor_can_now_fill_equipment_and_gender_and_ignores_junk(db, client):
    from onboarding_agent import _store_extracted_data, _get_missing_fields
    code, _ = _waitlist(client, "5105550923")
    assert code == 200
    u = _user(db, "+15105550923")
    _store_extracted_data(u.id, {"equipment": "Home_Gym", "gender": "prefer_not_to_say", "goal": "fat_loss",
                                 "experience": "beginner"})
    db.expire_all()
    u = _user(db, "+15105550923")
    assert (u.goal, u.experience, u.equipment, u.gender) == ("fat_loss", "beginner", "home_gym", "prefer_not_to_say")
    assert not ({"goal", "experience", "equipment", "gender"} & {f for f, _ in _get_missing_fields(u)})
    _store_extracted_data(u.id, {"equipment": "a treadmill", "gender": "yes"})
    db.expire_all()
    u = _user(db, "+15105550923")
    assert (u.equipment, u.gender) == ("home_gym", "prefer_not_to_say")


def test_the_calorie_math_and_workouts_survive_a_null_row(db, client):
    # Every consumer falls back on read — a NULL row must not raise anywhere the
    # defaults used to flow.
    code, _ = _waitlist(client, "5105550924", height_ft=5, height_in=9, weight_lbs=170, age="30")
    assert code == 200
    u = _user(db, "+15105550924")
    from macro_calculator import calculate_targets
    t = calculate_targets(u)
    assert t and int(t.get("calories") or t.get("calorie_target") or 0) > 1000
    assert "Goal:" not in u.profile_summary and "Experience:" not in u.profile_summary
