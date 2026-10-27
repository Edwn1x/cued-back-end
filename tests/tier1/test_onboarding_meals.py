"""
Meals mid-setup (onboarding_meals.py, founder 2026-10-27): "it should still log it and the
functionality should work as normal, acknowledge the request and then continue with the flow."
A meal text (cue regex + one-word Haiku gate) or any photo from a user still in onboarding
runs the normal agent loop; stats in the same text still land; code then asks the next intake
field unless the loop's line already ends on a question.
"""
from __future__ import annotations

import pytest

import config
from tests.factories import make_user
from tests._fake_anthropic import ToolUse

MID = dict(onboarding_step=2, preferred_channel="imessage", user_timezone="America/Los_Angeles", name="Karol",
           goal="muscle_building", occupation="cs student", activity_level=None, avg_steps=None,
           cooking_situation=None, injuries=None, wake_time=None, sleep_time=None, workout_days="3",
           current_split="none", height_ft=None, weight_lbs=None)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_MEALS_ENABLED", True)
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_FOOD_FIRST_ENABLED", False)


def _is_gate(kw) -> bool:
    return "Reply with exactly one word: yes or no" in str(kw["messages"][0]["content"])


def _gate_says(word):
    return lambda kw: word if _is_gate(kw) else None


def _intake_counter(monkeypatch):
    import app as appmod
    called = {"n": 0}
    monkeypatch.setattr(appmod, "handle_onboarding_reply", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    return called


# ── the gate ────────────────────────────────────────────────────────────────

def test_gate_needs_a_cue_then_haikus_yes(anthropic_stub):
    from onboarding_meals import is_meal_turn
    anthropic_stub.reply_with(_gate_says("yes"))
    assert is_meal_turn("had a chicken burrito", has_image=False, user_id=1) is True
    assert len(anthropic_stub.calls) == 1 and config.HAIKU_MODEL == anthropic_stub.calls[0]["model"]
    assert is_meal_turn("no allergies", has_image=False, user_id=1) is False     # no cue → no call
    assert is_meal_turn("5'10 and 170", has_image=False, user_id=1) is False
    assert len(anthropic_stub.calls) == 1


def test_gate_says_no_for_a_habit_answer_and_any_photo_is_a_meal_turn(anthropic_stub):
    from onboarding_meals import is_meal_turn
    anthropic_stub.reply_with(_gate_says("no"))
    assert is_meal_turn("i usually eat lunch at crossroads", has_image=False, user_id=1) is False
    assert len(anthropic_stub.calls) == 1, "the cue matched, the gate said no"
    assert is_meal_turn("", has_image=True, user_id=1) is True and len(anthropic_stub.calls) == 1, "a photo skips the gate"


def test_gate_failure_means_not_a_meal_turn(anthropic_stub, monkeypatch):
    import onboarding_meals
    def boom(kw):
        if _is_gate(kw):
            raise RuntimeError("down")
    anthropic_stub.reply_with(boom)
    assert onboarding_meals.is_meal_turn("had a burrito", has_image=False, user_id=1) is False
    monkeypatch.setattr(config, "ONBOARDING_MEALS_ENABLED", False)
    assert onboarding_meals.is_meal_turn("", has_image=True, user_id=1) is False


# ── the turn ────────────────────────────────────────────────────────────────

def test_a_burrito_mid_setup_is_logged_then_the_next_ask_follows(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    from models import Meal, User
    u = make_user(db, **MID)
    called = _intake_counter(monkeypatch)
    state = {"n": 0}

    def script(kw):
        if _is_gate(kw):
            return "yes"
        state["n"] += 1
        if state["n"] == 1:
            assert any("STILL MID-SETUP — A MEAL CAME IN" in str(b.get("text", "") if isinstance(b, dict) else b)
                       for b in (kw.get("system") or [])), "the loop sees the mid-setup block"
            return ToolUse("log_meal", {"description": "chicken burrito", "calories": 650, "protein_g": 35, "carbs_g": 70, "fat_g": 25})
        return "chicken burrito, ~650 cal and 35g protein. logged."
    anthropic_stub.reply_with(script)
    appmod.process_buffered_message(u.id, "had a chicken burrito", "freeform")
    out = [b for _p, b in sms_capture]
    assert called["n"] == 0, "the questionnaire did not answer this one"
    assert db.query(Meal).filter(Meal.user_id == u.id).count() == 1
    assert out[0].startswith("chicken burrito, ~650 cal")
    assert out[1] == "logged. back to setup real quick: ur height and weight?"
    db.expire_all()
    assert db.get(User, u.id).onboarding_step == 2, "still in setup"
    # the next text is intake's again
    sms_capture.clear()
    anthropic_stub.reply_with(_gate_says("no"))
    appmod.process_buffered_message(u.id, "5'6 and 150", "freeform")
    assert called["n"] == 1


def test_stats_riding_with_the_meal_land_and_the_ask_moves_on(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    from models import User
    u = make_user(db, **MID)
    _intake_counter(monkeypatch)
    state = {"n": 0}

    def script(kw):
        if _is_gate(kw):
            return "yes"
        state["n"] += 1
        return ToolUse("log_meal", {"description": "burrito", "calories": 600, "protein_g": 30, "carbs_g": 60, "fat_g": 20}) if state["n"] == 1 else "burrito, ~600 cal / 30g. logged."
    anthropic_stub.reply_with(script)
    appmod.process_buffered_message(u.id, "had a burrito, im 5'10 and 170 lbs btw", "freeform")
    db.expire_all()
    row = db.get(User, u.id)
    assert (row.height_ft, row.height_in, row.weight_lbs) == (5, 10, 170.0)
    out = [b for _p, b in sms_capture]
    assert out[1].startswith("logged. back to setup real quick: ") and "height" not in out[1], "height/weight already in → the next field"


def test_a_loop_line_that_ends_on_a_question_gets_no_second_bubble(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    u = make_user(db, **MID)
    _intake_counter(monkeypatch)
    anthropic_stub.reply_with(lambda kw: "yes" if _is_gate(kw) else "was that the big burrito or the small one?")
    appmod.process_buffered_message(u.id, "had a burrito", "freeform")
    assert [b for _p, b in sms_capture] == ["was that the big burrito or the small one?"]


def test_flag_off_or_no_meal_means_the_intake_agent_answers(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    u = make_user(db, **MID)
    called = _intake_counter(monkeypatch)
    anthropic_stub.reply_with(_gate_says("no"))
    appmod.process_buffered_message(u.id, "i mostly eat at crossroads", "freeform")
    assert called["n"] == 1 and sms_capture == []
    monkeypatch.setattr(config, "ONBOARDING_MEALS_ENABLED", False)
    anthropic_stub.reply_with(_gate_says("yes"))
    appmod.process_buffered_message(u.id, "had a burrito", "freeform")
    assert called["n"] == 2
    assert sum(1 for c in anthropic_stub.calls if _is_gate(c)) == 0, "no cue word, then flag off: the gate never ran"


def test_continue_line_is_none_when_nothing_is_left(db):
    from onboarding_meals import continue_line
    u = make_user(db, onboarding_step=2, height_ft=5, height_in=6, weight_lbs=142, occupation="student",
                  activity_level="lightly active", avg_steps=10000, workout_days="3", workout_time="18:00",
                  current_split="ppl", cooking_situation="mix", diet="omnivore", injuries="none",
                  wake_time="09:00", sleep_time="01:00", existing_tools="none")
    assert continue_line(u) is None
    assert continue_line(make_user(db, **MID)) == "logged. back to setup real quick: ur height and weight?"


def test_the_block_only_shows_during_a_marked_turn(db):
    import onboarding_meals
    u = make_user(db, **MID)
    assert onboarding_meals.context_block(u) is None
    onboarding_meals.begin_turn(u.id)
    try:
        assert "A MEAL CAME IN" in onboarding_meals.context_block(u)
    finally:
        onboarding_meals.end_turn(u.id)
    assert onboarding_meals.context_block(u) is None
