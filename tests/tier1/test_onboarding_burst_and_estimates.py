"""
Onboarding fixes from the from-zero founder account (2026-10-05, user 48):

  1. A burst ("Lwk a combo of both" / "I'm either walking somewhere or in my room", 7s
     apart) became two turns and the cooking question was asked twice, 11s apart; same
     for "cooked how". A turn whose inbound the coach has already replied past, or that
     lands right after the coach's line and adds no fact, is a CONTINUATION: one line
     or silence, never a question.
  2. Wake/sleep was asked three times. A qualitative answer to a wake/sleep ask takes
     the coach's own proposed times; after two asks a descriptor default lands.
  3. The summary read "fat loss,muscle building,strength".
  4. "Ok?" was swallowed as a closing ack.
  5. "So what now" ×3 → "nothing til ur at the gym": the loop gets a NEW HERE block.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import INTAKE, _is_extract


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _msg(db, user, direction, body, *, ago_s=0, message_type="onboarding"):
    from models import Message
    m = Message(user_id=user.id, direction=direction, body=body,
                message_type=message_type if direction == "out" else "freeform",
                created_at=_utcnow() - timedelta(seconds=ago_s))
    db.add(m); db.commit()
    return m


def _step2(db, **over):
    kw = dict(INTAKE, name="Edwin", age=20, goal="fat_loss,muscle_building,strength", experience="intermediate",
              onboarding_step=2, height_ft=5, height_in=6, weight_lbs=139, occupation="student")
    kw.update(over)
    return make_user(db, **kw)


def _outbound(db, user):
    from models import Message
    db.expire_all()
    return [(m.body, m.message_type) for m in db.query(Message)
            .filter(Message.user_id == user.id, Message.direction == "out").order_by(Message.id).all()]


# ── 1. the continuation guard ─────────────────────────────────────────────────

def test_a_message_the_coach_already_replied_past_gets_no_second_question(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    u = _step2(db)
    # their first text, then the coach's reply (which asked the cooking question)
    _msg(db, u, "in", "Lwk a combo of both", ago_s=30)
    _msg(db, u, "out", "real. do u cook in ur room or is it dining hall mostly", ago_s=12)
    # the second half of their thought arrived BEFORE that reply went out (7s after the first)
    _msg(db, u, "in", "I’m either walking somewhere or in my room", ago_s=23)
    seen = {}

    def _h(kw):
        if _is_extract(kw):
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        return "[silent]"
    anthropic_stub.reply_with(_h)
    assert oa.handle_onboarding_reply(u, "I’m either walking somewhere or in my room") is False
    assert sms_capture == [], "nothing sent — the cooking question already out there stands"
    ins = seen["instruction"]
    assert "STANDS" in ins and "Do NOT ask" in ins and "do u cook in ur room" in ins


def test_a_covered_message_may_get_one_line_but_never_counts_as_a_turn(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    u = _step2(db)
    _msg(db, u, "in", "I just run ppl", ago_s=40)
    _msg(db, u, "in", "And idk, sleep schedule is cooked", ago_s=25)
    _msg(db, u, "out", "cooked how, like 3am and up at 11 or all over the place", ago_s=12)   # saw both

    def _h(kw):
        return "{}" if _is_extract(kw) else "ha fair"
    anthropic_stub.reply_with(_h)
    before = oa._coach_turns(u.id)
    assert oa.handle_onboarding_reply(u, "And idk, sleep schedule is cooked") is False
    assert [b for _p, b in sms_capture] == ["ha fair"]
    assert _outbound(db, u)[-1] == ("ha fair", oa.FOLLOWUP_MESSAGE_TYPE)
    assert oa._coach_turns(u.id) == before, "a follow-up line is not an intake turn"


def test_a_short_reply_that_arrived_after_the_coach_line_is_a_real_turn(db, anthropic_stub, sms_capture):
    """'lol' 20s after the question is a reply to it, not a continuation — the coach is
    free to carry on (and ask)."""
    import onboarding_agent as oa
    u = _step2(db)
    _msg(db, u, "out", "cooked how, like 3am and up at 11 or all over the place", ago_s=20)
    _msg(db, u, "in", "lol", ago_s=1)
    seen = {}

    def _h(kw):
        if _is_extract(kw):
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        return "lol ok but which"
    anthropic_stub.reply_with(_h)
    oa.handle_onboarding_reply(u, "lol")
    assert "ONE question" in seen["instruction"] and "STANDS" not in seen["instruction"]


def test_an_answer_with_a_new_fact_is_a_normal_turn(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    from models import User
    u = _step2(db)
    _msg(db, u, "in", "Yeah I have one", ago_s=40)
    _msg(db, u, "out", "nice, how many days a week u training", ago_s=20)
    _msg(db, u, "in", "4 days a week", ago_s=1)
    seen = {}

    def _h(kw):
        if _is_extract(kw):
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        return "4's a good number. what time do u usually go"
    anthropic_stub.reply_with(_h)
    assert oa.handle_onboarding_reply(u, "4 days a week") is False
    db.expire_all()
    assert str(db.get(User, u.id).workout_days) == "4"
    assert "ONE question" in seen["instruction"] and "STANDS" not in seen["instruction"]
    assert _outbound(db, u)[-1][1] == "onboarding"


def test_guard_flag_off_restores_the_old_behaviour(db, anthropic_stub, sms_capture, monkeypatch):
    import config, onboarding_agent as oa
    monkeypatch.setattr(config, "ONBOARDING_CONTINUATION_GUARD_ENABLED", False)
    u = _step2(db)
    _msg(db, u, "in", "Lwk a combo of both", ago_s=30)
    _msg(db, u, "out", "real. do u cook in ur room or is it dining hall mostly", ago_s=12)
    _msg(db, u, "in", "I’m either walking somewhere or in my room", ago_s=23)
    seen = {}

    def _h(kw):
        if _is_extract(kw):
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        return "ha so either moving or parked"
    anthropic_stub.reply_with(_h)
    oa.handle_onboarding_reply(u, "I’m either walking somewhere or in my room")
    assert "ONE question" in seen["instruction"]


# ── 2. wake/sleep: take the estimate ─────────────────────────────────────────

@pytest.mark.parametrize("coach, want", [
    ("cooked how, like 3am and up at 11 or all over the place", ("11:00", "03:00")),
    ("lol classic student schedule. whats late tho, like 3am up at noon", ("12:00", "03:00")),
    ("so like midnight and up at 8?", ("08:00", "00:00")),
    ("u a 10pm-6am person or more of a 1am-9am one", ("06:00", "22:00")),
    ("when do u usually get up", (None, None)),
])
def test_times_the_coach_floated_are_read(coach, want):
    from onboarding_agent import _times_proposed
    assert _times_proposed(coach) == want


def test_a_qualitative_yes_to_a_proposed_schedule_takes_the_proposal(db, anthropic_stub, sms_capture):
    """Live: 'Yeah like staying up hella late and waking up late too' → asked AGAIN."""
    import onboarding_agent as oa
    from models import User
    u = _step2(db, workout_days=4, workout_time="18:00", current_split="ppl", injuries="none",
               avg_steps=10000, cooking_situation="cooks", diet="omnivore", existing_tools="none",
               activity_level="moderate")
    _msg(db, u, "in", "And idk, sleep schedule is cooked", ago_s=60)
    _msg(db, u, "out", "cooked how, like 3am and up at 11 or all over the place", ago_s=50)
    _msg(db, u, "in", "Yeah like staying up hella late and waking up late too", ago_s=1)

    def _h(kw):
        return "{}" if _is_extract(kw) else "locked in"
    anthropic_stub.reply_with(_h)
    oa.handle_onboarding_reply(u, "Yeah like staying up hella late and waking up late too")
    db.expire_all()
    row = db.get(User, u.id)
    assert (row.wake_time, row.sleep_time) == ("11:00", "03:00")
    # wake/sleep was the last unknown → the code summary closed onboarding, with the goal in words
    bodies = [b for _p, b in sms_capture]
    summary = [b for b in bodies if b.startswith("ok so ")]
    assert summary and "up at 11 down by 3" in summary[0] and "recomp + getting stronger" in summary[0], bodies
    assert row.onboarding_step == 3


def test_after_two_asks_with_no_clock_a_descriptor_default_lands(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    from models import User
    u = _step2(db)
    _msg(db, u, "out", "whats ur sleep schedule like", ago_s=90)
    _msg(db, u, "in", "idk it's random", ago_s=80)
    _msg(db, u, "out", "random how — when do u usually get up", ago_s=40)
    _msg(db, u, "in", "honestly hella late most days", ago_s=1)

    def _h(kw):
        return "{}" if _is_extract(kw) else "ok night owl, noted"
    anthropic_stub.reply_with(_h)
    oa.handle_onboarding_reply(u, "honestly hella late most days")
    db.expire_all()
    row = db.get(User, u.id)
    assert (row.wake_time, row.sleep_time) == oa.LATE_DEFAULT


def test_one_ask_and_no_proposal_still_asks(db, anthropic_stub, sms_capture):
    import onboarding_agent as oa
    from models import User
    u = _step2(db)
    _msg(db, u, "in", "Yeah I have one", ago_s=60)
    _msg(db, u, "out", "when do u usually get up and crash", ago_s=50)
    _msg(db, u, "in", "idk depends", ago_s=1)

    def _h(kw):
        return "{}" if _is_extract(kw) else "fair. roughly tho — like 8ish or more like noon"
    anthropic_stub.reply_with(_h)
    oa.handle_onboarding_reply(u, "idk depends")
    db.expire_all()
    row = db.get(User, u.id)
    assert row.wake_time is None and row.sleep_time is None


def test_a_clock_answer_is_not_overridden_by_an_estimate(db):
    import onboarding_agent as oa
    u = _step2(db)
    row = type("R", (), {"id": u.id, "wake_time": None, "sleep_time": None})()
    assert oa._maybe_estimate_wake_sleep(row, "like 2am and 10am", "cooked how, like 3am and up at 11") is None


# ── 3. goal words ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("goal, phrase, label", [
    ("fat_loss,muscle_building,strength", "recomp + getting stronger", "recomp + getting stronger"),
    ("fat_loss,muscle_building", "recomp", "recomp"),
    ("strength", "getting stronger", "getting stronger"),
    ("fat_loss", "cutting", "losing fat"),
    ("muscle_building,endurance", "building muscle + endurance", "building muscle + building endurance"),
    ("", "general fitness", "your goal"),
])
def test_goal_lists_read_as_words(goal, phrase, label):
    from onboarding_agent import _goal_phrase, _goal_label
    assert _goal_phrase(goal) == phrase and _goal_label(goal) == label


# ── 4. "Ok?" is not a closing ack ─────────────────────────────────────────────

def test_a_question_mark_ack_is_a_prompt_not_a_closer():
    from app import is_closing_acknowledgment
    assert is_closing_acknowledgment("ok") is True
    assert is_closing_acknowledgment("Ok") is True
    assert is_closing_acknowledgment("Ok?") is False
    assert is_closing_acknowledgment("cool?") is False
    assert is_closing_acknowledgment("got it") is True


# ── 5. NEW HERE in the coach loop ─────────────────────────────────────────────

def test_new_here_block_for_the_first_hours_only(db):
    from agent_loop import _new_here_block
    import config
    u = make_user(db, onboarding_step=3)
    u.created_at = _utcnow() - timedelta(hours=2)
    blk = _new_here_block(u)
    assert blk and blk.startswith("## NEW HERE (finished setup ~2h ago)") and "what now" in blk
    assert "Never answer \"nothing til ur at the gym\"" in blk
    u.created_at = _utcnow() - timedelta(hours=config.NEW_USER_ORIENTATION_HOURS + 1)
    assert _new_here_block(u) is None
    u.created_at = _utcnow() - timedelta(minutes=10)
    u.onboarding_step = 2
    assert _new_here_block(u) is None


def test_new_here_block_is_in_the_loop_context(db):
    from agent_loop import build_loop_context
    from models import get_session, User
    u = make_user(db, onboarding_step=3)
    s = get_session()
    try:
        row = s.get(User, u.id)
        row.created_at = _utcnow() - timedelta(hours=1)
        s.commit()
        assert "## NEW HERE" in build_loop_context(row, s)
        row.created_at = _utcnow() - timedelta(days=5)
        s.commit()
        assert "## NEW HERE" not in build_loop_context(row, s)
    finally:
        s.close()
