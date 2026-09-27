"""
Send-reliability layer — two shipped fixes (2026-09-26).

1. BUFFER-RACE DEDUP ("same 4 messages"):
   (a) message_buffer tags every timer with a token; a superseded timer that
       fires anyway (cancel() lost the race to a late append) is a no-op, so the
       late append joins the CURRENT timer's flush as one turn instead of a
       second one.
   (b) sms._is_duplicate_send suppresses a reply that is byte-for-byte identical
       to the last one sent to the same user inside OUTBOUND_DEDUP_WINDOW_S — the
       backstop for the residual race where a second turn is already in flight.

2. SIDECAR-LATENCY DUP-SEND / FALSE-BREAKER TRIP:
   A read timeout on the sidecar /send is "maybe delivered" — Photon likely
   landed the iMessage. So a read timeout does NOT fall over to SMS (no
   double-send) and does NOT trip the breaker (a slow ack is not a dead pipe).
   A connect error / non-2xx is still a hard failure and takes the old failover.
"""

from __future__ import annotations

import requests
import pytest

import config
import message_buffer
import sms
from tests.factories import make_user

SIDECAR = "http://sidecar.railway.internal:8080"
SECRET = "testsecret"


@pytest.fixture
def imessage_on(monkeypatch):
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)


def _outbound_rows(db, user):
    from models import Message
    db.expire_all()
    return (db.query(Message).filter(Message.user_id == user.id, Message.direction == "out")
            .order_by(Message.id).all())


# ═══════════════════════════════════════════════════════════════════════════
# 1a. buffer race — a late append joins the same flush, not a second turn
# ═══════════════════════════════════════════════════════════════════════════

def test_superseded_timer_does_not_start_a_second_turn():
    """The timer-vs-append race: the first message's timer fires late (cancel lost
    the race), but a second message already reset the timer. The stale timer must
    be a no-op, so both messages flush together as ONE turn."""
    calls = []

    def cb(user_id, body, message_type, image_url, images=None):
        calls.append(body)

    message_buffer.buffer_message("+15550000001", "first", 1, "freeform",
                                  process_callback=cb, delay_override=(10, 10))
    stale = message_buffer._buffers["+15550000001"]["timer"]

    # Second message appends and resets the timer with a fresh token.
    message_buffer.buffer_message("+15550000001", "second", 1, "freeform",
                                  process_callback=cb, delay_override=(10, 10))
    current = message_buffer._buffers["+15550000001"]["timer"]
    assert stale is not current

    # RACE: the first (now superseded) timer fires anyway. Must NOT flush.
    stale.fire()
    assert calls == [], "a superseded timer must not start a turn"

    # The current timer flushes both messages as one combined turn.
    current.fire()
    assert calls == ["first\nsecond"], "late append joins the same flush"


def test_normal_single_flush_still_works():
    calls = []

    def cb(user_id, body, message_type, image_url, images=None):
        calls.append(body)

    message_buffer.buffer_message("+15550000002", "just one", 1, "freeform",
                                  process_callback=cb, delay_override=(10, 10))
    message_buffer._buffers["+15550000002"]["timer"].fire()
    assert calls == ["just one"]


# ═══════════════════════════════════════════════════════════════════════════
# 1b. outbound dedup — a near-identical consecutive reply is suppressed
# ═══════════════════════════════════════════════════════════════════════════

def test_duplicate_consecutive_outbound_suppressed(db, sms_capture):
    from sms import send_sms
    user = make_user(db)  # plain SMS user

    first = send_sms(user.phone, "nice work today — get some protein in", user_id=user.id)
    second = send_sms(user.phone, "Nice work today —  get some protein in", user_id=user.id)

    assert first is not None
    assert second is None, "the second, normalized-identical send is suppressed"
    assert sms_capture == [(user.phone, "nice work today - get some protein in")]

    # Only the delivered send wrote an outbound row.
    assert len(_outbound_rows(db, user)) == 1


def test_distinct_bodies_are_not_suppressed(db, sms_capture):
    from sms import send_sms
    user = make_user(db)

    send_sms(user.phone, "how'd the lift go?", user_id=user.id)
    send_sms(user.phone, "you eat yet?", user_id=user.id)

    assert len(sms_capture) == 2


def test_dedup_off_lets_duplicates_through(db, sms_capture, monkeypatch):
    monkeypatch.setattr(config, "OUTBOUND_DEDUP_ENABLED", False)
    from sms import send_sms
    user = make_user(db)

    send_sms(user.phone, "same thing", user_id=user.id)
    send_sms(user.phone, "same thing", user_id=user.id)

    assert len(sms_capture) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 2. sidecar read timeout — no double-send, no breaker trip
# ═══════════════════════════════════════════════════════════════════════════

def test_read_timeout_does_not_fail_over_or_trip_breaker(db, imessage_on, monkeypatch, sms_capture):
    """The real incident: sidecar latency > SIDECAR_TIMEOUT_S. The iMessage
    actually lands, so we must not resend on SMS and must not trip the breaker."""
    import sms as sms_mod
    from models import User

    def _slow(phone, body):
        raise requests.exceptions.ReadTimeout("sidecar took too long")
    monkeypatch.setattr(sms_mod, "_send_imessage", _slow)

    user = make_user(db, preferred_channel="imessage")
    sid = sms_mod.send_sms(user.phone, "quick pull sesh?", user_id=user.id, message_type="heartbeat")

    # No SMS double-send.
    assert sms_capture == [], "a read timeout must not fall over to SMS (would double-send)"
    assert sid is None

    # Breaker stays closed — a slow ack is not a dead pipe.
    db.expire_all()
    u = db.get(User, user.id)
    assert u.channel_failed_over is False
    assert u.channel_failover_at is None

    # The row is stamped landed (not 'failed'), so the keystone doesn't mis-count it.
    rows = _outbound_rows(db, user)
    assert [(r.channel, r.delivery_status) for r in rows] == [("imessage", "sent")]


def test_hard_failure_still_falls_over_and_trips_breaker(db, imessage_on, monkeypatch, sms_capture):
    """Regression guard: a non-timeout failure keeps the original semantics —
    failed row first, breaker trips, SMS carries the message."""
    import sms as sms_mod
    from models import User

    def _boom(phone, body):
        raise RuntimeError("sidecar /send 502: boom")
    monkeypatch.setattr(sms_mod, "_send_imessage", _boom)

    user = make_user(db, preferred_channel="imessage")
    sid = sms_mod.send_sms(user.phone, "morning check-in", user_id=user.id, message_type="heartbeat")

    assert sms_capture == [(user.phone, "morning check-in")]
    assert sid is not None and sid.startswith("SMfake")

    rows = _outbound_rows(db, user)
    assert [(r.channel, r.delivery_status) for r in rows] == [("imessage", "failed"), ("sms", "sent")]

    db.expire_all()
    u = db.get(User, user.id)
    assert u.channel_failed_over is True


def test_connect_timeout_is_treated_as_hard_failure(db, imessage_on, monkeypatch, sms_capture):
    """A ConnectTimeout means nothing was sent — it must still fail over (unlike a
    ReadTimeout, where the request went out and may have landed)."""
    import sms as sms_mod
    from models import User

    def _noconnect(phone, body):
        raise requests.exceptions.ConnectTimeout("could not reach sidecar")
    monkeypatch.setattr(sms_mod, "_send_imessage", _noconnect)

    user = make_user(db, preferred_channel="imessage")
    sms_mod.send_sms(user.phone, "you around?", user_id=user.id, message_type="heartbeat")

    assert sms_capture == [(user.phone, "you around?")], "connect failure → nothing landed → fail over"
    db.expire_all()
    u = db.get(User, user.id)
    assert u.channel_failed_over is True
