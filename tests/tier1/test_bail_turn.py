"""
Bail turn (bail.py): the push rule arrives in the prompt the moment they back out of today's
session — naming the session, the pushback count, whether a real reason was named, and the LEAD.
Live: u40 "Ughhh I don't want toooo" → "lol ok don't then"; u51 "I'll prolly hit back instead ngl"
→ "alr, back monday then"; u56 "i didnt end up goign to work out sorry keep it next time" → card cleared.
"""

from __future__ import annotations

import datetime as dt

import pytest

import config
from tests.factories import make_user

PROG = {"split": "ppl", "days": {"mon": "push", "tue": "pull", "wed": "legs", "thu": "push", "fri": "pull", "sat": "legs", "sun": "push"},
        "times": {}}


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "BAIL_TURN_BLOCK_ENABLED", True)
    monkeypatch.setattr(config, "SCOREBOARD_ENABLED", True, raising=False)


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


@pytest.mark.parametrize("text, bail", [
    ("Ughhh I don't want toooo", True),
    ("I'll prolly hit back instead ngl", True),
    ("i didnt end up goign to work out sorry keep it next time", True),
    ("Not happening / Got plans / Maybe Monday", True),
    ("I dont wanna 🙂 Core instead?", True),
    ("can't go till wednesday", True),
    ("my thumb hurts, skipping pull", True),
    ("starting legs!!!", False),
    ("what should i eat before the gym", False),
    ("nah im good, did it this morning", False),
])
def test_looks_like_bail(text, bail):
    from bail import looks_like_bail
    assert looks_like_bail(text) is bail


def test_real_reasons_are_recognized():
    from bail import real_reason
    assert real_reason("my thumb hurts, skipping pull") == "hurts"
    assert real_reason("i got work to do so i will just go to dance thats my workout for the day") == "dance"
    assert real_reason("got a midterm tmrw, not tonight") == "midterm"
    assert real_reason("Ughhh I don't want toooo") is None


def test_block_fires_on_a_program_day_with_a_push_and_not_on_a_rest_day(db, on):
    from bail import context_block, session_live_today
    u = make_user(db, program=PROG, calorie_target=2500)        # every day is a training day
    blk = context_block(_u(u.id), "Ughhh I don't want toooo", db)
    assert blk and blk.startswith("## BAIL TURN — Test is backing out of today's session (today is")
    assert "PUSH, warmly and for real" in blk and "no 'ok don't then'" in blk and "First pushback" in blk
    assert "SCOREBOARD LEAD" not in blk or "LEAD" in blk
    rest = make_user(db, program={"split": "ppl", "days": {}, "times": {}}, workout_days="", phone="+15550009101")
    assert session_live_today(_u(rest.id), db) is None
    assert context_block(_u(rest.id), "Ughhh I don't want toooo", db) is None


def test_a_planned_card_or_a_gym_ping_makes_the_session_live(db, on):
    from bail import session_live_today
    from models import WorkoutSession, Message
    u = make_user(db, program={"split": "ppl", "days": {}, "times": {}}, workout_days="")
    db.add(WorkoutSession(user_id=u.id, template_key="legs", status="planned", date=_now()))
    db.commit()
    assert "legs card is already on their phone" in session_live_today(_u(u.id), db)
    v = make_user(db, program={"split": "ppl", "days": {}, "times": {}}, workout_days="", phone="+15550009102")
    db.add(Message(user_id=v.id, direction="out", body="gym time", message_type="reminder", channel="imessage", created_at=_now()))
    db.commit()
    assert session_live_today(_u(v.id), db) == "the coach already brought up today's session"


def test_a_real_reason_turns_the_push_into_a_rearm(db, on):
    from bail import context_block
    u = make_user(db, program=PROG, calorie_target=2500)
    blk = context_block(_u(u.id), "my thumb hurts, skipping pull", db)
    assert "They NAMED a real reason (\"hurts\")" in blk and "no push" in blk and "set_reminder for tomorrow" in blk
    assert "PUSH, warmly" not in blk


def test_repeated_pushback_steps_aside_after_two(db, on):
    from bail import context_block
    from models import Message
    u = make_user(db, program=PROG, calorie_target=2500)
    for t in ("Ughhh I don't want toooo", "nahh not today"):
        db.add(Message(user_id=u.id, direction="in", body=t, message_type="freeform", channel="imessage", created_at=_now()))
    db.commit()
    blk = context_block(_u(u.id), "i really don't wanna", db)
    assert "pushback #2" in blk and "if the next reply is a firm no, step aside" in blk


def test_a_done_session_today_or_setup_or_flag_off_means_no_block(db, on, monkeypatch):
    from bail import context_block
    from models import WorkoutSession
    u = make_user(db, program=PROG, calorie_target=2500)
    db.add(WorkoutSession(user_id=u.id, template_key="push", status="done", date=_now(), finished_at=_now()))
    db.commit()
    assert context_block(_u(u.id), "not today", db) is None
    s = make_user(db, program=PROG, onboarding_step=2, phone="+15550009103")
    assert context_block(_u(s.id), "not today", db) is None
    monkeypatch.setattr(config, "BAIL_TURN_BLOCK_ENABLED", False)
    v = make_user(db, program=PROG, phone="+15550009104")
    assert context_block(_u(v.id), "not today", db) is None


def test_the_block_reaches_the_loop(db, on, anthropic_stub):
    from agent_loop import run_agent_loop
    u = make_user(db, program=PROG, calorie_target=2500, preferred_channel="sms")
    seen = {}

    def script(kw):
        sysb = kw.get("system") or ""
        text = "\n".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in sysb) if isinstance(sysb, list) else str(sysb)
        if "BAIL TURN" in text:
            seen["ok"] = True
        return "day one or one day. 20 min, 3 machines, then home. what's actually in the way"
    anthropic_stub.reply_with(script)
    run_agent_loop(_u(u.id), "Ughhh I don't want toooo", "freeform")
    assert seen.get("ok"), "the BAIL TURN block rode the loop's context"
