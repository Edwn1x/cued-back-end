"""
Intake in the loop (intake.py, founder 2026-11-21): a mid-setup turn is the agent loop's turn
with the SETUP block, every coaching tool minus the post-setup ones, and the intake agent's
code-owned guards on the way out. Live 2026-11-20, user 100 (Maddy): "can we go to imsg" →
"i can't move us over from my end rn" — the intake agent had no tools and nothing to do but
write a sentence. The flag is off by default; the allowlist narrows it for a staged rollout;
the loop failing falls through to the intake agent for that turn.
"""
from __future__ import annotations

import logging

import pytest

import config
from tests.factories import make_user
from tests._fake_anthropic import ToolUse

MID = dict(onboarding_step=2, preferred_channel="imessage", user_timezone="America/Los_Angeles", name="Maddy",
           age=21, gender="female", goal="muscle_building", experience="advanced", equipment="full_gym",
           occupation="student at uc berkeley", activity_level=None, avg_steps=None, cooking_situation=None,
           injuries=None, wake_time=None, sleep_time=None, workout_days="3", workout_time="18:00",
           current_split="none", height_ft=None, height_in=None, weight_lbs=None, diet=None,
           existing_tools=None, reported_intake=None)


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_USER_IDS", frozenset())
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_MEALS_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_FOOD_FIRST_ENABLED", False)
    monkeypatch.setattr(config, "ONBOARDING_CONTINUATION_FOLD_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", False)
    monkeypatch.setattr(config, "CARD_SETUP_ENABLED", False)
    monkeypatch.setattr(config, "WATER_OFFER_ENABLED", False)
    monkeypatch.setattr(config, "PROGRAM_BRIEF_ENABLED", False)
    monkeypatch.setattr(config, "SETUP_SEQUENCE_ENABLED", False)
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "SET_TARGETS_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "START_WORKOUT_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "REMINDERS_ENABLED", True)


def _is_extract(kw) -> bool:
    return "Extract any fitness coaching profile data" in str(kw["messages"][0]["content"])


def _is_gate(kw) -> bool:
    return "Reply with exactly one word: yes or no" in str(kw["messages"][0]["content"])


def _system_text(kw) -> str:
    sysb = kw.get("system") or ""
    if isinstance(sysb, list):
        return "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in sysb)
    return str(sysb)


def _is_loop(kw) -> bool:
    return "SETUP IN PROGRESS" in _system_text(kw)


def _tool_names(kw) -> set:
    return {t.get("name") for t in (kw.get("tools") or []) if isinstance(t, dict)}


def _intake_counter(monkeypatch):
    import app as appmod
    called = {"n": 0}
    monkeypatch.setattr(appmod, "handle_onboarding_reply", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    return called


def _bodies(sms_capture):
    return [b for _p, b in sms_capture]


def _coach_said(user, text):
    from sms import send_sms
    send_sms(user.phone, text, user_id=user.id, message_type="onboarding")


# ── the turn rides the loop ───────────────────────────────────────────────────

def test_a_mid_setup_turn_rides_the_loop_with_the_setup_block(db, sms_capture, anthropic_stub, monkeypatch, caplog):
    import app as appmod
    from models import Message
    u = make_user(db, **MID)
    called = _intake_counter(monkeypatch)
    seen = {}

    def script(kw):
        if _is_extract(kw):
            return "{}"
        if _is_loop(kw):
            seen["system"] = _system_text(kw)
            seen["tools"] = _tool_names(kw)
            return "legs on a tuesday is a move. u cooking most nights or is it dining hall"
        return "{}"   # memory extraction etc.
    anthropic_stub.reply_with(script)
    caplog.set_level(logging.INFO)
    appmod.process_buffered_message(u.id, "hit legs on tuesday, climbing the rest of the week", "freeform")
    assert called["n"] == 0, "the intake agent did not answer this turn"
    assert _bodies(sms_capture) == ["legs on a tuesday is a move. u cooking most nights or is it dining hall"]
    sysb = seen["system"]
    assert "STILL UNKNOWN" in sysb and "height and weight" in sysb, "the unknowns ride in the block"
    assert "Name: Maddy" in sysb, "what's on file rides in the block"
    assert "THIS TURN: react to the specific thing" in sysb, "friend mode by default"
    assert "log_meal" in seen["tools"] and "set_reminder" in seen["tools"] and "web_search" in seen["tools"]
    assert not (seen["tools"] & {"set_targets", "start_workout_session", "adjust_program", "save_routine"})
    row = db.query(Message).filter(Message.user_id == u.id, Message.direction == "out").order_by(Message.id.desc()).first()
    assert row.message_type == "onboarding", "the reply is a setup turn for the turn count / re-engage sweep"
    assert "ONBOARDING_REPLY mode=friend" in caplog.text and "path=loop" in caplog.text
    assert "INTAKE_TOOLS_FILTERED" in caplog.text
    # nothing of the intake agent's post-setup blocks leaked in
    assert "A MEAL CAME IN" not in sysb and "RE-ENGAGED" not in sysb


def test_a_meal_mid_setup_is_logged_and_the_question_rides_the_same_reply(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    from models import Meal
    u = make_user(db, **MID)
    _intake_counter(monkeypatch)
    state = {"n": 0}

    def script(kw):
        if _is_extract(kw) or (not _is_loop(kw) and not _is_gate(kw)):
            return "{}"
        assert not _is_gate(kw), "the meal gate never runs on an intake turn"
        state["n"] += 1
        if state["n"] == 1:
            return ToolUse("log_meal", {"description": "chicken burrito", "calories": 650, "protein_g": 35, "carbs_g": 70, "fat_g": 25})
        return "chicken burrito, ~650 cal and 35g protein. logged."
    anthropic_stub.reply_with(script)
    appmod.process_buffered_message(u.id, "had a chicken burrito", "freeform")
    assert db.query(Meal).filter(Meal.user_id == u.id).count() == 1
    out = _bodies(sms_capture)
    assert len(out) == 1, "one bubble: the meal AND the next ask — no 'logged. back to setup' second line"
    assert out[0] == "chicken burrito, ~650 cal and 35g protein. logged. also, ur height and weight?"


def test_stats_land_and_the_unknowns_the_block_lists_are_the_ones_after_this_turn(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    from models import User
    u = make_user(db, **MID)
    _intake_counter(monkeypatch)
    seen = {}

    def script(kw):
        if _is_extract(kw) or not _is_loop(kw):
            return "{}"
        seen["system"] = _system_text(kw)
        return "5'10 170 noted. roughly how many steps u get in a day"
    anthropic_stub.reply_with(script)
    appmod.process_buffered_message(u.id, "im 5'10 and 170 lbs", "freeform")
    db.expire_all()
    row = db.get(User, u.id)
    assert (row.height_ft, row.height_in, row.weight_lbs) == (5, 10, 170.0)
    block = seen["system"].split("STILL UNKNOWN")[1].split("HOW A SETUP REPLY WORKS")[0]
    assert "height and weight" not in block, "the block lists what's unknown AFTER this turn's answer landed"
    assert "Height: 5'10" in seen["system"]


def test_completion_closes_from_the_loop_path_without_a_loop_call(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    from models import User
    u = make_user(db, **dict(MID, activity_level="lightly active — walks around campus", avg_steps=10000,
                             cooking_situation="mix", diet="omnivore", injuries="none", wake_time="10:00",
                             sleep_time="01:30", existing_tools="none", reported_intake=2000))
    called = _intake_counter(monkeypatch)
    loop_calls = {"n": 0}

    def script(kw):
        if _is_extract(kw):
            return "{}"
        if _is_loop(kw):
            loop_calls["n"] += 1
            return "nope"
        return "5'10 170, got u."   # the completion reaction bubble (intake agent's generator)
    anthropic_stub.reply_with(script)
    appmod.process_buffered_message(u.id, "5'10 and 170", "freeform")
    db.expire_all()
    assert db.get(User, u.id).onboarding_step == 3
    assert called["n"] == 0 and loop_calls["n"] == 0, "the close is code's: no loop turn"
    out = _bodies(sms_capture)
    assert out and any("cal" in b for b in out), "the summary with the numbers went out"


def test_a_repeat_of_the_question_just_asked_is_dropped_on_the_way_out(db, sms_capture, anthropic_stub, monkeypatch, caplog):
    import app as appmod
    u = make_user(db, **MID)
    _intake_counter(monkeypatch)
    _coach_said(u, "last thing, u cook most nights or is it dining hall")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "fair enough. ok so food, u cooking or dining hall" if _is_loop(kw) else "{}")
    caplog.set_level(logging.INFO)
    appmod.process_buffered_message(u.id, "lol", "freeform")
    assert "ONBOARDING_REPEAT_QUESTION_SUPPRESSED" in caplog.text
    assert not any("dining hall" in b for b in _bodies(sms_capture)), "the repeated question never went out"


def test_a_continuation_the_model_keeps_silent_on_sends_nothing(db, sms_capture, anthropic_stub, monkeypatch, caplog):
    import app as appmod
    u = make_user(db, **MID)
    called = _intake_counter(monkeypatch)
    _coach_said(u, "nice. u cooking most nights or is it dining hall")
    sms_capture.clear()
    seen = {}

    def script(kw):
        if _is_extract(kw) or not _is_loop(kw):
            return "{}"
        seen["system"] = _system_text(kw)
        return "[silent]"
    anthropic_stub.reply_with(script)
    caplog.set_level(logging.INFO)
    appmod.process_buffered_message(u.id, "and climbing wednesdays", "freeform", continuation=True)
    assert called["n"] == 0
    assert _bodies(sms_capture) == []
    assert "THIS TURN: Maddy sent this before seeing your last reply" in seen["system"]
    assert "ONBOARDING_CONTINUATION_SUPPRESSED" in caplog.text and "reason=silent" in caplog.text


def test_a_silent_non_continuation_reply_still_gets_the_next_ask(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    u = make_user(db, **MID)
    _intake_counter(monkeypatch)
    anthropic_stub.reply_with(lambda kw: "[silent]" if _is_loop(kw) else "{}")
    appmod.process_buffered_message(u.id, "ok", "freeform")
    assert _bodies(sms_capture) == ["quick one: ur height and weight?"]


def test_card_ask_mode_when_they_want_the_workout_before_the_basics(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    u = make_user(db, **MID)
    _intake_counter(monkeypatch)
    seen = {}

    def script(kw):
        if _is_extract(kw) or not _is_loop(kw):
            return "{}"
        seen["system"] = _system_text(kw)
        return "card's coming the second i have ur height, weight and anything that hurts — what r they"
    anthropic_stub.reply_with(script)
    appmod.process_buffered_message(u.id, "can i get my workout for today", "freeform")
    assert "THIS TURN: they want their workout" in seen["system"]
    assert "height and weight" in seen["system"].split("THIS TURN: they want their workout")[1]
    assert len(_bodies(sms_capture)) == 1


def test_the_loop_failing_falls_through_to_the_intake_agent(db, sms_capture, anthropic_stub, monkeypatch, caplog):
    import app as appmod
    import agent_loop
    u = make_user(db, **MID)
    called = _intake_counter(monkeypatch)
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "unused")

    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(agent_loop, "run_agent_loop", boom)
    caplog.set_level(logging.WARNING)
    appmod.process_buffered_message(u.id, "hey", "freeform")
    assert called["n"] == 1, "the intake agent answered this turn"
    assert "INTAKE_LOOP_FAILED" in caplog.text and "INTAKE_LOOP_FELL_THROUGH" in caplog.text
    import intake
    assert not intake.is_turn(u.id), "the turn marker is cleared on failure"


def test_flag_off_or_outside_the_allowlist_is_the_intake_agent(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    import intake
    u = make_user(db, **MID)
    called = _intake_counter(monkeypatch)
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "unused")
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_ENABLED", False)
    appmod.process_buffered_message(u.id, "hey", "freeform")
    assert called["n"] == 1
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_USER_IDS", frozenset({u.id + 1000}))
    assert intake.enabled_for(u) is False
    appmod.process_buffered_message(u.id, "hey", "freeform")
    assert called["n"] == 2
    monkeypatch.setattr(config, "INTAKE_IN_LOOP_USER_IDS", frozenset({u.id}))
    assert intake.enabled_for(u) is True
    # a finished user is never an intake turn, whatever the flag says
    done = make_user(db, onboarding_step=3)
    assert intake.enabled_for(done) is False


def test_filter_tools_only_bites_on_an_intake_turn():
    import intake
    tools = [{"name": "log_meal"}, {"name": "set_targets"}, {"name": "start_workout_session"}, {"name": "web_search"}]
    assert intake.filter_tools(7, tools) == tools, "not an intake turn: untouched"
    intake.begin_turn(7, {"mode": "friend", "missing": []})
    try:
        assert [t["name"] for t in intake.filter_tools(7, tools)] == ["log_meal", "web_search"]
    finally:
        intake.end_turn(7)
    assert intake.filter_tools(7, tools) == tools
