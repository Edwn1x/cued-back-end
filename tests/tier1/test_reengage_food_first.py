"""
Founder, 2026-10-25, the planned attack on stalled sign-ups: the first nudge leads with why
they signed up, says starting is the hard part and the coach is here, and lands on the
easiest highest-ROI thing they can do right now — log today's food — as the only ask. And
the path underneath is rebuilt: before this, a mid-setup user's "a burrito" went to the
intake agent (web_search only) and got "nice! what's ur height and weight" instead of the
numbers. Now it goes to the agent loop (log_meal), and code asks the next intake field only
once a meal is logged, with the reason attached.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from tests.factories import make_user
from tests._fake_anthropic import ToolUse

PT_NOON_UTC = datetime(2026, 10, 20, 19, 0)      # 12:00 PT
PT_8AM_UTC = datetime(2026, 10, 20, 15, 0)
PT_7PM_UTC = datetime(2026, 10, 21, 2, 0)

STALLED = dict(onboarding_step=2, preferred_channel="imessage", user_timezone="America/Los_Angeles", name="Karol",
               goal="muscle_building,fat_loss", occupation=None, activity_level=None, avg_steps=None,
               cooking_situation=None, injuries=None, wake_time=None, sleep_time=None, workout_days="3",
               current_split="none", height_ft=None, weight_lbs=None)


def _msg(db, uid, direction, body, *, at, mtype="onboarding"):
    from models import Message
    db.add(Message(user_id=uid, direction=direction, body=body, channel="imessage", message_type=mtype, created_at=at))
    db.commit()


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _nudged(db, **kw):
    """A stalled user who got nudge #1 twenty minutes ago."""
    u = make_user(db, **dict(STALLED, **kw))
    _msg(db, u.id, "out", "hey, it's cued. how's ur day going", at=_now() - timedelta(hours=30))
    _msg(db, u.id, "in", "good! i cook all my meals at home", at=_now() - timedelta(hours=29))
    _msg(db, u.id, "out", "hey Karol, u signed up to build muscle… what have u eaten so far today?",
         at=_now() - timedelta(minutes=20), mtype="onboarding_nudge")
    return u


# ── copy ────────────────────────────────────────────────────────────────────

def test_the_food_ask_follows_their_clock(db):
    from onboarding_reengage import food_ask
    u = make_user(db, **STALLED)
    assert food_ask(u, PT_8AM_UTC).endswith("what's breakfast?")
    assert "count it up" in food_ask(u, PT_NOON_UTC) and "where ur at" not in food_ask(u, PT_NOON_UTC)   # no target to be 'at' yet
    assert food_ask(u, PT_NOON_UTC).startswith("what have u eaten so far today?")
    assert food_ask(u, PT_7PM_UTC).startswith("what did u eat today so far?") and "dinner" in food_ask(u, PT_7PM_UTC)


def test_goal_phrase_reads_like_a_friend_not_a_form(db):
    from onboarding_reengage import goal_phrase
    assert goal_phrase(make_user(db, **dict(STALLED, goal="muscle_building"))) == "build muscle"
    assert goal_phrase(make_user(db, phone="+15550310001", **dict(STALLED, goal="fat_loss,strength"))) == "lean out and get stronger"
    assert goal_phrase(make_user(db, phone="+15550310002", **dict(STALLED, goal="muscle_building,fat_loss,strength,general_fitness,endurance"))) == "get stronger, leaner and fitter"
    assert goal_phrase(make_user(db, phone="+15550310003", **dict(STALLED, goal=None))) == "get in shape"


def test_model_copy_is_used_when_it_keeps_the_shape_and_the_template_is_the_floor(db, sms_capture, anthropic_stub, monkeypatch):
    import onboarding_reengage as r
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_FIRST_HOURS", 20)
    u = make_user(db, **STALLED)
    _msg(db, u.id, "out", "hey, it's cued. how's ur day going", at=PT_NOON_UTC - timedelta(hours=25))
    _msg(db, u.id, "in", "good! i cook all my meals at home", at=PT_NOON_UTC - timedelta(hours=24))
    good = ("hey Karol, u cook all ur own meals which is honestly the best setup for building muscle and leaning out. "
            "u signed up so u want this\n---\ngetting started is always the hard part, that's what i'm here for. "
            "easiest place to start: what have u eaten so far today? pic or text works")
    anthropic_stub.reply_with(lambda kw: good)
    assert r.sweep(now=PT_NOON_UTC) == 1
    out = [b for _p, b in sms_capture]
    assert len(out) == 2 and out[0].startswith("hey Karol, u cook all ur own meals") and out[1].endswith("pic or text works")
    # the brief carried their own words and the structure
    prompt = str(anthropic_stub.calls[-1]["messages"][0]["content"])
    assert "i cook all my meals at home" in prompt and "never 'if you're serious'" in prompt and "what have u eaten so far today?" in prompt
    # the second bubble is typed so it never counts as a second nudge
    from models import Message
    types = [m.message_type for m in db.query(Message).filter(Message.user_id == u.id, Message.direction == "out").order_by(Message.id)]
    assert types[-2:] == ["onboarding_nudge", "onboarding_nudge_more"]
    from onboarding_reengage import _nudges
    assert len(_nudges(db, u.id)) == 1

    # a dare / setup question / no food ask → rejected → the template goes out instead
    v = make_user(db, phone="+15550310010", **STALLED)
    _msg(db, v.id, "out", "hey, it's cued. how's ur day going", at=PT_NOON_UTC - timedelta(hours=25))
    _msg(db, v.id, "in", "good", at=PT_NOON_UTC - timedelta(hours=24))
    sms_capture.clear()
    anthropic_stub.reply_with(lambda kw: "hey Karol! if you're serious about your fitness journey, lock in. what's ur height and weight?")
    assert r.sweep(now=PT_NOON_UTC) == 1
    out = [b for _p, b in sms_capture]
    assert len(out) == 1 and out[0].startswith("hey Karol, u signed up to build muscle and lean out") and "?" in out[0]
    assert "height" not in out[0] and "eaten" in out[0]


def test_valid_first_nudge_rules():
    from onboarding_reengage import valid_first_nudge
    assert valid_first_nudge("hey Ben, u signed up to build muscle so u want this. starting is the hard part, i'm here. what have u eaten so far today?")
    assert not valid_first_nudge("hey Ben, let's go! what have u eaten today?")                       # exclamation
    assert not valid_first_nudge("hey Ben, we never got going, that's on me. what have u eaten today?")  # apology
    assert not valid_first_nudge("hey Ben, u want this. what's ur height and weight?")               # setup question
    assert not valid_first_nudge("hey Ben, u signed up so u want this. we can get started right now")  # no question
    assert not valid_first_nudge("hey Ben, u signed up so u want this. ready to start?")                # a question, but not the food ask
    assert not valid_first_nudge("a\n---\nb\n---\nwhat did u eat?")                                   # three bubbles
    assert not valid_first_nudge("hey Ben, " + "x" * 400 + " what did u eat?")                        # too long


def test_second_nudge_is_smaller_and_still_about_food(db):
    from onboarding_reengage import nudge_text
    u = make_user(db, **STALLED)
    t = nudge_text(u, 2, replied=True, now=PT_7PM_UTC)
    assert t.startswith("hey Karol, last one from me") and "pic" in t and t.endswith("no stress if not")
    assert len(t) < len(nudge_text(u, 1, replied=True, now=PT_7PM_UTC))


# ── the window ──────────────────────────────────────────────────────────────

def test_food_first_window_opens_on_the_nudge_and_closes_on_a_meal_or_a_timeout(db):
    import onboarding_reengage as r
    from models import Meal
    u = make_user(db, **STALLED)
    assert not r.food_first_active(db, u)                                   # no nudge → intake as usual
    v = _nudged(db, phone="+15550310020")
    assert r.food_first_active(db, v)
    assert r.food_first_active(db, v, now=_now() + timedelta(hours=49)) is False   # window closed
    db.add(Meal(user_id=v.id, description="burrito", calories=650, protein_g=35, carbs_g=70, fat_g=25, logged_at=_now()))
    db.commit()
    assert not r.food_first_active(db, v)                                   # a meal got logged → intake takes over
    w = _nudged(db, phone="+15550310021")
    for i in range(5):
        _msg(db, w.id, "in", f"text {i}", at=_now() - timedelta(minutes=10 - i))
    assert not r.food_first_active(db, w)                                   # five diverted turns is enough
    x = _nudged(db, phone="+15550310022", onboarding_step=3)
    assert not r.food_first_active(db, x)                                   # onboarded → never


def test_the_loop_sees_the_block_only_inside_the_window(db):
    from agent_loop import build_loop_context
    u = _nudged(db)
    ctx = build_loop_context(u, db)
    assert "RE-ENGAGED, STILL MID-SETUP" in ctx and "log_meal it" in ctx and "NO targets yet" in ctx
    v = make_user(db, phone="+15550310030", **STALLED)
    assert "RE-ENGAGED" not in build_loop_context(v, db)


# ── the routing ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _loop_on(monkeypatch):
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)      # prod's value; the test default is off


def test_a_burrito_from_a_nudged_user_gets_logged_and_then_the_next_intake_ask_with_the_reason(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    from models import Meal
    u = _nudged(db)
    called = {"onboarding": 0}
    monkeypatch.setattr(appmod, "handle_onboarding_reply", lambda *a, **k: called.__setitem__("onboarding", called["onboarding"] + 1))
    state = {"n": 0}

    def script(kw):
        state["n"] += 1
        if state["n"] == 1:
            return ToolUse("log_meal", {"description": "chicken burrito", "calories": 650, "protein_g": 35, "carbs_g": 70, "fat_g": 25})
        return "chicken burrito, ~650 cal and 35g protein. easy."
    anthropic_stub.reply_with(script)
    _msg(db, u.id, "in", "a chicken burrito", at=_now())
    appmod.process_buffered_message(u.id, "a chicken burrito", "freeform")
    out = [b for _p, b in sms_capture]
    assert called["onboarding"] == 0                                        # the questionnaire did NOT answer
    assert db.query(Meal).filter(Meal.user_id == u.id).count() == 1        # the meal is real
    assert out[0].startswith("chicken burrito, ~650 cal")
    assert out[1].startswith("that's it, u just text me or send a pic") and "how much u actually need" in out[1]
    assert out[1].endswith("ur height and weight?")                         # the next intake field, with the reason
    # the window is closed now: the next text is intake's
    sms_capture.clear()
    _msg(db, u.id, "in", "5'6 and 150", at=_now())
    appmod.process_buffered_message(u.id, "5'6 and 150", "freeform")
    assert called["onboarding"] == 1


def test_a_yes_to_the_nudge_asks_for_the_food_instead_of_the_next_field(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    from models import Meal
    u = _nudged(db)
    monkeypatch.setattr(appmod, "handle_onboarding_reply", lambda *a, **k: pytest.fail("intake agent must not answer inside the window"))
    anthropic_stub.reply_with(lambda kw: "bet. what have u eaten so far today? pic or text works")
    _msg(db, u.id, "in", "yeah im down", at=_now())
    appmod.process_buffered_message(u.id, "yeah im down", "freeform")
    out = [b for _p, b in sms_capture]
    assert out == ["bet. what have u eaten so far today? pic or text works"]       # no hook line: nothing logged yet
    assert db.query(Meal).filter(Meal.user_id == u.id).count() == 0
    seen = " ".join(str(b.get("text", "")) if isinstance(b, dict) else str(b)
                    for c in anthropic_stub.calls for b in (c.get("system", []) or []))
    assert "RE-ENGAGED, STILL MID-SETUP" in seen


def test_without_a_nudge_or_with_the_flag_off_the_intake_agent_answers_as_before(db, sms_capture, anthropic_stub, monkeypatch):
    import app as appmod
    u = make_user(db, **STALLED)
    called = {"n": 0}
    monkeypatch.setattr(appmod, "handle_onboarding_reply", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    anthropic_stub.reply_with(lambda kw: "nope")
    appmod.process_buffered_message(u.id, "a chicken burrito", "freeform")
    assert called["n"] == 1
    v = _nudged(db, phone="+15550310040")
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_FOOD_FIRST_ENABLED", False)
    appmod.process_buffered_message(v.id, "a chicken burrito", "freeform")
    assert called["n"] == 2
