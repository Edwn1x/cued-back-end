"""
Held outbound — one number, delivered late rather than from a different one.

Live 2026-09-28 04:44 UTC: Photon's upstream answered "Service temporarily
unavailable" for ~14 minutes. sms.send_sms fell over to Twilio, tripped the
user's breaker, and three green texts landed in her old thread from a number she
wasn't talking to. Her ask: stay on the one number; if the line is down, hold the
message, say something light about it, and send it over once the line is back.

This module is the hold + the drain:

  hold(...)        — sms.send_sms parks a message here after its inline retries.
                     One 'held' Message row per bubble is written at the same time
                     (the conversation window sees what the coach meant to say, the
                     engagement gates ignore it: engagement_tracker._landed).
  drain()          — scheduler job (IMESSAGE_HOLD_DRAIN_SECONDS). Expires stale
                     rows, then per user, in id order, re-sends through
                     sms._send_bubbles. The first delivery for a user is preceded
                     by the heads-up bubble (IMESSAGE_HOLD_NOTE) when the oldest
                     row waited ≥ IMESSAGE_HOLD_NOTE_MIN_AGE_S. A transient
                     failure re-arms the row with backoff and re-flags the outage
                     (sms.note_outage) so live sends keep queuing behind.
  drain_user(id)   — the inline variant sms.send_sms calls when a user has a
                     backlog and no outage is flagged: deliver the backlog first,
                     then the live message, so bubbles read in the order the coach
                     said them. Returns True when nothing is left held.

Expiry: proactive types (heartbeat, reminder, goodnight, gym beat, session probe)
go stale fast and are dropped after IMESSAGE_HOLD_PROACTIVE_MAX_AGE_MIN; replies
wait IMESSAGE_HOLD_MAX_AGE_MIN. An expired row's Message rows flip to 'failed'
(never landed → the keystone doesn't count it). All timestamps naive UTC.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

import config
from models import get_session, HeldOutbound

logger = logging.getLogger("cued.held_outbound")

# Types that are noise if they land late — expire fast, never re-sent stale.
PROACTIVE_TYPES = frozenset({"heartbeat", "reminder", "goodnight", "gym_summon", "session_probe",
                             "water_offer", "workout_intro", "routine_capture_offer"})
RETRY_BACKOFF_S = (30, 60, 120, 300)
NOTE_TYPE = "outage_note"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ttl_minutes(message_type: str | None) -> int:
    if message_type in PROACTIVE_TYPES:
        return config.IMESSAGE_HOLD_PROACTIVE_MAX_AGE_MIN
    return config.IMESSAGE_HOLD_MAX_AGE_MIN


def hold(user_id: int, phone: str, body: str, bubbles: list[str], message_type: str,
         reply_to_sid: str | None, reason: str, error: str | None = None) -> int:
    """Park `body` for `user_id`. Writes the 'held' Message rows (one per bubble)
    first, then the queue row that points at them. Returns the queue row id."""
    from sms import _log_message  # local: sms imports this module lazily too
    now = _now()
    row_ids = [
        _log_message(user_id, part, message_type, channel="imessage",
                     provider_sid=None, delivery_status="held")
        for part in bubbles
    ]
    session = get_session()
    try:
        row = HeldOutbound(
            user_id=user_id, phone=phone, body=body, message_type=message_type or "freeform",
            reply_to_sid=reply_to_sid, status="held", attempts=0,
            last_error=(error or "")[:300] or None, message_ids=row_ids,
            created_at=now, next_attempt_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes(message_type)),
        )
        session.add(row)
        session.commit()
        rid = row.id
    finally:
        session.close()
    logger.info("IMESSAGE_HELD_QUEUED id=%s user_id=%s type=%s reason=%s bubbles=%d",
                rid, user_id, message_type, reason, len(bubbles))
    return rid


def pending_count(user_id: int) -> int:
    session = get_session()
    try:
        return (session.query(HeldOutbound)
                .filter(HeldOutbound.user_id == user_id, HeldOutbound.status.in_(("held", "sending")))
                .count())
    finally:
        session.close()


def _claim(row_id: int) -> bool:
    """held → sending, atomically. False if another drain got there first."""
    session = get_session()
    try:
        res = session.execute(
            text("UPDATE held_outbound SET status='sending' WHERE id=:id AND status='held'"),
            {"id": row_id},
        )
        session.commit()
        return (res.rowcount or 0) == 1
    finally:
        session.close()


def _set(row_id: int, **fields):
    session = get_session()
    try:
        row = session.get(HeldOutbound, row_id)
        if row is None:
            return
        for k, v in fields.items():
            setattr(row, k, v)
        session.commit()
    finally:
        session.close()


def _expire(row: HeldOutbound):
    from sms import _flip_row
    for mid in (row.message_ids or []):
        _flip_row(mid, provider_sid=None, delivery_status="failed")
    _set(row.id, status="expired")
    logger.error("IMESSAGE_HELD_EXPIRED id=%s user_id=%s type=%s age_min=%.0f attempts=%s — dropped, "
                 "never delivered", row.id, row.user_id, row.message_type,
                 (_now() - row.created_at).total_seconds() / 60, row.attempts)


def _rearm(row_id: int, attempts: int, err) -> None:
    from sms import note_outage
    delay = RETRY_BACKOFF_S[min(attempts, len(RETRY_BACKOFF_S) - 1)]
    _set(row_id, status="held", attempts=attempts + 1, last_error=str(err)[:300],
         next_attempt_at=_now() + timedelta(seconds=delay))
    note_outage()


def _deliver(row: HeldOutbound, send_note: bool) -> bool:
    """Try to land one held row (heads-up first, if asked). True when it landed
    (or maybe-landed on a read timeout — never re-sent). False → re-armed."""
    from sms import _send_bubbles, _imessage_body, split_bubbles, clear_outage, _typing_stop
    if not _claim(row.id):
        return False
    if send_note:
        _, err, timeout = _send_bubbles(row.phone, [config.IMESSAGE_HOLD_NOTE], None,
                                        row.user_id, NOTE_TYPE)
        if err is not None and not timeout:
            _rearm(row.id, row.attempts, err)
            return False
    bubbles = [_imessage_body(b) for b in split_bubbles(row.body)]
    row_ids = list(row.message_ids or [])
    _, err, timeout = _send_bubbles(row.phone, bubbles, row.reply_to_sid, row.user_id,
                                    row.message_type, row_ids=row_ids)
    if err is not None and not timeout:
        _rearm(row.id, row.attempts, err)
        return False
    _set(row.id, status="sent", sent_at=_now(), attempts=row.attempts + 1)
    _typing_stop(row.user_id)
    clear_outage()
    logger.info("IMESSAGE_HELD_DELIVERED id=%s user_id=%s type=%s waited_s=%.0f attempts=%s note=%s",
                row.id, row.user_id, row.message_type,
                (_now() - row.created_at).total_seconds(), row.attempts + 1, send_note)
    return True


def _due_rows(user_id: int | None, now: datetime) -> list[HeldOutbound]:
    session = get_session()
    try:
        q = session.query(HeldOutbound).filter(HeldOutbound.status == "held")
        if user_id is not None:
            q = q.filter(HeldOutbound.user_id == user_id)
        rows = q.order_by(HeldOutbound.user_id, HeldOutbound.id).all()
        session.expunge_all()
        return [r for r in rows if (r.next_attempt_at or now) <= now or r.expires_at <= now]
    finally:
        session.close()


def _drain_rows(rows: list[HeldOutbound], now: datetime) -> dict:
    """Per user, in id order. The heads-up goes before the FIRST delivery of a user
    whose oldest held row waited long enough. One failure stops that user for this
    pass (order); other users still get their probe."""
    out = {"sent": 0, "expired": 0, "stuck": 0}
    by_user: dict[int, list[HeldOutbound]] = {}
    for r in rows:
        by_user.setdefault(r.user_id, []).append(r)
    for uid, urows in by_user.items():
        noted = False
        for r in urows:
            if r.expires_at <= now:
                _expire(r)
                out["expired"] += 1
                continue
            waited = (now - r.created_at).total_seconds()
            send_note = (not noted) and waited >= config.IMESSAGE_HOLD_NOTE_MIN_AGE_S
            try:
                landed = _deliver(r, send_note)
            except Exception as e:  # noqa: BLE001 — a row must never wedge in 'sending'
                logger.exception("IMESSAGE_HELD_DELIVER_CRASHED id=%s user_id=%s", r.id, r.user_id)
                _rearm(r.id, r.attempts, e)
                landed = False
            if landed:
                out["sent"] += 1
                noted = True  # one heads-up per user per pass, ever needed once
            else:
                out["stuck"] += 1
                break
    return out


def drain(now: datetime | None = None) -> dict:
    """Scheduler job. Cheap when nothing is held (one indexed query)."""
    now = now or _now()
    rows = _due_rows(None, now)
    if not rows:
        return {"sent": 0, "expired": 0, "stuck": 0}
    out = _drain_rows(rows, now)
    logger.info("IMESSAGE_HELD_DRAIN sent=%s expired=%s stuck=%s", out["sent"], out["expired"], out["stuck"])
    return out


def drain_user(user_id: int, now: datetime | None = None) -> bool:
    """Inline drain for one user (sms.send_sms, before a live send). Ignores
    per-row backoff — the live send is itself the probe. True = nothing left held."""
    now = now or _now()
    session = get_session()
    try:
        rows = (session.query(HeldOutbound)
                .filter(HeldOutbound.user_id == user_id, HeldOutbound.status == "held")
                .order_by(HeldOutbound.id).all())
        session.expunge_all()
    finally:
        session.close()
    if rows:
        _drain_rows(rows, now)
    return pending_count(user_id) == 0
