"""
Live 2026-10-27 (user 77, setup): two coach replies asked their question mid-sentence after a
comma, with no "?" — and the always-ask backstop, seeing "no question", appended the plain ask:

  "swell lol. im assuming ur a berkeley student, what major and any part time job also, what u
   do (im assuming berkeley student — what major, any part time job)?"
  "…i get that ur sleep is all over the place, but on most nights roughly when do u crash and
   when r u up also, roughly when ur up and when u crash?"

_question_part now reads an embedded question lead in the CLOSING sentence; a clause that
reports what they told us is not an ask; mid-reply statements are untouched.
"""

from __future__ import annotations

import logging

import pytest

import onboarding_agent as oa

LIVE_1 = "swell lol. im assuming ur a berkeley student, what major and any part time job"
LIVE_2 = ("if it might be a fracture, get it x-rayed at the tang center this week, don't just wait it out, "
          "and keep heavy weight out of that hand till they say it's ok. i get that ur sleep is all over "
          "the place, but on most nights roughly when do u crash and when r u up")


def test_the_two_live_replies_are_read_as_questions():
    assert oa._question_part(LIVE_1) == "im assuming ur a berkeley student, what major and any part time job"
    assert oa._question_part(LIVE_2).startswith("i get that ur sleep is all over the place, but on most nights")
    assert "occupation" in oa.question_topics(LIVE_1)
    assert "wake_sleep" in oa.question_topics(LIVE_2)


@pytest.mark.parametrize("text", [
    "yeah physics does that, so the gym's gonna do a lot for u",
    "ok so meal plan, that's easy since it's already paid for",
    "7000's not bad for someone at a desk all day, so walking to class is doing some work",
    "here's ur first card, push day. starting u at 165 on bench press — weights are off what u told me",
    "ok ima go with up at 7, down around midnight, and u can fix it anytime. 7 hours is decent on those days",
    "the meal plan's already paid for tho, so a bigger plate at the dining hall doesn't cost u anything extra",
    "cs, so ur basically living in soda.",
])
def test_statements_still_ask_nothing(text):
    assert oa._question_part(text) == "", text


def test_only_the_closing_sentence_is_read_for_an_embedded_lead():
    """A mid-reply statement with a lead after a comma stays a statement: the duplicate-topic
    guard must not see a 'question' there."""
    text = "ur up at 7, so when do u eat is on u. anyway, nice pset grind"
    assert oa._question_part(text) == ""
    assert oa._question_part("nice. ur up at 7, so when do u usually eat") != ""


def test_strip_questions_drops_the_embedded_ask_too():
    assert oa.strip_questions(LIVE_1) == "swell lol."


def test_flag_off_restores_the_old_reading(monkeypatch):
    import config
    monkeypatch.setattr(config, "ONBOARDING_EMBEDDED_QUESTION_ENABLED", False)
    assert oa._question_part(LIVE_1) == ""


def test_backstop_no_longer_appends_a_second_copy(db, anthropic_stub, sms_capture, caplog):
    """End to end: the model asks the major after a comma → one question goes out, nothing glued on."""
    from tests.tier1.test_vague_sleep_code_wins import _new_signup, _coach_said, _is_extract, _bodies
    caplog.set_level(logging.INFO)
    user = _new_signup(db, onboarding_step=2, height_ft=6, height_in=2, weight_lbs=165)
    _coach_said(user, "hey, it's cued. how's ur day going")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else LIVE_1)
    oa.handle_onboarding_reply(user, "Swell")
    sent = _bodies(sms_capture)[-1]
    assert sent == LIVE_1, sent
    assert "ONBOARDING_QUESTION_APPENDED" not in caplog.text
