"""
Live 2026-10-27, user 77:
  11:41 "All time PRs: Bench: 185, squat: 3 plates. This morning I did 3x10 of 135 on bench and 30
        lbs tricep extension." → the keyword classifier set session_state at_gym (2h), though the
        session was hours over. Gym beats, the heartbeat and the session guards read that state.
  11:42 "No… It's Tuesday lunch" landed mid-turn (a correction to "today's breakfast was the sandwich
        w pepper jack, so i'll add a slice of cheese"); the continuation turn logged his lifts and
        never touched the meal — no fix, no acknowledgement.
"""

from __future__ import annotations

import pytest

from tests.factories import make_user
from tests.tier1.test_alex_onboarding_repair import ALEX
from tests.tier1.test_burst_replay import _they_said, LIVE_6224

LIVE_REPORT = ("All time PRs: Bench: 185, squat: 3 plates. This morning I did 3x10 of 135 on bench and "
               "30 lbs tricep extension.")


def _coach_said(user, *texts, message_type="freeform"):
    from models import get_session, Message
    s = get_session()
    try:
        for t in texts:
            s.add(Message(user_id=user.id, direction="out", body=t, message_type=message_type))
        s.commit()
    finally:
        s.close()


# ─── 1. a past-tense report is a confirmed workout, not "at the gym" ────────────

@pytest.mark.parametrize("text,past", [
    (LIVE_REPORT, True),
    ("this morning i hit bench 135 3x10", True),
    ("my bench pr is 185", True),
    ("just got back, did legs", True),
    ("hit 185 on bench today", False),
    ("bench 190x4 done", False),
    ("at the gym, starting push", False),
])
def test_past_report_regex(text, past):
    from app import is_past_workout_report
    assert is_past_workout_report(text) is past, text


def test_live_report_confirms_the_day_but_does_not_set_at_gym(db, driver, anthropic_stub, monkeypatch, caplog):
    import config, logging
    caplog.set_level(logging.INFO)
    from app import classify_message
    from models import get_session_state, is_workout_confirmed_today
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", False)
    assert classify_message(LIVE_REPORT) == "workout_log"
    user = make_user(db, **ALEX, onboarding_step=3, diet="omnivore")
    anthropic_stub.reply_with(lambda kw: "nice")
    driver.send(user, LIVE_REPORT)
    db.expire_all()
    assert get_session_state(user.id) is None
    assert is_workout_confirmed_today(user.id) is True
    assert "AT_GYM_SKIPPED_PAST_REPORT" in caplog.text


def test_a_live_lift_text_still_sets_at_gym(db, driver, anthropic_stub, monkeypatch):
    import config
    from models import get_session_state
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", False)
    user = make_user(db, **ALEX, onboarding_step=3, diet="omnivore")
    anthropic_stub.reply_with(lambda kw: "nice")
    driver.send(user, "hit 185 on bench today")
    db.expire_all()
    assert (get_session_state(user.id) or {}).get("status") == "at_gym"


def test_flag_off_restores_at_gym_on_a_report(db, driver, anthropic_stub, monkeypatch):
    import config
    from models import get_session_state
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", False)
    monkeypatch.setattr(config, "AT_GYM_SKIPS_PAST_REPORT_ENABLED", False)
    user = make_user(db, **ALEX, onboarding_step=3, diet="omnivore")
    anthropic_stub.reply_with(lambda kw: "nice")
    driver.send(user, LIVE_REPORT)
    db.expire_all()
    assert (get_session_state(user.id) or {}).get("status") == "at_gym"


# ─── 2. a mid-turn correction is acted on ───────────────────────────────────────

def _block(db, user, text):
    from agent_loop import _continuation_block
    from models import get_session, User
    s = get_session()
    try:
        return _continuation_block(s.get(User, user.id), text, s)
    finally:
        s.close()


def test_correction_continuation_is_flagged_and_never_silent(db):
    from agent_loop import CONTINUATION_HDR, _CONTINUATION_CORRECTION_RE
    from agent_tools import REACTION_ONLY_SENTINEL
    u = make_user(db, onboarding_step=3, name="Alexey")
    _coach_said(u, "i logged it: 840 cal, 38g protein. fix anything i got wrong: about 3 eggs, 2½ strips of bacon…",
                "today's breakfast was the sandwich w pepper jack, so i'll add a slice of cheese to it unless u skipped it",
                "lmk if the layout's confusing or u want me to walk u through it")
    blk = _block(db, u, "No… It's Tuesday lunch")
    assert blk.startswith(CONTINUATION_HDR) and "NOT a fresh prompt" in blk
    assert "This reads as a CORRECTION" in blk and "manage_log" in blk
    assert f"Never {REACTION_ONLY_SENTINEL} on a correction" in blk
    assert "the replies just before that" in blk and "slice of cheese" in blk, "the corrected reply is quoted even behind code bubbles"
    for t in ("not that, it was dinner", "actually that was lunch", "nah i meant 2 eggs", "that's wrong, it's 3pm"):
        assert _CONTINUATION_CORRECTION_RE.search(t), t


def test_a_plain_tail_gets_no_correction_hint(db):
    u = make_user(db, onboarding_step=3, name="Nau")
    _coach_said(u, LIVE_6224)
    blk = _block(db, u, "Like look at the time rn")
    assert "CORRECTION" not in blk and "ur last reply went out AFTER it" in blk and "up around 11 down around 2" in blk


def test_hint_flag_off(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "CONTINUATION_CORRECTION_HINT_ENABLED", False)
    u = make_user(db, onboarding_step=3, name="Alexey")
    _coach_said(u, "today's breakfast was the sandwich w pepper jack")
    assert "CORRECTION" not in _block(db, u, "No… It's Tuesday lunch")
