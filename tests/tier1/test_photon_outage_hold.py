"""
One number — hold on the line during a Photon outage (2026-09-28).

Live incident, 04:44 UTC: Photon's upstream answered "Service temporarily
unavailable" for ~14 min. The old failover carried the coach's reply to Twilio and
tripped the breaker, so three green texts landed in the user's OLD thread from a
different number. Founder's call: a user who is on their line stays on their line.

  - opted-in user + transient sidecar failure → brief inline retries on the same
    line, then HOLD (held_outbound + 'held' Message rows). No SMS, no breaker.
  - while an outage is flagged, or a backlog is parked, new sends queue behind it
    (order preserved, one probe per drain tick).
  - held_outbound.drain re-sends in order once Photon answers, with a light
    heads-up first when it waited a while; stale proactive rows expire.
  - a user who has NOT opted in (never texted their line) keeps the old path:
    Twilio IS the number they're on. The consent gate does too.
"""

from __future__ import annotations

import datetime as dt

import pytest
import requests

import config
import held_outbound
import sms
from models import HeldOutbound, Message, User
from tests.factories import make_user

SIDECAR = "http://sidecar.railway.internal:8080"
SECRET = "testsecret"
UPSTREAM_DOWN = ('sidecar /send 502: {"ok":false,"error":"ConnectionError: [upstream] '
                 'Service temporarily unavailable. Please retry."}')
OPTED_IN_AT = dt.datetime(2026, 9, 20, 0, 0, 0)


@pytest.fixture
def imessage_on(monkeypatch):
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    monkeypatch.setattr(config, "IMESSAGE_HOLD_RETRY_BACKOFF_S", [0, 0])


class FakeSidecar:
    """Controllable stand-in for sms._send_imessage. `down` → the live 502 body."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.down = False
        self.fail_first = 0        # fail this many calls, then recover
        self.error = UPSTREAM_DOWN

    def __call__(self, phone, body, reply_to=None):
        self.calls.append((phone, body, reply_to))
        if self.down or self.fail_first > 0:
            if self.fail_first > 0:
                self.fail_first -= 1
            raise RuntimeError(self.error)
        return f"spc-{len(self.calls)}"

    @property
    def bodies(self):
        return [c[1] for c in self.calls]


@pytest.fixture
def sidecar(monkeypatch):
    fake = FakeSidecar()
    monkeypatch.setattr(sms, "_send_imessage", fake)
    sms.clear_outage()
    yield fake
    sms.clear_outage()


def _opted_in(db, **kw):
    return make_user(db, preferred_channel="imessage", imessage_opted_in_at=OPTED_IN_AT, **kw)


def _rows(db, user):
    db.expire_all()
    return (db.query(Message).filter(Message.user_id == user.id, Message.direction == "out")
            .order_by(Message.id).all())


def _held(db, user=None):
    db.expire_all()
    q = db.query(HeldOutbound)
    if user is not None:
        q = q.filter(HeldOutbound.user_id == user.id)
    return q.order_by(HeldOutbound.id).all()


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


# ═══════════════════════════════════════════════════════════════════════════
# The incident: hold, don't hop
# ═══════════════════════════════════════════════════════════════════════════

def test_opted_in_user_transient_failure_holds_on_the_line(db, imessage_on, sidecar, sms_capture):
    sidecar.down = True
    user = _opted_in(db)

    sid = sms.send_sms(user.phone, "all good, no pressure --- wanna grab it tmrw morning before u eat?",
                       user_id=user.id, message_type="freeform")

    assert sid is None
    assert sms_capture == [], "one number: never a green text from Twilio for an opted-in user"
    # First attempt + the two inline retries, all on the same line.
    assert len(sidecar.calls) == 3
    # The message is parked with one 'held' row per bubble — the window sees it,
    # the engagement gates ignore it.
    assert [(r.channel, r.delivery_status) for r in _rows(db, user)] == [("imessage", "held"), ("imessage", "held")]
    held = _held(db, user)
    assert len(held) == 1 and held[0].status == "held" and held[0].message_type == "freeform"
    assert held[0].message_ids == [r.id for r in _rows(db, user)]
    # Breaker untouched; an outage is flagged for the cooldown.
    u = db.get(User, user.id)
    assert u.channel_failed_over is False and u.channel_failover_at is None
    assert sms.outage_active() is True


def test_inline_retry_recovers_without_holding(db, imessage_on, sidecar, sms_capture):
    sidecar.fail_first = 1
    user = _opted_in(db)

    sid = sms.send_sms(user.phone, "yeah send it, screenshot works", user_id=user.id)

    assert sid == "spc-2" and sms_capture == []
    assert [(r.channel, r.delivery_status, r.provider_sid) for r in _rows(db, user)] == [("imessage", "sent", "spc-2")]
    assert _held(db) == []
    assert sms.outage_active() is False


def test_outage_flag_queues_directly_without_probing(db, imessage_on, sidecar, sms_capture):
    """During the cooldown every reply would burn 3 attempts; instead they queue
    behind and the drain job is the single probe."""
    sms.note_outage()
    user = _opted_in(db)

    sms.send_sms(user.phone, "send the screenshot and i'll take the numbers off it", user_id=user.id)

    assert sidecar.calls == [] and sms_capture == []
    assert [h.status for h in _held(db, user)] == ["held"]


def test_hold_clears_the_typing_bubble(db, imessage_on, sidecar, monkeypatch):
    import typing_indicator
    stops = []
    monkeypatch.setattr(typing_indicator, "typing_stop", lambda uid: stops.append(uid) or True)
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "hey", user_id=user.id)
    assert stops == [user.id]


def test_hard_non_transient_error_for_opted_in_user_also_holds(db, imessage_on, sidecar, sms_capture, caplog):
    """One number means one number: even an error we don't recognize as an outage
    parks the message on the line (it expires if it never clears) — it is logged
    at ERROR so we look, but it never becomes a green text."""
    sidecar.down = True
    sidecar.error = "sidecar /send 500: something odd"
    user = _opted_in(db)
    with caplog.at_level("ERROR"):
        sms.send_sms(user.phone, "hmm", user_id=user.id)
    assert sms_capture == []
    assert [h.status for h in _held(db, user)] == ["held"]
    assert any("IMESSAGE_HELD" in r.message and "transient=False" in r.message for r in caplog.records)


# ═══════════════════════════════════════════════════════════════════════════
# Who keeps the old path
# ═══════════════════════════════════════════════════════════════════════════

def test_not_opted_in_user_keeps_twilio_failover(db, imessage_on, sidecar, sms_capture):
    """They have never texted their line → Twilio IS the number they're on."""
    sidecar.down = True
    user = make_user(db, preferred_channel="imessage")  # no imessage_opted_in_at

    sms.send_sms(user.phone, "morning check-in", user_id=user.id, message_type="heartbeat")

    assert sms_capture == [(user.phone, "morning check-in")]
    assert len(sidecar.calls) == 1, "no inline retries on the old path"
    assert [(r.channel, r.delivery_status) for r in _rows(db, user)] == [("imessage", "failed"), ("sms", "sent")]
    assert db.get(User, user.id).channel_failed_over is True
    assert _held(db) == []


def test_consent_gate_for_opted_in_user_still_goes_to_sms(db, imessage_on, sidecar, sms_capture):
    """Photon says the line can't reach them (seat gone) — that is not an outage to
    wait out; the invite path is the honest one."""
    sidecar.down = True
    sidecar.error = "sidecar /send 502: Target not allowed for this project"
    user = _opted_in(db)

    sms.send_sms(user.phone, "you around?", user_id=user.id)

    assert sms_capture == [(user.phone, "you around?")]
    db.expire_all()
    assert db.get(User, user.id).channel_failed_over is True
    assert _held(db) == []


def test_flag_off_restores_the_old_failover(db, imessage_on, sidecar, sms_capture, monkeypatch):
    monkeypatch.setattr(config, "IMESSAGE_HOLD_ON_OUTAGE", False)
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "hey", user_id=user.id)
    assert sms_capture == [(user.phone, "hey")]
    assert _held(db) == []


def test_read_timeout_is_still_maybe_delivered_not_held(db, imessage_on, monkeypatch, sms_capture):
    def _slow(phone, body, reply_to=None):
        raise requests.exceptions.ReadTimeout("sidecar took too long")
    monkeypatch.setattr(sms, "_send_imessage", _slow)
    user = _opted_in(db)
    sms.send_sms(user.phone, "quick pull sesh?", user_id=user.id, message_type="heartbeat")
    assert sms_capture == [] and _held(db) == []
    assert [(r.channel, r.delivery_status) for r in _rows(db, user)] == [("imessage", "sent")]


def test_stale_reply_target_resends_unthreaded_not_held(db, imessage_on, monkeypatch, sms_capture):
    calls = []

    def _fake(phone, body, reply_to=None):
        calls.append(reply_to)
        if reply_to:
            raise RuntimeError("sidecar /send 502: reply_to message not found: spc-msg-old")
        return "spc-new"
    monkeypatch.setattr(sms, "_send_imessage", _fake)
    user = _opted_in(db)

    sid = sms.send_sms(user.phone, "yeah np, whenever", user_id=user.id, reply_to_sid="spc-msg-old")

    assert sid == "spc-new" and calls == ["spc-msg-old", None]
    assert _held(db) == [] and sms_capture == []


# ═══════════════════════════════════════════════════════════════════════════
# The drain: deliver in order once Photon answers, heads-up first if it waited
# ═══════════════════════════════════════════════════════════════════════════

def test_drain_delivers_in_order_with_a_heads_up_after_a_real_wait(db, imessage_on, sidecar, sms_capture):
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "all good, no pressure --- wanna grab it tmrw?", user_id=user.id)
    sms.send_sms(user.phone, "send the screenshot", user_id=user.id)  # queues behind (outage flagged)
    sidecar.calls.clear()

    sidecar.down = False
    out = held_outbound.drain(now=_now() + dt.timedelta(seconds=config.IMESSAGE_HOLD_NOTE_MIN_AGE_S + 5))

    assert out == {"sent": 2, "expired": 0, "stuck": 0}
    assert sidecar.bodies == [config.IMESSAGE_HOLD_NOTE, "all good, no pressure", "wanna grab it tmrw?",
                              "send the screenshot"]
    assert sms_capture == []
    # Held rows flipped to sent with their Photon ids; the heads-up has its own row.
    rows = _rows(db, user)
    assert [(r.message_type, r.delivery_status, r.provider_sid) for r in rows] == [
        ("freeform", "sent", "spc-2"), ("freeform", "sent", "spc-3"), ("freeform", "sent", "spc-4"),
        ("outage_note", "sent", "spc-1"),
    ]
    # Delivery order is the window order: the held rows were re-stamped to now.
    assert rows[0].created_at >= rows[3].created_at - dt.timedelta(seconds=5)
    assert [h.status for h in _held(db, user)] == ["sent", "sent"]
    assert all(h.sent_at is not None for h in _held(db, user))
    assert sms.outage_active() is False


def test_drain_skips_the_heads_up_for_a_short_blip(db, imessage_on, sidecar):
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "got all three in", user_id=user.id)
    sidecar.calls.clear()

    sidecar.down = False
    held_outbound.drain(now=_now() + dt.timedelta(seconds=10))

    assert sidecar.bodies == ["got all three in"]


def test_drain_while_still_down_rearms_with_backoff(db, imessage_on, sidecar):
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "hey", user_id=user.id)
    sidecar.calls.clear()
    sms.clear_outage()

    now = _now()
    out = held_outbound.drain(now=now)

    assert out == {"sent": 0, "expired": 0, "stuck": 1}
    assert len(sidecar.calls) == 1, "the drain is ONE probe, not a retry storm"
    h = _held(db, user)[0]
    assert h.status == "held" and h.attempts == 1
    assert h.next_attempt_at >= now + dt.timedelta(seconds=held_outbound.RETRY_BACKOFF_S[0] - 1)
    assert "temporarily unavailable" in (h.last_error or "")
    assert sms.outage_active() is True
    # Not due yet → the next tick doesn't even probe.
    held_outbound.drain(now=now + dt.timedelta(seconds=5))
    assert len(sidecar.calls) == 1


def test_stale_proactive_hold_expires_instead_of_landing_late(db, imessage_on, sidecar, caplog):
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "water break, go drink", user_id=user.id, message_type="reminder")
    sidecar.calls.clear()

    sidecar.down = False
    with caplog.at_level("ERROR"):
        out = held_outbound.drain(now=_now() + dt.timedelta(minutes=config.IMESSAGE_HOLD_PROACTIVE_MAX_AGE_MIN + 1))

    assert out["expired"] == 1 and sidecar.calls == []
    assert [h.status for h in _held(db, user)] == ["expired"]
    assert [r.delivery_status for r in _rows(db, user)] == ["failed"]
    assert any("IMESSAGE_HELD_EXPIRED" in r.message for r in caplog.records)


def test_a_reply_waits_longer_than_a_reminder(db, imessage_on, sidecar):
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "here's the recap", user_id=user.id, message_type="freeform")
    sidecar.calls.clear()

    sidecar.down = False
    held_outbound.drain(now=_now() + dt.timedelta(minutes=config.IMESSAGE_HOLD_PROACTIVE_MAX_AGE_MIN + 1))

    assert [h.status for h in _held(db, user)] == ["sent"]


def test_one_stuck_user_does_not_block_another(db, imessage_on, sidecar, monkeypatch):
    sidecar.down = True
    a = _opted_in(db)
    b = _opted_in(db)
    sms.send_sms(a.phone, "for a", user_id=a.id)
    sms.send_sms(b.phone, "for b", user_id=b.id)
    sidecar.calls.clear()
    sms.clear_outage()

    # Photon is back for everyone but a's DM is rejected outright.
    def _selective(phone, body, reply_to=None):
        sidecar.calls.append((phone, body, reply_to))
        if phone == a.phone:
            raise RuntimeError("sidecar /send 502: boom")
        return "spc-b"
    monkeypatch.setattr(sms, "_send_imessage", _selective)

    out = held_outbound.drain(now=_now())

    assert out == {"sent": 1, "expired": 0, "stuck": 1}
    assert [h.status for h in _held(db, b)] == ["sent"]
    assert [h.status for h in _held(db, a)] == ["held"]


# ═══════════════════════════════════════════════════════════════════════════
# Order: a live send drains the backlog first, or queues behind it
# ═══════════════════════════════════════════════════════════════════════════

def test_live_send_drains_the_backlog_first(db, imessage_on, sidecar, sms_capture):
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "first thing", user_id=user.id)
    sidecar.calls.clear()
    sms.clear_outage()  # cooldown over; Photon is back

    sidecar.down = False
    sid = sms.send_sms(user.phone, "second thing", user_id=user.id)

    assert sid == "spc-2"
    assert sidecar.bodies == ["first thing", "second thing"], "held bubble lands BEFORE the live one"
    assert [h.status for h in _held(db, user)] == ["sent"]
    assert [(r.body, r.delivery_status) for r in _rows(db, user)] == [("first thing", "sent"), ("second thing", "sent")]


def test_live_send_queues_behind_a_backlog_that_is_still_stuck(db, imessage_on, sidecar, sms_capture):
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "first thing", user_id=user.id)
    sidecar.calls.clear()
    sms.clear_outage()  # cooldown over, but Photon is NOT back

    sid = sms.send_sms(user.phone, "second thing", user_id=user.id)

    assert sid is None and sms_capture == []
    assert sidecar.bodies == ["first thing"], "one probe (the backlog), and the live one never jumps the line"
    assert [(h.body, h.status) for h in _held(db, user)] == [("first thing", "held"), ("second thing", "held")]

    # When Photon answers, they land in the order the coach said them.
    sidecar.down = False
    sidecar.calls.clear()
    held_outbound.drain(now=_now() + dt.timedelta(seconds=held_outbound.RETRY_BACKOFF_S[0] + 1))
    assert sidecar.bodies == ["first thing", "second thing"]


# ═══════════════════════════════════════════════════════════════════════════
# The keystone: a held row is not the user's silence
# ═══════════════════════════════════════════════════════════════════════════

def test_held_row_is_not_an_unanswered_strike(db, imessage_on, sidecar):
    from engagement_tracker import increment_unanswered
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "you eat yet?", user_id=user.id, message_type="heartbeat")

    increment_unanswered(user.id)

    db.expire_all()
    assert (db.get(User, user.id).unanswered_count or 0) == 0


def test_held_row_is_invisible_to_the_silence_gates(db, imessage_on, sidecar):
    from sqlalchemy import and_
    from engagement_tracker import _landed
    sidecar.down = True
    user = _opted_in(db)
    sms.send_sms(user.phone, "you eat yet?", user_id=user.id, message_type="heartbeat")

    landed = db.query(Message).filter(and_(Message.user_id == user.id, Message.direction == "out", _landed())).count()
    assert landed == 0
