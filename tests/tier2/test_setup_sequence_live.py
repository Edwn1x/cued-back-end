"""
Tier-2 (live) anchors for onboarding restructure PR 2 (2026-10-09). Binary, x3 each.
The founder's run (user 48, msgs 5820-5828): after the setup card, "So what now" →
"start the card. tap each set as u go" → "I'm not going to the gym rn tho?" → "my bad,
cleared it". The WORKOUT CARD block now says a planned setup card is not a session.

  1. "So what now" with a planned setup card → never 'start the card' / 'tap each set';
     says nothing til they lift (heading in / gym) or points at food.
  2. "I'm not going to the gym rn tho?" → no 'cleared it', no apology-and-clear; the
     setup session stays planned.
  3. The completion bubble asks nothing and promises no card; the rundown carries no link.
Run: pytest tests/tier2/test_setup_sequence_live.py --run-tier2 -s
"""
from __future__ import annotations

import re

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import imessage_on, sidecar_ok, card_ok  # noqa: F401 — fixtures
from tests.tier2.test_card_setup_live import ANGEL, _run, _on

pytestmark = pytest.mark.tier2

START_RE = re.compile(r"start (the|ur|your) card|tap (each|a|the) set|open (the|ur|your) card and|let'?s go\b", re.I)
CLEAR_RE = re.compile(r"\bclear(ed|ing)? (it|that|the card)|\bmy bad\b|\bsorry\b|scrapped|took it (back|down)", re.I)
LATER_RE = re.compile(r"\b(til|till|until|when) (u|you|ur|you're|ur at|u're|u get|you get|ur heading|ur in)\b.*\b(gym|rsf|lift|head|in|there)\b|"
                      r"\b(nothing|nun|not much) (to do |til |till |until |rn|right now)|\b(heading|head) (in|to the gym|there)|"
                      r"\b(eat|ate|food|meal|lunch|dinner)\b", re.I)


def _setup_user(db, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(config, "SETUP_SEQUENCE_ENABLED", True)
    from workouts.start import start_workout_session
    u = make_user(db, **ANGEL)
    r = start_workout_session(u.id, setup=True)          # the planned setup card, as the sequence sends it
    assert r["surface"] == "card" and r["setup"] is True
    return u


def _status(user_id):
    from models import get_session, WorkoutSession
    s = get_session()
    try:
        return [w.status for w in s.query(WorkoutSession).filter_by(user_id=user_id).order_by(WorkoutSession.id).all()]
    finally:
        s.close()


@pytest.mark.parametrize("i", range(3))
def test_so_what_now_is_not_start_the_card(db, imessage_on, sidecar_ok, card_ok, monkeypatch, i):
    u = _setup_user(db, monkeypatch)
    reply = _run(u.id, "So what now")
    print(f"\n[what now {i}] {reply!r}")
    assert not START_RE.search(reply), reply
    assert LATER_RE.search(reply), reply
    assert _status(u.id) == ["planned"], _status(u.id)


@pytest.mark.parametrize("i", range(3))
def test_not_at_the_gym_is_not_an_apology_and_a_clear(db, imessage_on, sidecar_ok, card_ok, monkeypatch, i):
    u = _setup_user(db, monkeypatch)
    reply = _run(u.id, "I'm not going to the gym rn tho?")
    print(f"\n[not at gym {i}] {reply!r}")
    assert not CLEAR_RE.search(reply), reply
    assert not START_RE.search(reply), reply
    assert _status(u.id) == ["planned"], _status(u.id)


@pytest.mark.parametrize("i", range(3))
def test_completion_bubble_and_rundown(db, sms_capture, monkeypatch, i):
    import onboarding_agent as oa
    from profile_page import profile_url
    for f in ("CARD_SETUP_ENABLED", "WATER_OFFER_ENABLED", "PHOTON_PROVISIONING_ENABLED"):
        monkeypatch.setattr(config, f, False)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_DELAY_S", 0)
    u = make_user(db, name="edwin", onboarding_step=2, age=20, gender="male", goal="fat_loss,muscle_building,strength",
                  experience="intermediate", equipment="full_gym", height_ft=5, height_in=6, weight_lbs=139,
                  workout_days="4", workout_time="18:00", diet="omnivore", occupation="cs student at berkeley",
                  activity_level="active", avg_steps=10000, current_split="ppl", cooking_situation="cook",
                  injuries="none", existing_tools="none", wake_time="12:00", sleep_time="03:00",
                  calorie_target=None, protein_target=None)
    from sms import send_sms
    send_sms(u.phone, "roughly when r u up and when do u crash", user_id=u.id, message_type="onboarding")
    sms_capture.clear()
    done = oa.handle_onboarding_reply(u, "up at 12 down by 3 lol")
    sent = [b for _p, b in sms_capture]
    print(f"\n[completion {i}] {sent!r}")
    assert done is True
    reaction = sent[0]
    assert "?" not in reaction and not re.search(r"\bcard\b", reaction, re.I), reaction
    assert sent[1].startswith("ok so far this is what i have: ur 5'6 139") and oa.SUMMARY_CLOSER in sent[2]
    assert profile_url(u) in sent[2]
    rundown = sent[3]
    assert rundown.lower().startswith("oh and") and "http" not in rundown and "?" not in rundown.rstrip()[-1:], rundown
