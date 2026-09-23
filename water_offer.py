"""
Water-reminder offer — the one-line, code-sent, once-only invitation that makes the
hydration reminder (reminders.py interval rows, PR #93) discoverable.

Founder (2026-09-22): "users aren't aware of this feature so how can they ask for
it." Before this, the only paths were a mid-relevance capability entry (rarely in the
kickoff rundown) and a reveal cue on "headaches / low energy". Nobody asks for a
feature they've never heard of.

Design (option 1 of three; default-on was rejected as annoying-bot risk):
  - New users: one bubble right after the kickoff rundown.
  - Existing users: once, on a scheduler sweep, only when the heartbeat's own
    guardrails would let a proactive text through (quiet hours, active conversation,
    daily budget, onboarding, opt-out).
  - The reply is handled deterministically in app.py (no model): yes → an interval
    reminder every WATER_OFFER_DEFAULT_HOURS hours between wake and bed ("every 2h"
    in the reply wins); no → remembered, never re-asked. Anything else → the offer
    lapses (status 'lapsed', never re-asked) and the message reaches the model
    normally; the model can still set_reminder if they come back to it later.
  - users.water_offer_status: None | offered | yes | no | lapsed; water_offered_at.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

import config

logger = logging.getLogger("cued.water_offer")

MESSAGE_TYPE = "water_offer"
OFFER_TEXT = ("one more thing: want me to ping u to drink water every few hours while ur up? "
              "yes or no")
YES_REPLY = "bet, every ~{n}h while ur up. say 'stop the water reminders' anytime"
NO_REPLY = "all good, won't bring it up again"
REMINDER_TEXT = "drink water"
WATER_OFFER_DEFAULT_HOURS = 3
WATER_OFFER_MAX_AGE_HOURS = 48    # a yes two days later is still a yes; older → lapsed

# Stretched spellings count (live 2026-09-23: "Nahh I drink a lot of water" matched
# nothing and LAPSED instead of NO).
YES_RE = re.compile(r"^\s*(yes+|yeah+|yea+h*|yeh|yep+|yup+|ya+s*|sure|ok|okay|k|bet|do it|go for it|please|"
                    r"yes please|sounds good|why not|👍|💧)\b", re.I)
NO_RE = re.compile(r"^\s*(no+|nah+|naw|nope+|no thanks|no thank you|i'?m good|im good|not now|don'?t|pass|👎)\b",
                   re.I)
HOURS_RE = re.compile(r"every\s*(\d{1,2})\s*(?:h|hr|hrs|hours?)", re.I)
STATUS_OFFERED, STATUS_YES, STATUS_NO, STATUS_LAPSED = "offered", "yes", "no", "lapsed"
# Outbound types that never ask the user anything (see pending_offer).
NO_QUESTION_TYPES = ("reminder",)


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def eligible(user) -> bool:
    """Never offered, onboarded, active, not opted out, and doesn't already have an
    interval reminder (someone who asked on their own needs no offer)."""
    if not config.WATER_OFFER_ENABLED or not config.WATER_REMINDERS_ENABLED or not config.REMINDERS_ENABLED:
        return False
    if getattr(user, "water_offer_status", None):
        return False
    if not user.active or (user.onboarding_step or 0) < 3:
        return False
    if (getattr(user, "waitlist_status", None) or "") == "pending":
        return False
    if config.STOP_OPTOUT_ENABLED and getattr(user, "opted_out", False):
        return False
    return True


def _has_interval_reminder(session, user_id: int) -> bool:
    from models import Reminder
    return (session.query(Reminder.id)
            .filter(Reminder.user_id == user_id, Reminder.active.is_(True),
                    Reminder.every_hours.isnot(None)).first()) is not None


def send_offer(user_id: int, *, source: str = "sweep") -> bool:
    """Send the one line and mark the row 'offered'. Idempotent: a second call for the
    same user is a no-op. Returns True when a message went out."""
    from models import get_session, User
    from sms import send_sms
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not eligible(user):
            return False
        if _has_interval_reminder(session, user_id):
            user.water_offer_status = STATUS_YES     # they already have one — nothing to offer
            user.water_offered_at = _naive_utcnow()
            session.commit()
            return False
        # Mark BEFORE sending so a send-side retry can't double-offer.
        user.water_offer_status = STATUS_OFFERED
        user.water_offered_at = _naive_utcnow()
        phone = user.phone
        session.commit()
    finally:
        session.close()
    send_sms(phone, OFFER_TEXT, user_id=user_id, message_type=MESSAGE_TYPE)
    logger.info("WATER_OFFER_SENT user=%s source=%s", user_id, source)
    return True


def sweep(now: datetime | None = None) -> int:
    """Existing users: offer once, only when the heartbeat's guardrails would allow a
    proactive text right now (quiet hours, active conversation, daily budget…). Runs
    every few minutes from the scheduler; each user is offered at most once, ever."""
    if not config.WATER_OFFER_ENABLED:
        return 0
    from models import get_session, User
    from heartbeat import guardrail_reason
    session = get_session()
    try:
        users = (session.query(User)
                 .filter(User.active.is_(True), User.water_offer_status.is_(None))
                 .all())
        todo = []
        for u in users:
            if not eligible(u):
                continue
            reason = guardrail_reason(u, session, now=now)
            if reason:
                continue
            todo.append(u.id)
    finally:
        session.close()
    sent = 0
    for uid in todo:
        try:
            if send_offer(uid, source="sweep"):
                sent += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("WATER_OFFER_SWEEP_FAILED user=%s err=%s", uid, e)
    return sent


def pending_offer(session, user) -> bool:
    """True when the user has an unanswered offer whose bubble is still the most recent
    outbound (so a bare 'yes' can only mean this) and it isn't stale."""
    if getattr(user, "water_offer_status", None) != STATUS_OFFERED:
        return False
    at = getattr(user, "water_offered_at", None)
    if at and (_naive_utcnow() - at).total_seconds() > WATER_OFFER_MAX_AGE_HOURS * 3600:
        return False
    from models import Message
    from engagement_tracker import _not_reaction
    # Code-sent bubbles that ask nothing (a fired reminder: "go run") don't take the
    # floor from the offer. Live 2026-09-23: Alex's "Nahh I drink a lot of water" landed
    # 30s after his 7:30 run reminder, was never counted, and the offer stuck at
    # 'offered' for good (a heartbeat/coach bubble CAN carry its own yes/no question,
    # so those still close the window).
    last_out = (session.query(Message)
                .filter(Message.user_id == user.id, Message.direction == "out", _not_reaction(),
                        Message.message_type.notin_(NO_QUESTION_TYPES))
                .order_by(Message.created_at.desc(), Message.id.desc()).first())
    return bool(last_out and last_out.message_type == MESSAGE_TYPE)


def handle_reply(user_id: int, text: str) -> str | None:
    """Deterministic reply to the offer. Returns the one line to send, or None when
    the inbound isn't an answer to a pending offer (normal turn). A non-yes/no reply
    to a pending offer lapses it (never re-asked) and returns None so the model sees
    the message as usual."""
    if not config.WATER_OFFER_ENABLED:
        return None
    from models import get_session, User
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not pending_offer(session, user):
            return None
        body = (text or "").strip()
        if YES_RE.match(body):
            m = HOURS_RE.search(body)
            n = int(m.group(1)) if m else WATER_OFFER_DEFAULT_HOURS
            n = max(1, min(12, n))
            user.water_offer_status = STATUS_YES
            session.commit()
            from reminders import create_reminder
            r = create_reminder(user_id, REMINDER_TEXT, None, every_hours=n, source="offer")
            if "error" in r:
                logger.warning("WATER_OFFER_CREATE_FAILED user=%s err=%s", user_id, r["error"])
                return "hm, couldn't set that up just now. say 'remind me to drink water' later and i'll sort it"
            logger.info("WATER_OFFER_YES user=%s every_hours=%s reminder=%s", user_id, n, r.get("id"))
            return YES_REPLY.format(n=n)
        if NO_RE.match(body):
            user.water_offer_status = STATUS_NO
            session.commit()
            logger.info("WATER_OFFER_NO user=%s", user_id)
            return NO_REPLY
        user.water_offer_status = STATUS_LAPSED
        session.commit()
        logger.info("WATER_OFFER_LAPSED user=%s body=%r", user_id, body[:40])
        return None
    finally:
        session.close()
