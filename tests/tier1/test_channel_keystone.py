"""
Photon migration — Phase 2: channel schema + the keystone.

Keystone (handoff v2, Phase 2 item 4): an outbound that we KNOW did not land
(`Message.delivery_status == 'failed'`) must never count as an unanswered strike.
Without this, a relay outage reads as user churn — `unanswered_count` climbs, the
engagement tier decays, and coaching is throttled for people who never left.
The pipe can fail; the measurement must not.

Schema (items 1-3): `Message.channel / provider_sid / delivery_status`,
`User.preferred_channel / channel_failed_over / channel_failover_at / photon_user_id`,
all additive with defaults that make every existing row an `sms` / `sent` row.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from tests.factories import make_user


def _outbound(db, user, body="hey — how'd the workout go?", minutes_ago=10, **cols):
    from models import Message
    m = Message(
        user_id=user.id, direction="out", body=body, message_type="evening",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        **cols,
    )
    db.add(m)
    db.commit()
    return m


def _inbound(db, user, body="went well", minutes_ago=5):
    from models import Message
    m = Message(
        user_id=user.id, direction="in", body=body, message_type="freeform",
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    )
    db.add(m)
    db.commit()
    return m


def _count(db, user) -> int:
    from models import User
    db.expire_all()
    return db.get(User, user.id).unanswered_count or 0


# ─── schema: defaults make every existing row an sms/sent row ────────────────

def test_message_channel_columns_default_to_sms_sent(db):
    from models import Message
    user = make_user(db)
    m = _outbound(db, user)
    db.expire_all()
    row = db.get(Message, m.id)
    assert row.channel == "sms"
    assert row.delivery_status == "sent"
    assert row.provider_sid is None


def test_message_channel_columns_accept_imessage_values(db):
    from models import Message
    user = make_user(db)
    m = _outbound(db, user, channel="imessage", provider_sid="photon-abc-123",
                  delivery_status="delivered")
    db.expire_all()
    row = db.get(Message, m.id)
    assert (row.channel, row.provider_sid, row.delivery_status) == \
        ("imessage", "photon-abc-123", "delivered")


def test_user_channel_columns_default_to_sms_not_failed_over(db):
    from models import User
    user = make_user(db)
    db.expire_all()
    row = db.get(User, user.id)
    assert row.preferred_channel == "sms"
    assert row.channel_failed_over is False
    assert row.channel_failover_at is None
    assert row.photon_user_id is None


# ─── migration: adds the columns to a prod-shaped DB, idempotently ───────────

def test_migration_adds_channel_columns_to_pre_existing_schema(db):
    """Simulate prod: tables exist WITHOUT the new columns (drop them), then run
    the real migration and assert they come back. Then run it again (idempotent)."""
    from sqlalchemy import inspect, text
    import models
    from migrate import run_migrations

    with models.engine.begin() as conn:
        for col in ("channel", "provider_sid", "delivery_status"):
            conn.execute(text(f"ALTER TABLE messages DROP COLUMN IF EXISTS {col}"))
        for col in ("preferred_channel", "channel_failed_over",
                    "channel_failover_at", "photon_user_id"):
            conn.execute(text(f"ALTER TABLE users DROP COLUMN IF EXISTS {col}"))

    run_migrations()
    run_migrations()  # idempotent

    insp = inspect(models.engine)
    msg_cols = {c["name"] for c in insp.get_columns("messages")}
    assert {"channel", "provider_sid", "delivery_status"} <= msg_cols
    user_cols = {c["name"] for c in insp.get_columns("users")}
    assert {"preferred_channel", "channel_failed_over",
            "channel_failover_at", "photon_user_id"} <= user_cols
    idx_cols = {tuple(i["column_names"]) for i in insp.get_indexes("messages")}
    assert ("provider_sid",) in idx_cols, "provider_sid lookup index missing"

    # Existing rows are backfilled by the column DEFAULT, not left NULL —
    # a NULL delivery_status would be ambiguous to the keystone.
    with models.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (phone, name) VALUES ('+15550009999', 'Legacy')"))
        conn.execute(text(
            "INSERT INTO messages (user_id, direction, body) "
            "SELECT id, 'out', 'legacy row' FROM users WHERE phone='+15550009999'"))
        ch, ds = conn.execute(text(
            "SELECT channel, delivery_status FROM messages WHERE body='legacy row'")).one()
        pc, cfo = conn.execute(text(
            "SELECT preferred_channel, channel_failed_over FROM users "
            "WHERE phone='+15550009999'")).one()
    assert (ch, ds) == ("sms", "sent")
    assert (pc, cfo) == ("sms", False)


# ─── KEYSTONE ────────────────────────────────────────────────────────────────

def test_failed_outbound_is_not_an_unanswered_strike(db):
    """The keystone. Most recent outbound failed to deliver, no reply →
    unanswered_count UNCHANGED. We know it didn't land; silence is not a strike."""
    from engagement_tracker import increment_unanswered
    user = make_user(db, unanswered_count=0)
    _outbound(db, user, delivery_status="failed", channel="imessage")

    increment_unanswered(user.id)

    assert _count(db, user) == 0


def test_failed_outbound_after_unanswered_sent_one_is_still_not_a_strike(db):
    """Spec wording: only the MOST RECENT outbound's status matters. An older
    delivered-but-unanswered message doesn't get re-counted through a later failure —
    the older one already took its strike (or will, when we next actually reach them)."""
    from engagement_tracker import increment_unanswered
    user = make_user(db, unanswered_count=1)
    _outbound(db, user, body="earlier, delivered", minutes_ago=60, delivery_status="sent")
    _outbound(db, user, body="later, failed", minutes_ago=10, delivery_status="failed")

    increment_unanswered(user.id)

    assert _count(db, user) == 1


def test_sent_outbound_without_reply_still_increments(db):
    """Regression guard: the existing behavior is untouched for messages that landed."""
    from engagement_tracker import increment_unanswered
    user = make_user(db, unanswered_count=0)
    _outbound(db, user, delivery_status="sent")

    increment_unanswered(user.id)

    assert _count(db, user) == 1


def test_sent_outbound_with_reply_does_not_increment(db):
    """Regression guard: a reply after the last outbound clears it (unchanged)."""
    from engagement_tracker import increment_unanswered
    user = make_user(db, unanswered_count=0)
    _outbound(db, user, minutes_ago=10, delivery_status="sent")
    _inbound(db, user, minutes_ago=5)

    increment_unanswered(user.id)

    assert _count(db, user) == 0


def test_legacy_null_delivery_status_counts_as_sent(db):
    """Belt-and-braces: a row whose delivery_status is NULL (should not happen after
    the DEFAULT backfill, but a raw INSERT could produce one) is treated as sent —
    the keystone only exempts an explicit 'failed'."""
    from engagement_tracker import increment_unanswered
    from sqlalchemy import text
    import models
    user = make_user(db, unanswered_count=0)
    with models.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO messages (user_id, direction, body, message_type, created_at, "
            "delivery_status) VALUES (:uid, 'out', 'null status', 'evening', NOW(), NULL)"),
            {"uid": user.id})

    increment_unanswered(user.id)

    assert _count(db, user) == 1


def test_send_sms_logs_outbound_row_as_sms_sent_with_sid(db, sms_capture):
    """The chokepoint's existing logging must stamp the new columns so the keystone
    has something to read on every real outbound, not just the imessage branch."""
    from sms import send_sms
    from models import Message
    user = make_user(db)

    sid = send_sms(user.phone, "one part, no split", user_id=user.id, message_type="evening")

    db.expire_all()
    row = (db.query(Message).filter(Message.user_id == user.id, Message.direction == "out")
           .order_by(Message.created_at.desc()).first())
    assert row is not None
    assert row.channel == "sms"
    assert row.delivery_status == "sent"
    assert row.provider_sid == sid
