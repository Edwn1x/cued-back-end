"""
Tier-2 (live) anchors for first-card calibration (2026-09-23; user 33's bench-135-
did-35 card, users 32/43 abandoned theirs). Binary, parametrized x3 per the founder
rule. Both branches of the ask are offered as tools (set_lift_anchors / no_anchors),
so the model is never left with a dead end.

  1. Trained user, nothing on file, "starting push" → NO card yet; the reply asks
     what they lift (one line).
  2. …then "bench 185 squat 225 for 5" → set_lift_anchors + start_workout_session:
     anchors saved, the card goes out, bench planned at 185.
  3. …or "no idea just start me light" → start_workout_session(no_anchors) → the
     card goes out at the profile-calibrated 140.
  4. Never-trained user "starting push" → straight to the card (bench 45), no ask.
  5. A lift stated mid-conversation → set_lift_anchors, no card.
Run: pytest tests/tier2/test_first_card_calibration_live.py --run-tier2 -s
"""

from __future__ import annotations

import re

import pytest

from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import imessage_on, sidecar_ok, card_ok  # noqa: F401 — fixtures

pytestmark = pytest.mark.tier2

ANGEL = dict(name="Angel", onboarding_step=3, preferred_channel="imessage", equipment="full_gym",
             current_split="ppl", confirmed_training_split="ppl", workout_days="5,6",
             workout_time="17:00-18:00", wake_time="08:00", sleep_time="00:00", height_ft=5, height_in=11,
             weight_lbs=180, age=20, gender="male", goal="fat_loss", experience="intermediate",
             avg_steps=9000, calorie_target=2080, protein_target=180, targets_source="computed")
KRLA = dict(name="Krla", onboarding_step=3, preferred_channel="imessage", equipment="full_gym",
            current_split="ppl", confirmed_training_split="ppl", workout_days="3", workout_time="18:00",
            wake_time="08:00", sleep_time="00:00", height_ft=5, height_in=0, weight_lbs=137, age=20,
            gender="female", goal="general_fitness", experience="none", avg_steps=7000,
            calorie_target=1550, protein_target=128, targets_source="computed")

ASK_RE = re.compile(r"\b(bench\w*|squat\w*|deadlift\w*|press\w*|lift\w*|numbers|weights?|working)\b", re.IGNORECASE)


def _loop_on(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "START_WORKOUT_TOOL_ENABLED", "LOG_WORKOUT_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)


def _run(user_id, text):
    """One inbound turn as app.process_buffered_message runs it: the pending-card
    code pre-pass first (the answer to the ask never reaches the model), then the loop."""
    from workouts.calibrate import handle_pending_card_reply
    if handle_pending_card_reply(user_id, text):
        return "[code: card sent]"
    from agent_loop import run_agent_loop
    from models import get_session, User
    s = get_session()
    try:
        return run_agent_loop(s.get(User, user_id), text, "freeform")
    finally:
        s.close()


def _sessions(user_id):
    from models import get_session, WorkoutSession, SetLog
    s = get_session()
    try:
        out = []
        for ws in s.query(WorkoutSession).filter_by(user_id=user_id).order_by(WorkoutSession.id).all():
            bench = s.query(SetLog).filter_by(session_id=ws.id, exercise="bench_press").first()
            out.append((ws.template_key, ws.status, bench.planned_weight if bench else None))
        return out
    finally:
        s.close()


def _anchors(user_id):
    from models import get_session, User
    s = get_session()
    try:
        return dict(s.get(User, user_id).lift_anchors or {})
    finally:
        s.close()


@pytest.mark.parametrize("run", [1, 2, 3])
def test_trained_first_card_asks_before_guessing(db, monkeypatch, imessage_on, sidecar_ok, card_ok, run):
    _loop_on(monkeypatch)
    user = make_user(db, **ANGEL)
    reply = _run(user.id, "starting push")
    assert _sessions(user.id) == [] and card_ok == [], f"a card went out before asking: {_sessions(user.id)} {card_ok} — {reply!r}"
    assert ASK_RE.search(reply or ""), f"reply doesn't ask what they lift: {reply!r}"
    assert (reply or "").count("?") <= 1 and len((reply or "").splitlines()) <= 2, f"not one line: {reply!r}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_answer_sets_anchors_and_sends_the_card(db, monkeypatch, imessage_on, sidecar_ok, card_ok, run):
    _loop_on(monkeypatch)
    user = make_user(db, **ANGEL)
    first = _run(user.id, "starting push")
    assert _sessions(user.id) == [], f"card before the ask: {first!r}"
    reply = _run(user.id, "bench 185 squat 225 for 5")
    a = _anchors(user.id)
    assert a.get("bench_press", {}).get("weight") == 185 and a.get("squat", {}).get("weight") == 225, \
        f"anchors not saved: {a} — reply: {reply!r}"
    sess = _sessions(user.id)
    assert sess and sess[-1][0] == "push" and sess[-1][2] == 185.0, f"no push card at 185: {sess} — reply: {reply!r}"
    assert card_ok, "card never sent"
    assert sidecar_ok and sidecar_ok[-1].startswith("push day. starting u at 185 on bench press"), sidecar_ok
    assert not ASK_RE.search(reply or "") or "[silent]" in (reply or "") or not (reply or "").strip(), \
        f"reply after the card should be silent: {reply!r}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_dont_know_sends_the_card_from_stats(db, monkeypatch, imessage_on, sidecar_ok, card_ok, run):
    _loop_on(monkeypatch)
    user = make_user(db, **ANGEL)
    first = _run(user.id, "starting push")
    assert _sessions(user.id) == [], f"card before the ask: {first!r}"
    reply = _run(user.id, "no idea lol just start me light")
    sess = _sessions(user.id)
    assert sess and sess[-1][0] == "push" and sess[-1][2] == 140.0, f"no push card at the calibrated 140: {sess} — reply: {reply!r}"
    assert _anchors(user.id) == {}, f"invented anchors: {_anchors(user.id)}"
    assert sidecar_ok and "best guess from ur stats" in sidecar_ok[-1], sidecar_ok


BEGINNER_ASK_RE = re.compile(r"\b(bar|bench\w*|squat\w*|press\w*|dumbbell\w*|number\w*|weight\w*|lifted|heaviest)\b", re.IGNORECASE)


@pytest.mark.parametrize("run", [1, 2, 3])
def test_never_trained_is_asked_for_any_number_then_no_clue_sends_a_light_card(db, monkeypatch, imessage_on, sidecar_ok, card_ok, run):
    """Founder 2026-09-23: an anchor ask is helpful for complete beginners too — worded
    for them (any number, even the bar), with 'no clue' as the easy out."""
    _loop_on(monkeypatch)
    user = make_user(db, **KRLA)
    reply = _run(user.id, "starting push")
    assert _sessions(user.id) == [] and card_ok == [], f"card before the ask: {card_ok} — {reply!r}"
    assert BEGINNER_ASK_RE.search(reply or ""), f"reply doesn't ask for a number: {reply!r}"
    assert (reply or "").count("?") <= 1 and len((reply or "").splitlines()) <= 2, f"not one line: {reply!r}"
    reply2 = _run(user.id, "no clue lol never really lifted")
    sess = _sessions(user.id)
    assert sess and sess[-1][0] == "push" and sess[-1][2] == 45.0, f"no push card at the bar: {sess} — reply: {reply2!r}"
    assert _anchors(user.id) == {}, f"invented anchors: {_anchors(user.id)}"
    assert card_ok and sidecar_ok and "best guess from ur stats" in sidecar_ok[-1], sidecar_ok


@pytest.mark.parametrize("run", [1, 2, 3])
def test_beginner_with_a_number_gets_the_card_from_it(db, monkeypatch, imessage_on, sidecar_ok, card_ok, run):
    _loop_on(monkeypatch)
    user = make_user(db, **KRLA)
    first = _run(user.id, "starting push")
    assert _sessions(user.id) == [], f"card before the ask: {first!r}"
    reply = _run(user.id, "i benched like 35 once w my friend, that's all i got")
    a = _anchors(user.id)
    assert a.get("bench_press", {}).get("weight") == 35, f"anchor not saved: {a} — reply: {reply!r}"
    sess = _sessions(user.id)
    assert sess and sess[-1][0] == "push" and sess[-1][2] == 35.0, f"no push card at 35: {sess} — reply: {reply!r}"
    assert sidecar_ok and sidecar_ok[-1].startswith("push day. starting u at 35 on bench press"), sidecar_ok


@pytest.mark.parametrize("run", [1, 2, 3])
def test_stated_lift_in_conversation_becomes_an_anchor(db, monkeypatch, imessage_on, sidecar_ok, card_ok, run):
    _loop_on(monkeypatch)
    user = make_user(db, **ANGEL)
    reply = _run(user.id, "btw i bench 185 now, felt easy today")
    a = _anchors(user.id)
    assert a.get("bench_press", {}).get("weight") == 185, f"anchor not saved: {a} — reply: {reply!r}"
    assert _sessions(user.id) == [] and card_ok == [], f"a card went out on a statement: {card_ok} — {reply!r}"
