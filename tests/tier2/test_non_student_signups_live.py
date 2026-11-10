"""
Tier-2 (live) anchors for the non-student sign-up fixes (2026-11-05, user 93). Binary, x3.

  1. A 28-year-old with no occupation on the form answers the hook ("hey cued" / a line about
     her day) → the reply never assumes a student: no berkeley, no major, no campus, no class.
     It asks ONE thing, and that thing is about her work or her day.
  2. Control: the 20-year-old still gets the stated Berkeley assumption
     (test_onboarding_restructure_1_live.test_first_reply_states_the_berkeley_assumption).
Run: pytest tests/tier2/test_non_student_signups_live.py --run-tier2 -s
"""
from __future__ import annotations

import re

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import INTAKE

pytestmark = pytest.mark.tier2

CAMPUS_RE = re.compile(r"\b(berkeley|cal\b|student|major|campus|class(es)?|dining hall|rsf|midterm|semester|dorm)\b", re.I)
HOOK = ("hey karina, it's cued. ur in. quick setup before i can actually help: gonna get to know u a bit over "
        "the next few texts, then run u through everything i can do. how's ur day going")


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    for f in ("ONBOARDING_RUNDOWN_ENABLED", "CARD_SETUP_ENABLED", "WATER_OFFER_ENABLED", "PHOTON_PROVISIONING_ENABLED"):
        monkeypatch.setattr(config, f, False)


def _karina(db):
    return make_user(db, **dict(
        INTAKE, name="karina", age=28, gender="female", goal="muscle_building,endurance,general_fitness",
        experience="beginner", equipment="full_gym", diet="pescatarian", workout_time="08:00", workout_days="5",
        height_ft=5, height_in=5, weight_lbs=125, existing_tools="strava,oura", onboarding_step=1, reported_intake=None))


@pytest.mark.parametrize("first", ["hey cued", "Good! Busy day of meetings but good.", "It's going well, thanks"])
def test_a_working_adult_is_never_assumed_a_student(db, sms_capture, first):
    import onboarding_agent as oa
    from sms import send_sms
    for i in range(3):
        u = _karina(db)
        send_sms(u.phone, HOOK, user_id=u.id, message_type="onboarding")
        sms_capture.clear()
        oa.handle_onboarding_reply(u, first)
        sent = [b for _p, b in sms_capture]
        print(f"\n[{first!r} {i+1}] sent={sent!r}")
        body = " ".join(sent)
        assert sent and not CAMPUS_RE.search(body), sent
        assert body.count("?") <= 1, sent
