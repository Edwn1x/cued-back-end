"""
Per-day / multi-select training time (founder's change 6, 2026-10-09). The form may send
one value, a list, or a day→time map; users.workout_time stays the primary clock for
every older reader; users.workout_times holds the structure; gym beats use the slot for
TODAY; the summary and profile say it in words.
"""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user
from training_time import parse_form_value, describe, slot_for


@pytest.mark.parametrize("raw, want", [
    ("evening", ("18:00", None)),
    ("17:30", ("17:30", None)),
    (["morning"], ("08:00", None)),
    (["morning", "evening"], ("08:00", {"slots": ["08:00", "18:00"]})),
    (["evening", "evening", "17:30"], ("18:00", {"slots": ["18:00", "17:30"]})),
    ({"mon": "morning", "wed": "morning", "tue": "evening", "thu": "evening"},
     ("08:00", {"by_day": {"mon": "08:00", "wed": "08:00", "tue": "18:00", "thu": "18:00"}})),
    ({"Monday": "evening", "Thursday": "evening"}, ("18:00", None)),       # same time every day → just the primary
    ({"mon": "morning", "tue": "evening", "thu": "evening"}, ("18:00", {"by_day": {"mon": "08:00", "tue": "18:00", "thu": "18:00"}})),
    ("whenever", None), (["morning", "25:00"], None), ({"funday": "morning"}, None), ([], None), ({}, None),
])
def test_parse_form_value(raw, want):
    assert parse_form_value(raw) == want


@pytest.fixture(autouse=True)
def _no_photon(monkeypatch):
    import photon
    monkeypatch.setattr(photon, "provision_user", lambda uid: False)


def _post(client, phone, **stats):
    body = dict(name="Pat Lee", phone=phone, sms_consent=True, age="22", gender="female",
                goal=["muscle_building"], experience="none", equipment="full_gym", source="hero", **stats)
    return client.post("/waitlist", data=json.dumps(body), content_type="application/json")


def _u(db, phone):
    from models import User
    db.expire_all()
    return db.query(User).filter(User.phone.like(f"%{phone[-7:]}")).first()


def test_form_list_and_map_land_in_both_columns(db, client):
    assert _post(client, "5105550931", workout_time=["morning", "evening"]).status_code in (200, 201)
    u = _u(db, "5105550931")
    assert (u.workout_time, u.workout_times) == ("08:00", {"slots": ["08:00", "18:00"]})
    assert _post(client, "5105550932", workout_times={"mon": "morning", "tue": "17:30", "thu": "17:30"}).status_code in (200, 201)
    v = _u(db, "5105550932")
    assert v.workout_time == "17:30" and v.workout_times == {"by_day": {"mon": "08:00", "tue": "17:30", "thu": "17:30"}}
    import onboarding_agent as oa
    assert "workout_time" not in [f[0] for f in oa._get_missing_fields(v)]


def test_form_rejects_a_bad_entry_in_a_list(db, client):
    r = _post(client, "5105550933", workout_time=["morning", "whenever"])
    assert r.status_code == 400 and r.get_json()["message"] == "That training time doesn't look right."


def test_describe_and_slot_for(db):
    by_day = make_user(db, workout_time="08:00", workout_times={"by_day": {"mon": "08:00", "wed": "08:00", "tue": "18:00", "thu": "17:30"}})
    assert describe(by_day) == "mornings mon/wed, evenings tue, 5:30pm thu"
    assert slot_for(by_day, "tue") == "18:00" and slot_for(by_day, "thu") == "17:30" and slot_for(by_day, "fri") == "08:00"
    slots = make_user(db, workout_time="08:00", workout_times={"slots": ["08:00", "18:00"]})
    assert describe(slots) == "mornings or evenings" and slot_for(slots, "mon") == "08:00"
    plain = make_user(db, workout_time="18:00", workout_times=None)
    assert describe(plain) == "in the evenings"
    clock = make_user(db, workout_time="17:30", confirmed_workout_time=None, workout_times=None)
    assert describe(clock) == "at 5:30pm"
    assert describe(make_user(db, workout_time=None, confirmed_workout_time=None)) == ""


def test_summary_says_the_days_and_times(db):
    import onboarding_agent as oa
    u = make_user(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=139, goal="fat_loss", workout_days="4",
                  workout_time="08:00", workout_times={"by_day": {"mon": "08:00", "wed": "08:00", "tue": "18:00", "thu": "18:00"}},
                  wake_time=None, sleep_time=None)
    s = oa._build_confirmation_summary(u)
    assert s.startswith("ok so far this is what i have: ur 5'6 139, training 4 days a week, mornings mon/wed, evenings tue/thu, tryna lose fat"), s
    sp = oa._build_system_prompt(u)
    assert "Workout time: mornings mon/wed, evenings tue/thu" in sp


def test_gym_beat_uses_todays_slot(db):
    from gym_beats import session_within
    u = make_user(db, workout_time="08:00", confirmed_workout_time=None,
                  workout_times={"by_day": {"mon": "08:00", "tue": "18:00"}})
    tz = ZoneInfo("America/Los_Angeles")
    mon_7am = datetime(2026, 10, 12, 7, 0, tzinfo=tz)      # a Monday
    tue_7am = datetime(2026, 10, 13, 7, 0, tzinfo=tz)      # a Tuesday: they lift at 6pm, not within 2h
    tue_5pm = datetime(2026, 10, 13, 17, 0, tzinfo=tz)
    assert session_within(u, 2.0, mon_7am) is True
    assert session_within(u, 2.0, tue_7am) is False
    assert session_within(u, 2.0, tue_5pm) is True


def test_profile_page_shows_the_words_and_the_structure(db):
    from profile_page import build_profile_payload
    from models import get_session, User
    u = make_user(db, workout_time="08:00", workout_times={"slots": ["08:00", "18:00"]})
    s = get_session()
    try:
        p = build_profile_payload(s, s.get(User, u.id))
    finally:
        s.close()
    assert p["training"]["time"] == "mornings or evenings" and p["training"]["times"] == {"slots": ["08:00", "18:00"]}


def test_column_is_migrated():
    assert "workout_times JSON" in open("migrate.py").read()
