"""
Photon migration — fifth commit: the keystone's two siblings.

1. The two silence gates (`has_unanswered_outbound`, `has_unanswered_proactive`)
   decide whether the coach stays quiet. Counting a `failed` outbound as
   "unanswered" means a sidecar outage silences the coach for every iMessage
   user — the keystone failure mode wearing a different hat. Fix: failed rows
   are invisible to both gates.
2. Breaker reset on iMessage inbound. An iMessage arriving FROM the user is proof
   the pipe works for that user; clear channel_failed_over there. No timers.
   A Twilio inbound proves nothing about iMessage and must not clear it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

from tests.factories import make_user

SECRET = "testsecret"


def _out(db, user, *, minutes_ago, status="sent", message_type="evening", body="you good?"):
    from models import Message
    # naive-UTC column (heartbeat convention) — an aware value would be shifted to
    # the session timezone on write and skew the gate's age math.
    m = Message(user_id=user.id, direction="out", body=body, message_type=message_type,
                created_at=(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).replace(tzinfo=None),
                delivery_status=status, channel="imessage" if status == "failed" else "sms")
    db.add(m); db.commit()
    return m


# ─── gate 1: has_unanswered_outbound (legacy scheduler's stay-mute gate) ─────

def test_failed_outbound_today_does_not_block(db):
    from engagement_tracker import has_unanswered_outbound
    user = make_user(db)
    _out(db, user, minutes_ago=10, status="failed")
    assert has_unanswered_outbound(user.id) is False


def test_sent_outbound_today_still_blocks(db):
    from engagement_tracker import has_unanswered_outbound
    user = make_user(db)
    _out(db, user, minutes_ago=10, status="sent")
    assert has_unanswered_outbound(user.id) is True


def test_older_sent_unanswered_still_blocks_through_a_later_failure(db):
    """The failed row is invisible, not a clearer: the delivered-but-unanswered
    message underneath it still gates."""
    from engagement_tracker import has_unanswered_outbound
    user = make_user(db)
    _out(db, user, minutes_ago=60, status="sent")
    _out(db, user, minutes_ago=10, status="failed")
    assert has_unanswered_outbound(user.id) is True


# ─── gate 2: has_unanswered_proactive (heartbeat anti-stack) ─────────────────

def test_failed_heartbeat_does_not_stack_block(db):
    from engagement_tracker import has_unanswered_proactive
    user = make_user(db)
    _out(db, user, minutes_ago=10, status="failed", message_type="heartbeat")
    assert has_unanswered_proactive(user.id, window_minutes=180) is False


def test_sent_heartbeat_within_window_still_stack_blocks(db):
    from engagement_tracker import has_unanswered_proactive
    user = make_user(db)
    _out(db, user, minutes_ago=10, status="sent", message_type="heartbeat")
    assert has_unanswered_proactive(user.id, window_minutes=180) is True


def test_failed_reactive_reply_after_sent_heartbeat_keeps_the_heartbeat_gate(db):
    """Most-recent LANDED outbound is what the gate reads. A later failed reactive
    row must not hide a fresh unanswered heartbeat (and vice-versa)."""
    from engagement_tracker import has_unanswered_proactive
    user = make_user(db)
    _out(db, user, minutes_ago=20, status="sent", message_type="heartbeat")
    _out(db, user, minutes_ago=5, status="failed", message_type="freeform")
    assert has_unanswered_proactive(user.id, window_minutes=180) is True


# ─── breaker reset on iMessage inbound ───────────────────────────────────────

def _post_inbound(client, phone, text, msg_id="photon-in-9"):
    payload = {"phone": phone, "text": text, "provider_message_id": msg_id, "chat_guid": f"any;-;{phone}",
               "service": "iMessage", "line_phone": "+15102646604",
               "timestamp": "2026-09-10T12:00:00.000Z", "attachments": []}
    return client.post("/internal/inbound", data=json.dumps(payload),
                       headers={"X-Internal-Secret": SECRET}, content_type="application/json")


def test_imessage_inbound_clears_the_breaker(db, client, monkeypatch, caplog):
    import config
    from models import User
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    user = make_user(db, preferred_channel="imessage", channel_failed_over=True,
                     channel_failover_at=datetime.now(timezone.utc) - timedelta(hours=2))

    with caplog.at_level(logging.INFO):
        r = _post_inbound(client, user.phone, "hey coach, back")
    assert r.status_code == 200

    db.expire_all()
    u = db.get(User, user.id)
    assert u.channel_failed_over is False
    assert u.channel_failover_at is None
    assert u.preferred_channel == "imessage"  # the ask is untouched; only the breaker moves
    assert any("BREAKER_RESET" in rec.getMessage() for rec in caplog.records)


def test_twilio_inbound_does_not_clear_the_breaker(db, driver):
    from models import User
    user = make_user(db, preferred_channel="imessage", channel_failed_over=True)
    driver.send(user, "hey")
    db.expire_all()
    assert db.get(User, user.id).channel_failed_over is True


def test_imessage_inbound_with_breaker_closed_is_a_noop(db, client, monkeypatch, caplog):
    import config
    from models import User
    monkeypatch.setattr(config, "INTERNAL_SHARED_SECRET", SECRET)
    user = make_user(db, preferred_channel="imessage", channel_failed_over=False)
    with caplog.at_level(logging.INFO):
        _post_inbound(client, user.phone, "hey")
    db.expire_all()
    assert db.get(User, user.id).channel_failed_over is False
    assert not any("BREAKER_RESET" in rec.getMessage() for rec in caplog.records)
