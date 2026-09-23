"""STOP opt-out flow (iMessage — SMS opt-out is handled by Twilio/carrier upstream).

Design goal: NEVER lose a user to an ACCIDENTAL opt-out. So the whole flow is
deliberately high-friction:

  1. TRIGGER — the WHOLE message (trimmed) is "STOP" or "UNSUBSCRIBE", any case, with
     or without a trailing period/exclamation. Founder 2026-09-23: the confirmation
     step is the buffer, so the trigger no longer needs the caps+period ritual. A
     "stop" inside a sentence ("stop asking me that") still does nothing here.
  2. CONFIRMATION — the trigger does NOT opt them out. It sets pending_optout_confirm
     and sends one confirmation offering: opt out for good (reply STOP again),
     "pause" (a few quiet days), or anything else = stay.
  3. RESOLUTION — while pending: another STOP → opted_out; "pause" → quiet_until
     set STOP_PAUSE_DAYS out; anything else → clear the pending flag and continue as a
     normal turn (they stay).
  4. RESUME — any inbound from an opted-out (or paused) user brings them right back.

Sends while opted_out are suppressed at the send chokepoint (sms.send_sms) and in the
proactive guardrail (heartbeat.guardrail_reason). The opt-out goodbye is sent BEFORE the
flag is set, so it isn't blocked by its own suppression.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta

import config

logger = logging.getLogger("cued.optout")

# The whole message must be one of these words — any case, optional trailing "." / "!".
# Kept as a tuple for the log line / tests; the match is is_trigger().
TRIGGERS = ("STOP", "UNSUBSCRIBE")
_TRIGGER_RE = re.compile(r"^\s*(stop|unsubscribe)\s*[.!]*\s*$", re.I)


def is_trigger(trimmed: str) -> bool:
    return bool(_TRIGGER_RE.match(trimmed or ""))


CONFIRM_MSG = ("wanna stop all texts from me? reply STOP again to opt out for good, "
               "or 'pause' to take a few days off. anything else and we're good")
GOODBYE_MSG = "you're off the texts. text me anytime and i'm right back"


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _is_pause(trimmed: str) -> bool:
    return trimmed.lower().rstrip(".") == "pause"


def handle_optout_flow(session, user, body: str, *, message_id=None, channel: str = "sms") -> bool:
    """Run the opt-out state machine for one inbound. Returns True if this inbound was
    TERMINAL (a confirmation was sent, they opted out, or they paused — don't process
    further), False otherwise (no trigger, a resume, or a declined confirmation — let the
    normal pipeline continue). Flag-gated: a no-op returning False when disabled."""
    if not config.STOP_OPTOUT_ENABLED:
        return False
    from sms import send_sms
    trimmed = (body or "").strip()

    # 1) RESUME — any inbound reactivates an opted-out user, then the coach handles it.
    if getattr(user, "opted_out", False):
        user.opted_out = False
        user.pending_optout_confirm = False
        session.commit()
        logger.info("OPTOUT_RESUME user=%s (inbound while opted out)", user.id)
        return False  # not terminal — let the coach respond to their message

    # 2) RESOLVE a pending confirmation.
    if getattr(user, "pending_optout_confirm", False):
        user.pending_optout_confirm = False
        if is_trigger(trimmed):
            # Confirmed. Send the goodbye BEFORE flipping the flag (suppression reads it).
            try:
                send_sms(user.phone, GOODBYE_MSG, user_id=user.id, message_type="optout_goodbye")
            except Exception:
                logger.exception("OPTOUT_GOODBYE_SEND_FAILED user=%s", user.id)
            user.opted_out = True
            session.commit()
            logger.info("OPTOUT_CONFIRMED user=%s", user.id)
            return True
        if _is_pause(trimmed):
            user.quiet_until = _naive_utcnow() + timedelta(days=config.STOP_PAUSE_DAYS)
            session.commit()
            try:
                send_sms(user.phone, f"cool, going quiet for {config.STOP_PAUSE_DAYS} days. "
                         "text me anytime and i'm back", user_id=user.id, message_type="optout_pause")
            except Exception:
                logger.exception("OPTOUT_PAUSE_SEND_FAILED user=%s", user.id)
            logger.info("OPTOUT_PAUSED user=%s days=%s", user.id, config.STOP_PAUSE_DAYS)
            return True
        # Anything else → they stay. Clear pending, let the coach handle THIS message.
        session.commit()
        logger.info("OPTOUT_CONFIRM_DECLINED user=%s (stays)", user.id)
        return False

    # 3) FRESH TRIGGER — "STOP" / "UNSUBSCRIBE" as the whole message → confirmation, no opt-out yet.
    if is_trigger(trimmed):
        user.pending_optout_confirm = True
        session.commit()
        try:
            send_sms(user.phone, CONFIRM_MSG, user_id=user.id, message_type="optout_confirm")
        except Exception:
            logger.exception("OPTOUT_CONFIRM_SEND_FAILED user=%s", user.id)
        logger.info("OPTOUT_CONFIRM_SENT user=%s trigger=%r", user.id, trimmed)
        return True

    return False


def is_opted_out(user_id: int) -> bool:
    """Cheap check for the send/guardrail suppression path. Flag-gated."""
    if not config.STOP_OPTOUT_ENABLED or not user_id:
        return False
    from models import get_session, User
    session = get_session()
    try:
        u = session.get(User, user_id)
        return bool(u and getattr(u, "opted_out", False))
    finally:
        session.close()
