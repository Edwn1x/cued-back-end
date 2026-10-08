"""inbound_recovery — the turns a dead process never finished.

The inbound buffer is in-memory (message_buffer.py). Live 2026-10-08 13:44 PT (user 48): a
photo landed two minutes before a deploy swapped the container; the 45–60s timer died with
the process; the inbound row was stored, no reply ever went out, the image bytes were gone.

message_buffer keeps an `inbound_pending` row per armed turn and deletes it at flush. On
boot, a marker older than INBOUND_RECOVERY_MIN_AGE_S is an orphan:
  • a TEXT turn is replayed from the stored inbound rows (first_message_id onward) through
    the normal pipeline — same body, same classify, one reply;
  • a PHOTO turn gets ONE honest line (the bytes never persist): "ur pic came in while i was
    updating and didn't make it thru — send it again?"
Markers older than INBOUND_RECOVERY_MAX_AGE_MIN are dropped (too late to be "now").
The marker is deleted BEFORE acting, so a crash mid-recovery can't loop.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import config
from models import get_session, InboundPending, Message, User

logger = logging.getLogger("cued.inbound_recovery")

PHOTO_RECOVERY_LINE = "ur pic came in while i was updating and didn't make it thru. send it again?"
RECOVERY_MESSAGE_TYPE = "inbound_recovery"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def recover_orphans(process_callback, *, now=None) -> dict:
    """Boot-time sweep. Returns {"replayed": n, "photo_notes": n, "dropped": n, "fresh": n}."""
    out = {"replayed": 0, "photo_notes": 0, "dropped": 0, "fresh": 0}
    if not config.INBOUND_RECOVERY_ENABLED:
        return out
    ref = now or _utcnow()
    s = get_session()
    try:
        rows = s.query(InboundPending).all()
        work = []
        for r in rows:
            age = (ref - (r.updated_at or r.created_at or ref)).total_seconds()
            if age < config.INBOUND_RECOVERY_MIN_AGE_S:
                out["fresh"] += 1                      # armed by THIS process (or just now) — leave it
                continue
            s.delete(r)                                # claim it: never replayed twice
            if age > config.INBOUND_RECOVERY_MAX_AGE_MIN * 60:
                out["dropped"] += 1
                logger.warning("INBOUND_ORPHAN_DROPPED phone=%s user=%s age_s=%d", r.phone, r.user_id, age)
                continue
            work.append((r.phone, r.user_id, r.first_message_id, bool(r.has_image), age))
        s.commit()
    finally:
        s.close()

    for phone, user_id, first_id, has_image, age in work:
        try:
            s = get_session()
            try:
                user = s.get(User, user_id)
                q = s.query(Message).filter(Message.user_id == user_id, Message.direction == "in")
                if first_id:
                    q = q.filter(Message.id >= first_id)
                else:
                    q = q.filter(Message.created_at >= ref - timedelta(minutes=config.INBOUND_RECOVERY_MAX_AGE_MIN))
                ins = q.order_by(Message.id).all()
                bodies = [m.body for m in ins if m.body]
                phone_now = user.phone if user else phone
            finally:
                s.close()
            if not user:
                continue
            if has_image:
                from sms import send_sms
                send_sms(phone_now, PHOTO_RECOVERY_LINE, user_id=user_id, message_type=RECOVERY_MESSAGE_TYPE)
                out["photo_notes"] += 1
                logger.warning("INBOUND_ORPHAN_PHOTO user=%s age_s=%d — asked them to resend", user_id, age)
                continue
            from sms import IMAGE_MARKER
            combined = "\n".join(b.replace(IMAGE_MARKER, "").strip() for b in bodies).strip()
            if not combined:
                logger.warning("INBOUND_ORPHAN_EMPTY user=%s age_s=%d", user_id, age)
                continue
            try:
                from app import classify_message
                mtype = classify_message(combined, has_image=False)
            except Exception:  # noqa: BLE001
                mtype = "freeform"
            logger.warning("INBOUND_ORPHAN_REPLAY user=%s age_s=%d rows=%d", user_id, age, len(ins))
            process_callback(user_id, combined, mtype, None, images=[])
            out["replayed"] += 1
        except Exception as e:  # noqa: BLE001 — one user's recovery never blocks the rest
            logger.error("INBOUND_ORPHAN_FAILED user=%s err=%s", user_id, e, exc_info=True)
    if any(out.values()):
        logger.info("INBOUND_RECOVERY %s", out)
    return out
