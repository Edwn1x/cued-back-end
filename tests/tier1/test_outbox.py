"""
Outbox (outbox.py): one gate at sms.send_sms for everything cued sends on its own clock.
Live thread → hold; merge window → hold; daily budget → drop (broadcast only); a held
broadcast whose person spoke is dropped at drain; SMS has no hold queue (broadcast → drop,
asked → send now); the silence downgrade is flag-gated off.
"""

from __future__ import annotations

import datetime as dt

import pytest

import config
import held_outbound
import outbox
import sms
from models import HeldOutbound, Message
from tests.factories import make_user

SIDECAR = "http://sidecar.railway.internal:8080"
OPTED_IN_AT = dt.datetime(2026, 9, 20, 0, 0, 0)


def _now():
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(config, "OUTBOX_ENABLED", True)
    monkeypatch.setattr(config, "OUTBOX_LIVE_THREAD_MIN", 3)
    monkeypatch.setattr(config, "OUTBOX_MERGE_WINDOW_S", 60)
    monkeypatch.setattr(config, "OUTBOX_DAILY_MAX", 6)
    monkeypatch.setattr(config, "OUTBOX_SILENCE_DOWNGRADE_ENABLED", False)
    monkeypatch.setattr(config, "IMESSAGE_CHANNEL_ENABLED", True)
    monkeypatch.setattr(config, "SIDECAR_URL", SIDECAR)
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", "s3cret")
    monkeypatch.setattr(config, "IMESSAGE_HOLD_ON_OUTAGE", True)
    monkeypatch.setattr(config, "IMESSAGE_HOLD_RETRY_BACKOFF_S", [0, 0])


@pytest.fixture
def sidecar(monkeypatch):
    calls: list = []

    def _fake(phone, body, reply_to=None):
        calls.append(body)
        return f"spc-{len(calls)}"
    monkeypatch.setattr(sms, "_send_imessage", _fake)
    sms.clear_outage()
    yield calls
    sms.clear_outage()


def _imsg(db, **kw):
    return make_user(db, preferred_channel="imessage", imessage_opted_in_at=OPTED_IN_AT, **kw)


def _inbound(db, user, ago_s=0, body="hey"):
    db.add(Message(user_id=user.id, direction="in", body=body, message_type="freeform", channel="imessage",
                   created_at=_now() - dt.timedelta(seconds=ago_s)))
    db.commit()


def _out(db, user, mtype, ago_s=0, status="sent"):
    db.add(Message(user_id=user.id, direction="out", body="x", message_type=mtype, channel="imessage",
                   delivery_status=status, created_at=_now() - dt.timedelta(seconds=ago_s)))
    db.commit()


def _held(db, user):
    db.expire_all()
    return db.query(HeldOutbound).filter(HeldOutbound.user_id == user.id).order_by(HeldOutbound.id).all()


def _rows(db, user, mtype=None):
    db.expire_all()
    q = db.query(Message).filter(Message.user_id == user.id, Message.direction == "out")
    if mtype:
        q = q.filter(Message.message_type == mtype)
    return q.order_by(Message.id).all()


# ─── the verdicts ──────────────────────────────────────────────────────────────────

def test_replies_are_never_gated_and_flag_off_sends(db, on, monkeypatch):
    u = _imsg(db)
    _inbound(db, u, ago_s=5)
    assert outbox.decide(u.id, "freeform").action == "send"
    assert outbox.decide(u.id, "card_setup").action == "send"
    monkeypatch.setattr(config, "OUTBOX_ENABLED", False)
    assert outbox.decide(u.id, "heartbeat").action == "send"


def test_live_thread_holds_both_classes(db, on):
    u = _imsg(db)
    _inbound(db, u, ago_s=30)
    v = outbox.decide(u.id, "heartbeat")
    assert v.action == "hold" and v.reason == "live_thread" and 100 <= v.delay_s <= 170
    assert outbox.decide(u.id, "reminder").action == "hold"        # the wake-up text mid-conversation (u51, 10046)
    assert outbox.decide(u.id, "water_reminder").action == "hold"


def test_in_flight_turn_holds(db, on, monkeypatch):
    import message_buffer
    u = _imsg(db)
    monkeypatch.setattr(message_buffer, "is_in_flight", lambda phone: phone == u.phone)
    v = outbox.decide(u.id, "gym_dead", phone=u.phone)
    assert v.action == "hold" and v.reason == "live_thread"


def test_merge_window_holds_the_second_sender(db, on):
    u = _imsg(db)
    _out(db, u, "water_reminder", ago_s=20)
    v = outbox.decide(u.id, "heartbeat")
    assert v.action == "hold" and v.reason == "merge_window" and 40 <= v.delay_s <= 50
    _out(db, u, "heartbeat", ago_s=120)
    u2 = _imsg(db)
    _out(db, u2, "heartbeat", ago_s=120)
    assert outbox.decide(u2.id, "water_reminder").action == "send"   # outside the window


def test_daily_budget_drops_broadcast_not_asked(db, on):
    u = _imsg(db)
    for _ in range(6):
        _out(db, u, "heartbeat", ago_s=3600)
    assert outbox.decide(u.id, "gym_dead") == outbox.Verdict("drop", "daily_budget")
    assert outbox.decide(u.id, "connect_offer").action == "drop"
    assert outbox.decide(u.id, "reminder").action == "send"
    assert outbox.decide(u.id, "water_reminder").action == "send"


def test_silence_downgrade_is_off_by_default_and_works_when_on(db, on, monkeypatch):
    u = _imsg(db)
    _inbound(db, u, ago_s=10 * 86400)
    _out(db, u, "reminder_checkin", ago_s=3 * 86400)
    assert outbox.decide(u.id, "heartbeat").action == "send"
    monkeypatch.setattr(config, "OUTBOX_SILENCE_DOWNGRADE_ENABLED", True)
    monkeypatch.setattr(config, "REMINDER_MAX_QUIET_DAYS", 5)
    assert outbox.decide(u.id, "heartbeat") == outbox.Verdict("drop", "silence_downgrade")
    assert outbox.decide(u.id, "water_reminder").action == "send"      # stickers keep going
    # they answer the check-in → the stream is back
    _inbound(db, u, ago_s=60, body="👍")
    assert outbox.decide(u.id, "heartbeat").reason != "silence_downgrade"


# ─── the door ──────────────────────────────────────────────────────────────────────

def test_send_sms_holds_a_heartbeat_inside_a_live_thread_and_the_reply_goes(db, on, sidecar):
    u = _imsg(db)
    _inbound(db, u, ago_s=20)
    assert sms.send_sms(u.phone, "u eat today?", user_id=u.id, message_type="heartbeat") is None
    held = _held(db, u)
    assert len(held) == 1 and held[0].status == "held" and held_outbound.is_outbox_row(held[0])
    assert held[0].next_attempt_at > _now() + dt.timedelta(seconds=60)
    assert _rows(db, u, "heartbeat")[0].delivery_status == "held"
    assert sidecar == []
    # the coach's reply is not queued behind the parked nudge
    assert sms.send_sms(u.phone, "logged it", user_id=u.id, message_type="freeform") is not None
    assert sidecar == ["logged it"]


def test_held_broadcast_is_dropped_at_drain_if_they_spoke_else_delivered(db, on, sidecar):
    u = _imsg(db)
    _inbound(db, u, ago_s=20)
    sms.send_sms(u.phone, "how's legs going", user_id=u.id, message_type="heartbeat")
    sms.send_sms(u.phone, "drink water", user_id=u.id, message_type="reminder")
    assert len(_held(db, u)) == 2 and sidecar == []
    # not due yet → the drain leaves them alone
    assert held_outbound.drain() == {"sent": 0, "expired": 0, "stuck": 0}
    # they spoke meanwhile; the window closes
    _inbound(db, u, ago_s=0, body="good")
    later = _now() + dt.timedelta(minutes=5)
    out = held_outbound.drain(now=later)
    assert out["expired"] == 1 and out["sent"] == 1
    assert sidecar == ["drink water"]                                 # asked-for lands, broadcast dropped
    hb = _rows(db, u, "heartbeat")[0]
    assert hb.delivery_status == "failed"


def test_held_broadcast_is_delivered_when_they_stayed_quiet(db, on, sidecar):
    u = _imsg(db)
    _inbound(db, u, ago_s=20)
    sms.send_sms(u.phone, "how's legs going", user_id=u.id, message_type="heartbeat")
    later = _now() + dt.timedelta(minutes=5)
    out = held_outbound.drain(now=later)
    assert out["sent"] == 1 and sidecar == ["how's legs going"]
    assert _rows(db, u, "heartbeat")[0].delivery_status == "sent"


def test_merge_window_paces_a_burst(db, on, sidecar):
    u = _imsg(db)
    _inbound(db, u, ago_s=600)
    assert sms.send_sms(u.phone, "[water-break gif]", user_id=u.id, message_type="water_reminder") is not None
    assert sms.send_sms(u.phone, "gym's quiet, quick push?", user_id=u.id, message_type="gym_dead") is None
    assert sms.send_sms(u.phone, "u eat yet", user_id=u.id, message_type="heartbeat") is None
    assert sidecar == ["[water-break gif]"] and len(_held(db, u)) == 2
    out = held_outbound.drain(now=_now() + dt.timedelta(minutes=2))
    assert out["sent"] == 2 and sidecar == ["[water-break gif]", "gym's quiet, quick push?", "u eat yet"]


def test_budget_drops_at_the_door(db, on, sidecar):
    u = _imsg(db)
    for _ in range(6):
        _out(db, u, "heartbeat", ago_s=3600)
    assert sms.send_sms(u.phone, "quick push?", user_id=u.id, message_type="gym_dead") is None
    assert sidecar == [] and _held(db, u) == []
    assert sms.send_sms(u.phone, "wake up, midterm at 12", user_id=u.id, message_type="reminder") is not None


def test_sms_user_has_no_hold_queue(db, on, sms_capture):
    u = make_user(db, preferred_channel="sms")
    _inbound(db, u, ago_s=20)
    assert sms.send_sms(u.phone, "u eat today?", user_id=u.id, message_type="heartbeat") is None   # broadcast → drop
    assert sms.send_sms(u.phone, "drink water", user_id=u.id, message_type="reminder") is not None  # asked → now
    assert [b for _, b in sms_capture] == ["drink water"]
    assert _held(db, u) == []


def test_program_gym_time_stands_down_under_the_silence_downgrade(db, on, monkeypatch, sidecar):
    from reminders import create_reminder, fire_due
    from models import Reminder
    monkeypatch.setattr(config, "OUTBOX_SILENCE_DOWNGRADE_ENABLED", True)
    monkeypatch.setattr(config, "REMINDER_MAX_QUIET_DAYS", 5)
    monkeypatch.setattr(config, "REMINDERS_ENABLED", True, raising=False)
    u = _imsg(db)
    _inbound(db, u, ago_s=10 * 86400)
    _out(db, u, "reminder_checkin", ago_s=3 * 86400)
    r = create_reminder(u.id, "gym", "18:00", days="mon,tue,wed,thu,fri,sat,sun", source="program")
    db.expire_all()
    row = db.get(Reminder, r["id"])
    before = row.fire_at
    assert fire_due(now=before) == 0
    db.expire_all()
    row = db.get(Reminder, r["id"])
    assert row.active and row.fire_at > before                        # re-armed, not fired, not cancelled
    assert sidecar == []
