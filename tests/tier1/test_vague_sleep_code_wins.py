"""A vague sleep answer is code's estimate, not the extractor's clock-time guess; and
"I already told you about the knees" when injuries is the open field fills it from memory.

Live 2026-10-11 19:07–19:08 (user 50, from zero): "I already told you the thing about the
knees?" + "And idk I sleep late and wake up late" → the extractor stored wake 12:00 / sleep
02:00 as FACT (sleep_estimated False; the summary would read "up at 12 down by 2") while
the reply said "ima guess…"; and injuries stayed unknown (the knee warm-up had gone to
memory as a training preference) → a third injuries ask was coming.
"""
from __future__ import annotations

import json
import logging

import pytest

import config
from tests.tier1.test_berkeley_friend_onboarding import _is_extract, _new_signup
from tests.tier1.test_onboarding_restructure_1 import _bodies, _row, _coach_said


def _all_but(db, *missing, **over):
    kw = dict(onboarding_step=2, height_ft=5, height_in=6, weight_lbs=142, occupation="student",
              activity_level="lightly active", avg_steps=10000, workout_days="3", workout_time="18:00",
              current_split="ppl", cooking_situation="mix", diet="omnivore", injuries="none",
              existing_tools="none")
    for m in missing:
        kw.pop(m, None)
    kw.update(over)
    return _new_signup(db, **kw)


def test_vague_sleep_answer_is_pinned_by_code_even_when_the_extractor_guessed_times(db, anthropic_stub, sms_capture, caplog):
    import onboarding_agent as oa
    user = _all_but(db)                                   # only wake_sleep missing
    _coach_said(user, "last thing — anything bugging u, and roughly when u up and when u crash")
    sms_capture.clear()
    caplog.set_level(logging.INFO)
    seen = {}

    def _handler(kw):
        if _is_extract(kw):
            return json.dumps({"wake_time": "12:00", "sleep_time": "02:00"})     # the model's guess, as fact
        seen["instruction"] = kw["messages"][0]["content"]
        return "gotcha, ima guess up around 11 and down by 2, fix it whenever"
    anthropic_stub.reply_with(_handler)

    assert oa.handle_onboarding_reply(user, "And idk I sleep late and wake up late") is True
    row = _row(db, user.id)
    assert (row.wake_time, row.sleep_time, row.sleep_estimated) == ("11:00", "02:00", True), \
        "code's estimate, flagged as a guess — not the extractor's 12:00 as fact"
    assert "ONBOARDING_SLEEP_MODEL_GUESS_DROPPED" in caplog.text and "ONBOARDING_SLEEP_ESTIMATED" in caplog.text
    assert "code pinned a guess: up around 11" in seen["instruction"]
    summary = [b for b in _bodies(sms_capture) if b.startswith("ok so")][0]
    assert "up around 11 down around 2 (my guess, fix it anytime)" in summary


def test_a_clock_time_answer_keeps_the_extractors_values(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    user = _all_but(db)
    _coach_said(user, "roughly when u up and when u crash")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: json.dumps({"wake_time": "10:00", "sleep_time": "01:00"}) if _is_extract(kw) else "10 to 1, got it")
    assert oa.handle_onboarding_reply(user, "up at 10, down around 1am") is True
    row = _row(db, user.id)
    assert (row.wake_time, row.sleep_time, bool(row.sleep_estimated)) == ("10:00", "01:00", False)


def test_already_told_you_about_the_knees_fills_injuries_from_memory(db, anthropic_stub, sms_capture, caplog):
    import onboarding_agent as oa
    from models import get_session, User
    user = _all_but(db, "injuries", wake_time="12:00", sleep_time="02:00")   # only injuries missing
    s = get_session()
    try:
        u = s.get(User, user.id)
        u.user_profile_memory = {"training_preferences": [
            {"id": "x1", "text": "warms up with leg extensions and leg curls before squatting on leg day", "ts": "t", "uses": 0}]}
        s.commit()
    finally:
        s.close()
    _coach_said(user, "last thing — anything actually bugging u rn, knees shoulders back, anything u gotta work around?")
    sms_capture.clear()
    caplog.set_level(logging.INFO)
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "my bad, u did — the leg curl warmup thing.")

    assert oa.handle_onboarding_reply(user, "I already told you the thing about the knees?") is True, "injuries was the last field"
    row = _row(db, user.id)
    assert row.injuries == "warms up with leg extensions and leg curls before squatting on leg day"
    assert "ONBOARDING_INJURIES_FROM_MEMORY" in caplog.text


def test_already_told_you_without_a_joint_memory_changes_nothing(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    user = _all_but(db, "injuries", wake_time="12:00", sleep_time="02:00")
    _coach_said(user, "anything bugging u?")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "my bad — so anything bugging u or all good?")
    assert oa.handle_onboarding_reply(user, "I already told you") is False
    assert _row(db, user.id).injuries is None


def test_extractor_is_told_a_joint_workaround_is_the_injuries_answer(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    user = _all_but(db, "injuries")
    _coach_said(user, "what split u run and anything that hurts or u gotta work around")
    seen = []
    anthropic_stub.reply_with(lambda kw: (seen.append(kw) or "{}") if _is_extract(kw) else "ok")
    oa.handle_onboarding_reply(user, "on leg day I warm up with leg extensions and leg curls first")
    blob = json.dumps([k.get("system") for k in seen] + [k["messages"] for k in seen], default=str)
    assert "A warm-up or work-around they do for a joint IS the injuries answer" in blob
