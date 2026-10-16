"""Live 2026-10-16 08:48 PT (user 61, calendar connect still pending, memory: 8am classes most
days): the briefing said "no classes down for today so good shot at an evening lift". An empty
events list was read as knowledge. Now the MORNING OPEN block names the unconnected calendar
and a send-time guard drops a free-day claim when no calendar is connected."""
from __future__ import annotations

import logging

import pytest

from tests.factories import make_user
from tests.tier1.test_briefing_completeness import (brief_on, _patch_weather, _pin_clock,  # noqa: F401
                                                     _tick_speaking, _utc)

EMMA = "morning, 66° and clear out. no classes down for today so good shot at an evening lift"


def _connect_gcal(db, user):
    from models import Integration
    db.add(Integration(user_id=user.id, provider="gcal", status="connected"))
    db.commit()


# ─── the guard itself ─────────────────────────────────────────────────────────

def test_guard_drops_the_no_classes_sentence_and_keeps_the_weather(db, caplog):
    import heartbeat
    user = make_user(db)
    with caplog.at_level(logging.INFO):
        out = heartbeat._guard_schedule_claims(EMMA, user.id)
    assert out == "morning, 66° and clear out."
    assert "HEARTBEAT_SCHEDULE_CLAIM_DROPPED" in caplog.text


@pytest.mark.parametrize("line", [
    "free day today, good time for a long lift",
    "nothing on ur calendar so hit the gym whenever",
    "ur schedule is wide open — go when u want",
    "no lectures today, rest day vibes",
])
def test_guard_catches_the_other_phrasings(db, line):
    import heartbeat
    user = make_user(db)
    assert "today" not in heartbeat._guard_schedule_claims(f"73° and sunny. {line}", user.id).split(".")[1:] or True
    assert heartbeat._guard_schedule_claims(f"73° and sunny. {line}", user.id) == "73° and sunny."


def test_guard_keeps_bubbles_and_drops_an_emptied_one(db):
    import heartbeat
    user = make_user(db)
    msg = "73° and sunny\n---\nno classes today\n---\neat before the gym"
    assert heartbeat._guard_schedule_claims(msg, user.id) == "73° and sunny\n---\neat before the gym"


def test_guard_is_a_noop_when_a_calendar_is_connected(db):
    import heartbeat
    user = make_user(db)
    _connect_gcal(db, user)
    assert heartbeat._guard_schedule_claims(EMMA, user.id) == EMMA


def test_guard_fails_open_when_nothing_would_survive(db, caplog):
    import heartbeat
    user = make_user(db)
    with caplog.at_level(logging.WARNING):
        assert heartbeat._guard_schedule_claims("no classes today", user.id) == "no classes today"
    assert "HEARTBEAT_SCHEDULE_CLAIM_KEPT" in caplog.text


def test_guard_respects_the_flag(db, monkeypatch):
    import config, heartbeat
    monkeypatch.setattr(config, "HEARTBEAT_NO_CALENDAR_GUARD_ENABLED", False)
    user = make_user(db)
    assert heartbeat._guard_schedule_claims(EMMA, user.id) == EMMA


# ─── the context ──────────────────────────────────────────────────────────────

def test_morning_open_names_the_unconnected_calendar(db, brief_on, monkeypatch):
    import heartbeat
    from models import get_session, User
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    s = get_session()
    try:
        u = s.get(User, user.id)
        text = heartbeat._morning_open_signal(u, s, now=_utc(12, 0))
    finally:
        s.close()
    assert text and "no calendar is connected" in text and "Never say 'no classes'" in text
    assert "none logged" not in text


def test_morning_open_says_none_on_calendar_when_connected_and_empty(db, brief_on, monkeypatch):
    import heartbeat
    from models import get_session, User
    _patch_weather(monkeypatch)
    user = make_user(db, wake_time="11:30", sleep_time="02:00")
    _connect_gcal(db, user)
    s = get_session()
    try:
        u = s.get(User, user.id)
        text = heartbeat._morning_open_signal(u, s, now=_utc(12, 0))
    finally:
        s.close()
    assert text and "none on their connected calendar" in text


# ─── end to end: the live line through a tick ─────────────────────────────────

def test_tick_sends_the_guarded_line(db, brief_on, monkeypatch, sms_capture, anthropic_stub):
    import heartbeat
    _patch_weather(monkeypatch, temp=66, code=0)
    user = make_user(db, wake_time="07:30", sleep_time="00:30")
    _pin_clock(monkeypatch, _utc(8, 48))
    _tick_speaking(monkeypatch, anthropic_stub, EMMA)
    heartbeat.heartbeat_tick(user.id)
    assert sms_capture, "the tick spoke"
    sent = sms_capture[0][1]
    assert "no classes" not in sent and "66" in sent
