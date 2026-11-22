"""
Tier-2 (live) anchors for intake in the loop (intake.py, 2026-11-21). Binary, x3 each
(founder rule). Maddy's thread (user 100, 2026-11-20) is the script.

  1. A meal mid-setup: the loop logs it (log_meal) AND the same single bubble ends on one
     setup question — no "logged. back to setup" second line, no targets talk.
  2. A plain reply: one bubble, one question from STILL UNKNOWN, no list, no `---`.
Run: pytest tests/tier2/test_intake_in_loop_live.py --run-tier2 -s
"""
from __future__ import annotations

import re

import pytest

import config
from tests.factories import make_user

pytestmark = pytest.mark.tier2

N = 3
MID = dict(onboarding_step=2, preferred_channel="sms", user_timezone="America/Los_Angeles", name="Maddy",
           age=21, gender="female", goal="muscle_building,strength", experience="advanced", equipment="full_gym",
           occupation="student at uc berkeley", activity_level=None, avg_steps=None, cooking_situation=None,
           injuries=None, wake_time=None, sleep_time=None, workout_days=None, workout_time=None,
           current_split=None, height_ft=None, height_in=None, weight_lbs=None, diet=None,
           existing_tools=None, reported_intake=None)
KICKOFF = ("hey Maddy, it's cued. ur in. quick setup before i can actually help: gonna get to know u a bit over "
           "the next few texts, then run u through everything i can do. how's ur day going")


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_USER_IDS", frozenset())
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", False)
    for f in ("ONBOARDING_RUNDOWN_ENABLED", "CARD_SETUP_ENABLED", "WATER_OFFER_ENABLED", "PHOTON_PROVISIONING_ENABLED",
              "ONBOARDING_REENGAGE_FOOD_FIRST_ENABLED"):
        monkeypatch.setattr(config, f, False)


def _coach_said(user, text):
    from sms import send_sms
    send_sms(user.phone, text, user_id=user.id, message_type="onboarding")


def _bodies(sms_capture):
    return [b for _p, b in sms_capture]


def _one_question(text: str) -> bool:
    import onboarding_agent as oa
    return bool(oa._question_part(text)) and text.count("?") <= 2


def test_a_meal_mid_setup_is_logged_and_the_bubble_still_asks(db, sms_capture):
    import app as appmod
    from models import Meal
    results = []
    for i in range(N):
        u = make_user(db, **MID)
        _coach_said(u, KICKOFF)
        _coach_said(u, "good, it's friday so can't complain. how much walking do u do on a normal day outside the gym")
        sms_capture.clear()
        appmod.process_buffered_message(u.id, "a lot, like 10k steps. also just had a chicken burrito from chipotle for lunch", "freeform")
        out = _bodies(sms_capture)
        logged = db.query(Meal).filter(Meal.user_id == u.id).count()
        text = out[0] if out else ""
        ok = (len(out) == 1 and logged >= 1 and _one_question(text) and "---" not in text
              and not re.search(r"\b(remaining|left for the day|on track|ur target)\b", text, re.I)
              and "back to setup" not in text)
        results.append(ok)
        print(f"\n[{i+1}] bubbles={len(out)} meals={logged} ok={ok}\n  {text!r}")
    assert all(results), results


def test_a_plain_reply_is_one_bubble_with_one_setup_question(db, sms_capture):
    import app as appmod
    results = []
    for i in range(N):
        u = make_user(db, **MID)
        _coach_said(u, KICKOFF)
        sms_capture.clear()
        appmod.process_buffered_message(u.id, "Good wbu", "freeform")
        out = _bodies(sms_capture)
        text = out[0] if out else ""
        ok = (len(out) == 1 and _one_question(text) and "---" not in text
              and not re.search(r"\b(a few things|i need from you|here's what i need)\b", text, re.I)
              and "\n-" not in text and not re.search(r"\b\d\)\s|\b\d\.\s", text))
        results.append(ok)
        print(f"\n[{i+1}] bubbles={len(out)} ok={ok}\n  {text!r}")
    assert all(results), results
