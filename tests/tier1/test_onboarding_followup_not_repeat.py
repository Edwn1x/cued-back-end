"""
Live 2026-10-28 06:53–06:55 PT, user 80 (Andrew, 26, SMS): "im assuming ur a berkeley student?
what do u study, and any part time job" → "yeah early ass job" → the model's follow-up "damn ur
up before the sun for it lol. what's the job, and r u on ur feet the whole time" shared the
occupation topic with the previous ask, so the repeat guard dropped the question and sent only
"damn ur up before the sun for it lol." — no question on the floor, setup parked.

A same-topic ask is a follow-up when their text talked about that topic and the field is still
open. A text that never touched it (user 49, 2026-10-10) keeps the repeat a repeat.
"""
from __future__ import annotations

import logging

import config
from tests.tier1.test_berkeley_friend_onboarding import _is_extract, _new_signup
from tests.tier1.test_onboarding_restructure_1 import _bodies, _coach_said, reactions, _flags  # noqa: F401 (fixtures)

LIVE_7707 = "lol ok, up early. im assuming ur a berkeley student? what do u study, and any part time job"
LIVE_7708 = "yeah early ass job"
DROPPED = "damn ur up before the sun for it lol. what's the job, and r u on ur feet the whole time"
MISSING = [("occupation", "occupation"), ("activity_level", "activity level"), ("avg_steps", "steps")]


def test_a_follow_up_on_what_they_said_is_not_a_repeat():
    import onboarding_agent as oa
    assert oa.question_topics(LIVE_7707) == {"occupation"}
    assert oa.question_topics(DROPPED) == {"occupation", "activity_level"}
    assert oa.is_duplicate_question(DROPPED, LIVE_7707) is True          # the topic guard alone drops it
    assert oa.followup_topics(DROPPED, LIVE_7707, LIVE_7708, MISSING) == {"occupation"}
    # their text never touched the topic → still a repeat
    assert oa.followup_topics(DROPPED, LIVE_7707, "lol yeah", MISSING) == set()
    # the field got filled this turn → asking again IS a repeat
    assert oa.followup_topics(DROPPED, LIVE_7707, LIVE_7708, MISSING[1:]) == set()


def _andrew(db):
    return _new_signup(db, onboarding_step=2, name="Andrew", age=26, height_ft=5, height_in=11, weight_lbs=320,
                       workout_days="3", workout_time="18:00", current_split="none", diet="omnivore",
                       existing_tools="fitbit", experience="none")


def test_live_user_80_gets_the_follow_up_question(db, anthropic_stub, sms_capture, reactions, caplog):
    import onboarding_agent as oa
    user = _andrew(db)
    _coach_said(user, LIVE_7707)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else DROPPED)
    caplog.set_level(logging.INFO)
    assert oa.handle_onboarding_reply(user, LIVE_7708) is False
    assert _bodies(sms_capture) == [DROPPED]
    assert reactions == []
    assert "ONBOARDING_FOLLOWUP_NOT_REPEAT" in caplog.text and "REPEAT_QUESTION_SUPPRESSED" not in caplog.text


def test_flag_off_is_the_old_guard(db, anthropic_stub, sms_capture, reactions, monkeypatch):
    import onboarding_agent as oa
    monkeypatch.setattr(config, "ONBOARDING_FOLLOWUP_NOT_REPEAT_ENABLED", False)
    user = _andrew(db)
    _coach_said(user, LIVE_7707)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else DROPPED)
    oa.handle_onboarding_reply(user, LIVE_7708)
    assert _bodies(sms_capture) == ["damn ur up before the sun for it lol."]


# ── 2026-10-28 21:26 PT, user 86 (James): the shared topic was ANSWERED this turn ──────────
# "what major and any part time job?" → "Civil engineering and dining hall job" (occupation
# stored) → "civil eng plus a job is a lot ngl. working at the dining hall, do u end up eating
# most of ur meals there too?" — 'working' put occupation in the question's topics, so the
# guard dropped a question that was really about meals (a NEW, still-open topic).

LIVE_8025 = "just fine lol. im assuming ur a berkeley student, what major and any part time job?"
LIVE_8027 = "Civil engineering and dining hall job"
DROPPED_86 = ("civil eng plus a job is a lot ngl. working at the dining hall, do u end up eating most of ur "
              "meals there too?")
MISSING_86 = [("activity_level", "activity level"), ("avg_steps", "steps"), ("cooking_situation", "cooking")]


def test_moving_on_to_a_new_topic_after_the_shared_one_was_answered_is_not_a_repeat():
    import onboarding_agent as oa
    assert oa.question_topics(DROPPED_86) == {"occupation", "cooking_situation"}
    assert oa.is_duplicate_question(DROPPED_86, LIVE_8025) is True
    assert oa.moving_on_topics(DROPPED_86, LIVE_8025, MISSING_86) == {"cooking_situation"}
    # occupation still open → not "moving on" (that's followup_topics' case or a real repeat)
    assert oa.moving_on_topics(DROPPED_86, LIVE_8025, MISSING_86 + [("occupation", "occupation")]) == set()
    # re-asking ONLY what was just answered stays a repeat
    assert oa.moving_on_topics("so what's the job exactly?", LIVE_8025, MISSING_86) == set()


def test_live_user_86_gets_the_meals_question(db, anthropic_stub, sms_capture, reactions, caplog):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, name="James", height_ft=5, height_in=10, weight_lbs=170,
                       workout_days="4", workout_time="18:00", experience="intermediate", diet="omnivore")
    _coach_said(user, LIVE_8025)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: ('{"occupation": "civil engineering student, dining hall job"}'
                                          if _is_extract(kw) else DROPPED_86))
    caplog.set_level(logging.INFO)
    assert oa.handle_onboarding_reply(user, LIVE_8027) is False
    assert _bodies(sms_capture) == [DROPPED_86]
    assert "ONBOARDING_MOVING_ON" in caplog.text and "REPEAT_QUESTION_SUPPRESSED" not in caplog.text


# ── 2026-10-28 21:33 PT, user 86: an "anything …" ask without a "?" read as no question ────
# "…no legs in there at all tho, anything hurt that keeps u off them" → the always-ask backstop
# appended "also, what u hit each day?" — a second ask glued on, right after he'd said push/pull.

LIVE_8046_MODEL = "lol everyone does more push, chest day is just more fun. no legs in there at all tho, anything hurt that keeps u off them"


def test_an_embedded_anything_ask_counts_as_a_question():
    import onboarding_agent as oa
    assert oa._question_part(LIVE_8046_MODEL)
    assert oa._question_part("ok cool, anyone u lift with")
    assert not oa._question_part("ok cool, anything works for me")      # 'anything works' is a statement


def test_live_user_86_gets_no_glued_second_ask(db, anthropic_stub, sms_capture, reactions, caplog):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, name="James", height_ft=5, height_in=10, weight_lbs=170,
                       workout_days="4", workout_time="18:00", experience="intermediate", diet="omnivore",
                       occupation="civil engineering student, dining hall job", avg_steps=10848,
                       activity_level="on feet 12 hrs a week at the dining hall")
    _coach_said(user, "ok that's a good avg. what split u run and which days u hit what")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: ('{"current_split": "push/pull, by feel"}' if _is_extract(kw) else LIVE_8046_MODEL))
    caplog.set_level(logging.INFO)
    oa.handle_onboarding_reply(user, "I usually just go when I feel like it's switching between push and pull but I def do push way more")
    assert _bodies(sms_capture) == [LIVE_8046_MODEL]
    assert "ONBOARDING_QUESTION_APPENDED" not in caplog.text


# ── 2026-10-28 21:35 PT, user 88 (Paul): a hedge word before the ask hid it ─────────────────
# "…roughly how many cal u think u eat in a normal day, a guess is fine, so is no idea" → the
# backstop appended "also, roughly how many cals u eat in a normal day now (…)?" — the SAME ask twice
# in one bubble.

LIVE_8050_MODEL = ("ok house meals that are already mostly protein help a lot, since protein is the main thing for "
                   "building muscle. roughly how many cal u think u eat in a normal day, a guess is fine, so is no idea")


def test_a_hedged_lead_is_still_a_question():
    import onboarding_agent as oa
    assert oa._question_part(LIVE_8050_MODEL)
    for s in ("about how much sleep u getting", "so what split u run", "and when do u usually lift",
              "ok what's food like rn"):
        assert oa._question_part(s), s
    assert not oa._question_part("roughly 2k a day sounds right")          # a hedge alone is no ask


def test_live_user_88_gets_one_intake_ask_not_two(db, anthropic_stub, sms_capture, reactions, caplog):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, name="paul", height_ft=5, height_in=7, weight_lbs=145,
                       workout_days="4", workout_time="18:00", experience="intermediate", diet="omnivore",
                       occupation="business student", avg_steps=5000, activity_level="half walking, half class")
    _coach_said(user, "ok 5000 is on the low side. what's food like for u rn, cooking most nights or mostly dining hall")
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: ('{"cooking_situation": "house meals, protein-based"}' if _is_extract(kw)
                                          else LIVE_8050_MODEL))
    caplog.set_level(logging.INFO)
    oa.handle_onboarding_reply(user, "house meals and usually protein based")
    assert _bodies(sms_capture) == [LIVE_8050_MODEL]
    assert "ONBOARDING_QUESTION_APPENDED" not in caplog.text


def test_a_hedge_before_a_pronoun_is_still_a_statement():
    import onboarding_agent as oa
    assert not oa._Q_LEAD_RE.match("ok u got this")
    assert not oa._Q_LEAD_RE.match("so you're set for tonight")
