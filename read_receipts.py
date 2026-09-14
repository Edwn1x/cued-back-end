"""
iMessage read receipts ("Read 11:04").

The sidecar wraps the provider's read action as POST /read {phone, message_id}.
Flask marks the conversation read the moment it starts GENERATING a reply — right
before the typing bubble — and when it thumbs-ups a suppressed ack. Sequence the
user sees: their text → a pause (reading) → "Read" → dots → the reply. That is how
a person's thread looks; sending "Read" on arrival would look like a bot.

Same contract as typing_indicator: fire-and-forget, short timeout, never raises, only
when the user's RESOLVED channel is iMessage (tripped breaker = nothing). SMS has
no equivalent.
"""

from __future__ import annotations

import logging
import threading

import requests

import config

logger = logging.getLogger("cued.read")

# Live 2026-09-12: 3 of 12 receipts timed out at 2s, every one the FIRST sidecar
# call after a long idle; the typing signal 1s later always succeeded. This runs
# in its own thread (mark_read is fire-and-forget), so a longer cap costs nothing.
TIMEOUT_S = 5.0


def _latest_inbound_sid(user_id: int):
    """Their latest iMessage's Photon id (questions included — this is a read
    receipt, not a tapback), plus their phone and resolved channel."""
    from models import get_session, User, Message
    from sms import _resolve_channel
    session = get_session()
    try:
        row = session.query(User.phone).filter(User.id == user_id).first()
        m = (session.query(Message.provider_sid)
             .filter(Message.user_id == user_id, Message.direction == "in",
                     Message.channel == "imessage", Message.provider_sid.isnot(None))
             .order_by(Message.id.desc()).first())
    finally:
        session.close()
    if not row or not m:
        return None, None, "sms"
    return row[0], m[0], _resolve_channel(user_id)


def _post(phone: str, sid: str, user_id: int) -> bool:
    try:
        resp = requests.post(
            config.SIDECAR_URL.rstrip("/") + "/read",
            json={"phone": phone, "message_id": sid},
            headers={"X-Internal-Secret": config.INTERNAL_SHARED_SECRET},
            timeout=TIMEOUT_S,
        )
        ok = resp.status_code < 300
        logger.info("READ_RECEIPT user=%s upto=%s ok=%s", user_id, sid, ok)
        return ok
    except Exception as e:  # noqa: BLE001 — never let a receipt surface
        logger.info("READ_RECEIPT user=%s upto=%s ok=False err=%s", user_id, sid, e)
        return False


def mark_read(user_id: int, *, wait: bool = False) -> bool:
    """Mark the user's conversation read up to their latest iMessage, if — and only
    if — the flag is on, a sidecar is configured, and their resolved channel is
    iMessage right now. Returns True if a receipt was dispatched."""
    if not config.READ_RECEIPTS_ENABLED or not config.SIDECAR_URL or not user_id:
        return False
    try:
        phone, sid, channel = _latest_inbound_sid(user_id)
    except Exception as e:  # noqa: BLE001
        logger.info("READ_RECEIPT user=%s skipped err=%s", user_id, e)
        return False
    if not phone or not sid or channel != "imessage":
        return False
    if wait:
        return _post(phone, sid, user_id)
    threading.Thread(target=_post, args=(phone, sid, user_id), daemon=True).start()
    return True
