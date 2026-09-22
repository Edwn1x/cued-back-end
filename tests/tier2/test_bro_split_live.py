"""
Tier-2 (live) anchors for bro-split cards (2026-09-22, user 43 "Angel"). Binary,
parametrized x3 per the founder rule.

  1. Extractor: Angel's exact message yields split_days that map to his three days.
  2. Loop: a bro-split user states his days in conversation → save_routine(split_days)
     → users.split_days is his order; the reply never claims a card it didn't send.
  3. Loop: with his days saved, "show me a workout card" → start_workout_session with
     no template_key → the session is chest_biceps (never full_body), reply silent.
  4. Loop: a "custom" split with no days → the tool refuses → no card goes out and the
     reply asks which days they run.
Run: pytest tests/tier2/test_bro_split_live.py --run-tier2 -s
"""

from __future__ import annotations

import re

import pytest

from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import imessage_on, sidecar_ok, card_ok  # noqa: F401 — fixtures

pytestmark = pytest.mark.tier2

ANGEL_MSG = "chest and biceps , back and triceps  and legs and shoulders"
ANGEL_DAYS = ["chest_biceps", "back_triceps", "legs_shoulders"]
ANGEL = dict(name="Angel", onboarding_step=3, preferred_channel="imessage", equipment="full_gym",
             current_split="bro_split", confirmed_training_split="bro_split", workout_days="5,6",
             workout_time="17:00-18:00", wake_time="08:00", sleep_time="00:00", height_ft=5, height_in=11,
             weight_lbs=180, age=20, gender="male", goal="fat_loss", experience="intermediate",
             avg_steps=9000, calorie_target=2080, protein_target=180, targets_source="computed")


def _loop_on(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "START_WORKOUT_TOOL_ENABLED", "LOG_WORKOUT_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)


def _run(user_id, text):
    from agent_loop import run_agent_loop
    from models import get_session, User
    s = get_session()
    try:
        return run_agent_loop(s.get(User, user_id), text, "freeform")
    finally:
        s.close()


def _reload(user_id):
    from models import get_session, User
    s = get_session()
    try:
        u = s.get(User, user_id)
        return dict(split_days=u.split_days, current_split=u.current_split)
    finally:
        s.close()


@pytest.mark.parametrize("run", [1, 2, 3])
def test_extractor_returns_angels_days_in_order(db, run):
    import onboarding_agent
    from workouts.routine import split_days_from_phrases
    user = make_user(db, **dict(ANGEL, onboarding_step=2, current_split=None, confirmed_training_split=None))
    data = onboarding_agent._extract_data_from_message(ANGEL_MSG, user, last_asked_field="current_split")
    assert split_days_from_phrases(data.get("split_days") or []) == ANGEL_DAYS, f"extractor gave {data!r}"
    assert data.get("current_split") == "bro_split", f"extractor gave {data!r}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_loop_saves_stated_days_via_save_routine(db, monkeypatch, imessage_on, sidecar_ok, card_ok, run):
    _loop_on(monkeypatch)
    user = make_user(db, **ANGEL)
    reply = _run(user.id, "my split is chest and bis, back and tris, then legs and shoulders")
    after = _reload(user.id)
    assert after["split_days"] == ANGEL_DAYS, f"split_days not saved: {after} — reply: {reply!r}"
    assert card_ok == [], f"a card went out on a split statement: {card_ok}"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_loop_starts_his_first_day_not_full_body(db, monkeypatch, imessage_on, sidecar_ok, card_ok, run):
    from models import get_session, WorkoutSession
    _loop_on(monkeypatch)
    user = make_user(db, **dict(ANGEL, split_days=ANGEL_DAYS))
    reply = _run(user.id, "show me a workout card")
    s = get_session()
    try:
        ws = s.query(WorkoutSession).filter_by(user_id=user.id).order_by(WorkoutSession.id.desc()).first()
        key = ws.template_key if ws else None
    finally:
        s.close()
    assert key == "chest_biceps", f"session template {key!r} — reply: {reply!r}"
    assert card_ok and card_ok[0]["caption"].startswith("chest + biceps · "), card_ok
    assert sidecar_ok and sidecar_ok[0].startswith("chest + biceps day."), sidecar_ok
    assert "full body" not in (reply or "").lower(), reply


@pytest.mark.parametrize("run", [1, 2, 3])
def test_loop_asks_for_days_instead_of_guessing(db, monkeypatch, imessage_on, sidecar_ok, card_ok, run):
    from models import get_session, WorkoutSession
    _loop_on(monkeypatch)
    user = make_user(db, **dict(ANGEL, current_split="custom", confirmed_training_split="custom"))
    reply = _run(user.id, "send me today's workout")
    s = get_session()
    try:
        n = s.query(WorkoutSession).filter_by(user_id=user.id).count()
    finally:
        s.close()
    assert n == 0 and card_ok == [], f"a card went out for an unmapped split: {card_ok} — reply: {reply!r}"
    low = (reply or "").lower()
    # Plain voice asks without a question mark ("what days do u run") — match the ask,
    # not the punctuation.
    assert re.search(r"\b(what|which|how)\b.*\b(days?|split)\b", low), \
        f"reply doesn't ask which days they run: {reply!r}"
