"""
Live 2026-11-20 12:19–12:20 PT, user 98 (Brandon, 20, Berkeley, iMessage, profile filled on the
site form): kickoff "…how's ur day going" → 64s later "Great, woke up early and planning on going
to the gym 5:30-6:30 today" → the model's reply "ok an hour tonight is plenty, and friday evening
is easy to keep. how long u been lifting" lost its question: no intake topic on either side, but
the wording scorer put "how long u been lifting" vs "how's ur day going" at 0.49, over the 0.45
floor, inside the 180s window. A bare ack went out and setup parked (nothing was stored that
turn — his gym time was already on file — so the stored-this-turn exemption never applied).

Without a shared topic only near-verbatim wording (the 0.8 tier) is a repeat.
"""
from __future__ import annotations

import logging

from tests.tier1.test_berkeley_friend_onboarding import _is_extract, _new_signup
from tests.tier1.test_onboarding_restructure_1 import _bodies, _coach_said, reactions, _flags  # noqa: F401 (fixtures)

KICKOFF = ("hey Brandon, it's cued. ur in. quick setup before i can actually help: gonna get to know u a bit "
           "over the next few texts, then run u through everything i can do. how's ur day going")
LIVE_11077 = "Great, woke up early and planning on going to the gym 5:30-6:30 today"
DROPPED = "ok an hour tonight is plenty, and friday evening is easy to keep. how long u been lifting"


def test_no_shared_topic_needs_near_verbatim_wording():
    import onboarding_agent as oa
    assert oa.question_topics(DROPPED) & oa.question_topics(KICKOFF) == set()
    assert oa.is_duplicate_question(DROPPED, KICKOFF) is False
    # the same words again are still a repeat without any topic
    assert oa.is_duplicate_question("lol fair. how's ur day going", KICKOFF) is True
    # a shared topic still decides on its own (user 49, 2026-10-10: food asked twice)
    assert oa.is_duplicate_question("ok so food, u cooking or dining hall",
                                    "last thing, u cook most nights or is it dining hall") is True
    # an explicit threshold is the caller's call
    assert oa.is_duplicate_question(DROPPED, KICKOFF, threshold=0.45) is True


def test_live_user_98_keeps_the_question(db, anthropic_stub, sms_capture, reactions, caplog):
    import onboarding_agent as oa
    user = _new_signup(db, onboarding_step=2, name="Brandon", age=20, gender="male", height_ft=5, height_in=9,
                       weight_lbs=130, workout_days="3", workout_time="18:00", diet="kosher", restrictions="Pork",
                       existing_tools="none", tools_decision="none", equipment="full_gym")
    _coach_said(user, KICKOFF)
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else DROPPED)
    caplog.set_level(logging.INFO)
    assert oa.handle_onboarding_reply(user, LIVE_11077) is False
    assert _bodies(sms_capture) == [DROPPED]
    assert reactions == []
    assert "REPEAT_QUESTION_SUPPRESSED" not in caplog.text
