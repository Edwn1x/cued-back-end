"""
Paul's thread (user 88, 2026-10-28 21:31–22:32 PT): four fixes.

1. A reply answered in code (the program-days yes, the water yes/no, …) still advances the
   setup sequence — "sounds good" → "locked in" and a bare return left no calendar offer,
   no water, no card until the sweep cleared the heartbeat's 30-min gate (app._code_reply_done).
2. "I have no injury. The only thing is my right shoulder feels weird sometimes" is not
   injuries='none' in code — the caveat is the extractor's to read.
3. Every time box ticked on the form reads "any time of day", not "evenings or mornings or afternoons".
4. The setup-parked first-card ask outlives the 3h TTL (asked once, must still land next day);
   past the TTL only a strong "no clue" takes it, a bare "nah" is the coach's question's answer.
"""
from __future__ import annotations

import pytest

from tests.factories import make_user
from tests.tier1.test_program_builder import _brief_user  # noqa: F401
from tests.tier1.test_water_offer import offer_on  # noqa: F401


@pytest.fixture
def setup_calls(monkeypatch):
    import setup_sequence
    got: list = []
    monkeypatch.setattr(setup_sequence, "on_inbound", lambda uid: got.append(uid) or "connect:gcal")
    return got


def test_program_days_yes_advances_the_setup_sequence(db, sms_capture, driver, anthropic_stub, setup_calls):
    from workouts.program import send_program_brief
    u = _brief_user(db)
    assert send_program_brief(u.id) is True
    sms_capture.clear()
    replies = driver.send(u, "sounds good")
    assert replies and replies[0].startswith("locked in."), replies
    assert anthropic_stub.calls == [], "the yes stays in code"
    assert setup_calls == [u.id], "…and the setup sequence advances, as after any turn"


def test_water_yes_advances_the_setup_sequence(db, offer_on, driver, anthropic_stub, setup_calls):
    from water_offer import send_offer, YES_REPLY
    u = make_user(db, wake_time="07:00", sleep_time="23:00")
    send_offer(u.id)
    assert driver.send(u, "yes") == [YES_REPLY.format(n=3)]
    assert anthropic_stub.calls == []
    assert setup_calls == [u.id]


@pytest.mark.parametrize("msg, want", [
    ("I have no injury. The only thing is my right shoulder feels weird sometimes from volleyball.", {}),
    ("no injuries but my knee clicks on squats", {}),
    ("nothing hurts, lower back gets tight sometimes", {}),
    ("no injuries", {"injuries": "none"}),
    ("4x a week, nothing hurts", {"workout_days": "4", "injuries": "none"}),
    ("nah nothing hurts, i'm back in the gym after summer", {"injuries": "none"}),   # "back" the adverb is not a joint
])
def test_a_no_with_a_caveat_is_not_none_in_code(msg, want):
    from onboarding_agent import parse_stats
    assert parse_stats(msg) == want


def test_every_time_box_ticked_reads_any_time_of_day(db):
    from training_time import describe, parse_form_value
    assert parse_form_value(["evening", "morning", "afternoon"]) == ("18:00", {"slots": ["18:00", "08:00", "14:00"]})
    u = make_user(db, workout_time="18:00", workout_times={"slots": ["18:00", "08:00", "14:00"]})
    assert describe(u) == "any time of day"
    two = make_user(db, phone="+15550002222", workout_time="08:00", workout_times={"slots": ["08:00", "18:00"]})
    assert describe(two) == "mornings or evenings"


def test_setup_ask_outlives_the_ttl_and_only_a_strong_answer_takes_it_late(db, monkeypatch):
    import workouts.calibrate as cal
    from workouts.calibrate import (set_pending_card, peek_pending_card, peek_pending_setup, pending_card_is_fresh,
                                    handle_pending_card_reply, pop_pending_card)
    import workouts.start as start_mod
    started: list = []
    monkeypatch.setattr(start_mod, "start_workout_session",
                        lambda uid, key, **kw: started.append((key, kw)) or {"session_id": 1, "surface": "card"})
    monkeypatch.setattr(start_mod, "send_defaults_note", lambda uid, sr: None)
    # a coach's mid-conversation ask lapses at the TTL, as before
    coach = make_user(db)
    set_pending_card(coach.id, "push")
    monkeypatch.setattr(cal, "PENDING_CARD_TTL_S", -1)
    assert peek_pending_card(coach.id) is None and pop_pending_card(coach.id) is None
    # the setup ask is still pending past the TTL — asked once, never re-asked, still answerable
    u = make_user(db, phone="+15550003333")
    set_pending_card(u.id, "upper", setup=True)
    assert peek_pending_card(u.id) == "upper" and peek_pending_setup(u.id) is True
    assert pending_card_is_fresh(u.id) is False
    assert handle_pending_card_reply(u.id, "nah") is False, "a bare no, days later, is not this ask's answer"
    assert handle_pending_card_reply(u.id, "lol idk") is False
    assert started == [] and peek_pending_card(u.id) == "upper"
    assert handle_pending_card_reply(u.id, "no clue honestly") is True
    assert started == [("upper", {"no_anchors": True, "setup": True})]
    # fresh: the weak forms still answer, as before
    monkeypatch.setattr(cal, "PENDING_CARD_TTL_S", 3 * 3600)
    v = make_user(db, phone="+15550004444")
    set_pending_card(v.id, "upper", setup=True)
    assert pending_card_is_fresh(v.id) is True
    assert handle_pending_card_reply(v.id, "nah") is True
