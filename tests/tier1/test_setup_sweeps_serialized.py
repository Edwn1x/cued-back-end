"""Inside the setup window, one sender: setup_sequence.sweep, one step per tick, in order.

Live 2026-10-10 11:19:48–11:20:02 PT (user 49, first tick after his 11:00 wake estimate):
connect_offers.sweep recorded the gcal offer at :48, water_offer.sweep sent "one more
thing: want me to ping u to drink water…" at :54, the calendar offer landed at :59 and
its link at :02 — water before the calendar, 5 seconds apart, the opposite of the
decided order (offers → water → card, one step per answer or 20 quiet minutes).
setup_sequence.next_step ordered the steps; the other two sweeps never consulted it.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_card_setup import sidecar_ok, card_ok, sync_threads, _u  # noqa: F401
from tests.tier1.test_setup_sequence import seq_on, _user, _inbound, _backdate, _now  # noqa: F401


def _types(uid):
    from models import get_session, Message
    s = get_session()
    try:
        return [m.message_type for m in s.query(Message).filter(Message.user_id == uid, Message.direction == "out")
                .order_by(Message.id).all()]
    finally:
        s.close()


def test_one_tick_sends_one_step_and_the_offer_comes_before_water(db, seq_on, sidecar_ok, card_ok, sync_threads,
                                                                   anthropic_stub):
    import onboarding_agent as oa
    import setup_sequence as ss
    import water_offer, connect_offers
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    _backdate(u.id, 25)                               # quiet since the summary → a step is owed
    before = _types(u.id)

    # the live tick: all three sweeps run in the same minute
    assert water_offer.sweep() == 0, "water is a setup step now — setup_sequence sends it, after the offers"
    assert connect_offers.sweep() == 0, "first offers in the window are setup_sequence's"
    assert ss.sweep() == 1
    after = _types(u.id)[len(before):]
    assert after and after[0] == "connect_offer" and "water_offer" not in after, after

    # same tick again, nothing answered → nothing more
    assert water_offer.sweep() == 0 and connect_offers.sweep() == 0 and ss.sweep() == 0

    # they answer; the conversation goes quiet; the NEXT step is one step
    _inbound(u.id, "ok")
    _backdate(u.id, 35)                               # past the heartbeat's active-conversation gate
    n0 = len(_types(u.id))
    assert water_offer.sweep() == 0 and connect_offers.sweep() == 0
    assert ss.sweep() == 1
    step2 = _types(u.id)[n0:]
    assert len([t for t in step2 if t in ("connect_offer", "water_offer")]) == 1, step2


def test_outside_the_window_the_other_sweeps_send_as_before(db, seq_on, sidecar_ok, card_ok, sync_threads,
                                                             anthropic_stub, monkeypatch):
    import onboarding_agent as oa
    import setup_sequence as ss
    import water_offer
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _user(db)
    oa._complete_onboarding(_u(u.id), "yes")
    _backdate(u.id, 25)
    monkeypatch.setattr(config, "SETUP_WINDOW_HOURS", 0)               # the window is over
    assert ss.owns(_u(u.id)) is False
    assert water_offer.sweep() == 1 and "water_offer" in _types(u.id)


def test_owns_is_false_once_the_card_went_or_the_sequence_is_off(db, seq_on, monkeypatch):
    import setup_sequence as ss
    u = _user(db, onboarding_step=3, onboarding_completed_at=_now() - timedelta(hours=1))
    assert ss.owns(_u(u.id)) is True
    u2 = _user(db, phone="+15550100077", onboarding_step=3, onboarding_completed_at=_now() - timedelta(hours=1),
               card_setup_at=_now())
    assert ss.owns(_u(u2.id)) is False
    monkeypatch.setattr(config, "SETUP_SEQUENCE_ENABLED", False)
    assert ss.owns(_u(u.id)) is False


def test_reconnect_nudges_are_not_held_by_the_window(db, seq_on, sms_capture, monkeypatch):
    import connect_offers
    u = _user(db, onboarding_step=3, onboarding_completed_at=_now() - timedelta(hours=1))
    monkeypatch.setattr(connect_offers, "_action_for", lambda session, user, now, **kw: ("reconnect", "gcal", "ur calendar link died, reconnect?"))
    monkeypatch.setattr(connect_offers, "_commit_action", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(connect_offers, "_send_action", lambda uid, kind, provider, text, phone: sent.append((uid, kind)))
    assert connect_offers.sweep() == 1 and sent == [(u.id, "reconnect")]
