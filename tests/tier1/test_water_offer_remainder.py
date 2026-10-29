"""
A water-offer yes/no with more in the same text: the yes is answered in code, the REST
still reaches the coach.

Live 2026-10-28 (user 80, Andrew): the offer was pending; he sent "yeah sure. I'm pretty
sick at the moment…" and got only "bet, every ~3h while ur up" — the code branch returned
after the yes and the sick part was never read. He had to say it again.

water_offer.remainder_after_ack strips the leading ack (and an "every N hours" clause,
which the yes consumes) and returns what's left when it is a real sentence; app.py sends
the code line, then continues the turn with the remainder as the inbound.
"""
from __future__ import annotations

import pytest

import config
from tests.factories import make_user


@pytest.fixture
def offer_on(monkeypatch):
    for f in ("WATER_OFFER_ENABLED", "WATER_REMINDERS_ENABLED", "REMINDERS_ENABLED", "SINGLE_AGENT_LOOP_ENABLED"):
        monkeypatch.setattr(config, f, True)


def test_knob_defaults():
    assert config.WATER_OFFER_REMAINDER_ENABLED is True
    assert config.WATER_OFFER_REMAINDER_MIN_WORDS == 3


@pytest.mark.parametrize("text,rest", [
    ("yeah sure. I'm pretty sick at the moment, can we go easy this week", "I'm pretty sick at the moment, can we go easy this week"),
    ("yes", None),
    ("sure, every 2 hours", None),
    ("ok every 3 hrs. also my knee hurts when i squat", "also my knee hurts when i squat"),
    ("nah. but can u send tonight's card early", "but can u send tonight's card early"),
    ("yep thanks", None),
    ("yeah sure thing", None),
])
def test_remainder_after_ack(text, rest):
    from water_offer import remainder_after_ack
    assert remainder_after_ack(text) == rest


def test_the_yes_is_answered_in_code_and_the_rest_reaches_the_coach(db, offer_on, driver, anthropic_stub):
    from water_offer import send_offer, YES_REPLY
    u = make_user(db, wake_time="07:00", sleep_time="23:00")
    send_offer(u.id)
    anthropic_stub.push("rest up. we'll keep this week light, no card till ur breathing right")
    replies = driver.send(u, "yeah sure. I'm pretty sick at the moment, can we go easy this week")
    assert replies[0] == YES_REPLY.format(n=3), replies
    assert replies[1:] == ["rest up. we'll keep this week light, no card till ur breathing right"], replies
    assert anthropic_stub.calls, "the remainder must reach the model"
    sent = str(anthropic_stub.calls[-1]["messages"][-1]["content"])
    assert "pretty sick" in sent and not sent.lstrip().lower().startswith("yeah sure")
    from models import get_session, User
    s = get_session()
    try:
        assert s.get(User, u.id).water_offer_status == "yes"
    finally:
        s.close()


def test_a_bare_yes_still_never_reaches_the_model(db, offer_on, driver, anthropic_stub):
    from water_offer import send_offer, YES_REPLY
    u = make_user(db, wake_time="07:00", sleep_time="23:00")
    send_offer(u.id)
    assert driver.send(u, "yeah sure") == [YES_REPLY.format(n=3)]
    assert anthropic_stub.calls == []


def test_flag_off_is_the_old_swallow(db, offer_on, driver, anthropic_stub, monkeypatch):
    from water_offer import send_offer, YES_REPLY
    monkeypatch.setattr(config, "WATER_OFFER_REMAINDER_ENABLED", False)
    u = make_user(db, wake_time="07:00", sleep_time="23:00")
    send_offer(u.id)
    assert driver.send(u, "yeah sure. I'm pretty sick at the moment, can we go easy this week") == [YES_REPLY.format(n=3)]
    assert anthropic_stub.calls == []
