"""Live 2026-10-16 10:04–10:07 PT (user 66, Aditi): "how is the privacy of this conversation?"
then "what information does cued store" — each got one thin line glued to the same setup ask
("last thing before i show u how this works…"), twice. She stopped replying. A privacy question
now pauses setup for that reply: full answer, no appended ask. Plus the money rule rewrite."""
from __future__ import annotations

import logging

import pytest

import config
from tests.tier1.test_berkeley_friend_onboarding import _is_extract, _new_signup
from tests.tier1.test_onboarding_restructure_1 import _bodies, _coach_said, _flags  # noqa: F401


@pytest.mark.parametrize("text", [
    "how is the privacy of this conversation?",
    "what information does cued store",
    "is this convo private",
    "do you sell my data",
    "who can see these texts",
])
def test_privacy_questions_are_recognised(text):
    import onboarding_agent as oa
    assert oa.is_privacy_question(text)


@pytest.mark.parametrize("text", ["i cook myself", "how's the gym rn", "4k", "im a TA at presto, mostly sitting"])
def test_ordinary_setup_answers_are_not(text):
    import onboarding_agent as oa
    assert not oa.is_privacy_question(text)


def test_privacy_turn_pauses_setup_and_does_not_append_the_ask(db, anthropic_stub, sms_capture, caplog):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=4, weight_lbs=130, occupation="TA")
    _coach_said(user, "nice, cooking at home is the move. anything that hurts, and when r u up / when do u crash?")
    sms_capture.clear()
    seen = {}

    def _handler(kw):
        if _is_extract(kw):
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        return ("everything stays on ur account — ur stats, what u log, and our convo — so the coaching fits u. "
                "i never sell or share it. once setup's done u get a profile link to see all of it, and u can tell me to delete any of it anytime.")
    anthropic_stub.reply_with(_handler)
    caplog.set_level(logging.INFO)

    oa.handle_onboarding_reply(user, "how is the privacy of this conversation?")
    assert "PRIVACY TURN" in seen["instruction"]
    assert "End with ONE question" not in seen["instruction"]
    out = _bodies(sms_capture)
    assert len(out) == 1 and "also," not in out[0] and "?" not in out[0]
    assert "ONBOARDING_PRIVACY_TURN" in caplog.text and "ONBOARDING_QUESTION_APPENDED" not in caplog.text


def test_ordinary_turn_still_gets_the_question_backstop(db, anthropic_stub, sms_capture, caplog):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=4, weight_lbs=130, occupation="TA")
    _coach_said(user, "do u cook most nights or is it dining hall?")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "nice, cooking at home is the move.")
    caplog.set_level(logging.INFO)
    oa.handle_onboarding_reply(user, "i cook myself")
    out = _bodies(sms_capture)
    assert len(out) == 1 and "also," in out[0] and out[0].endswith("?")
    assert "ONBOARDING_QUESTION_APPENDED" in caplog.text


def test_identity_money_rule_rewritten():
    from agent_loop import identity_prompt
    idp = identity_prompt()
    assert "Don't assume they're broke" in idp
    assert "might not have the most money" in idp and "financially" in idp
    assert "They're broke students" not in idp
    assert "friend who's also broke" not in idp
