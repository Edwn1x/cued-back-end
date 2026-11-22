"""
Outbox (outbox.py): one door, one pace, one budget for everything cued sends on its own.

Every proactive sender — the heartbeat, the reminder firer (water stickers, "gym time", the
quiet check-in), the gym beats, the setup sweep (connect offers, the water offer, the
re-engage nudge), tasks — has its own clock and its own send rights, and none of them can
see the others or the live thread. The 11-thread audit (2026-11-21) is what that looks like
from the phone: twelve bubbles in 30 seconds from five senders (u56); "wake up, 61c midterm
at 12" fired at a founder who had been texting for 15 minutes (u51, 10030→10046); nine
outbound in 61 seconds including two offers stacked on an unanswered question (u80,
7820–7830); a water sticker, a heartbeat and a reminder in the same minute (u42, 11468–11472);
and 200 / 130 / 115 / 123 code-fired pings into threads that had been silent for weeks (u33,
u40, u80, u77). The heartbeat alone already had an "active conversation" gate and a daily cap;
nothing else did, and nothing counted across senders.

This module is called from sms.send_sms — the one path every outbound takes — for the
GATED_TYPES only (a reply to the user is never gated). Rules, in order:

  1. live thread: the person texted in the last OUTBOX_LIVE_THREAD_MIN minutes, or a reply
     turn is in flight for them → HOLD until the window closes (held_outbound, same queue
     Photon outages use). A coach that is mid-conversation does not also fire a timer.
  2. merge window: another gated send went to them in the last OUTBOX_MERGE_WINDOW_S → HOLD
     until the window closes, so the drain delivers the queue in order as one paced group
     instead of three senders landing in nine seconds.
  3. daily budget: BROADCAST_TYPES (the coach talking on its own clock: heartbeat, gym beats,
     offers, nudges, check-ins) count toward OUTBOX_DAILY_MAX per local day → DROP past it.
     ASKED_TYPES (a reminder they set, the water stickers they said yes to, the program brief)
     are never budgeted and are delivered late rather than dropped.
  4. silence downgrade (OUTBOX_SILENCE_DOWNGRADE_ENABLED, default OFF — founder 2026-11-04:
     pings ask, never pause): a quiet check-in ("still want these?") unanswered for
     OUTBOX_SILENCE_GRACE_H hours while they stay quiet → broadcast types DROP; the water
     stickers and the reminders they set keep going. reminders._fire_one applies the same
     test to the program's own "gym time" rows.

A broadcast line held under rule 1 or 2 is dropped at drain time if the person spoke after it
was queued: the moment it was written for has passed (held_outbound._drain_rows). On SMS
(no hold queue) a HOLD becomes: broadcast → drop, asked → send now.

Flag OUTBOX_ENABLED (default on). Any failure inside decide() → "send": the gate can only
ever make cued quieter, never silent by accident.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger("cued.outbox")

# The coach talking on its own clock. Budgeted; dropped if they spoke while it was held.
BROADCAST_TYPES = frozenset({
    "heartbeat", "water_offer", "connect_offer", "onboarding_nudge", "onboarding_nudge_more",
    "goodnight", "session_probe", "task", "reminder_checkin",
    "gym_dead", "gym_line_d1", "gym_line_d2", "gym_optin_ask", "gym_summon",
})
# Something they asked for, firing on time. Never budgeted; late beats dropped.
ASKED_TYPES = frozenset({"reminder", "water_reminder", "program_brief"})
GATED_TYPES = BROADCAST_TYPES | ASKED_TYPES

HOLD_PREFIX = "outbox:"


@dataclass(frozen=True)
class Verdict:
    action: str            # send | hold | drop
    reason: str
    delay_s: int = 0

    @property
    def hold_note(self) -> str:
        return f"{HOLD_PREFIX}{self.reason}"


SEND = Verdict("send", "ok")


def enabled() -> bool:
    return bool(getattr(config, "OUTBOX_ENABLED", True))


def gated(message_type: str | None) -> bool:
    return enabled() and (message_type or "") in GATED_TYPES


def is_broadcast(message_type: str | None) -> bool:
    return (message_type or "") in BROADCAST_TYPES


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _in_flight(phone: str | None) -> bool:
    if not phone:
        return False
    try:
        from message_buffer import is_in_flight
        return is_in_flight(phone)
    except Exception:  # noqa: BLE001
        return False


def _last_inbound_age_s(session, user_id: int, now: datetime) -> float | None:
    from models import Message
    row = (session.query(Message.created_at)
           .filter(Message.user_id == user_id, Message.direction == "in")
           .order_by(Message.created_at.desc()).first())
    if not row or not row[0]:
        return None
    return (now - row[0].replace(tzinfo=None)).total_seconds()


def _last_gated_out_age_s(session, user_id: int, now: datetime) -> float | None:
    """Age of the newest gated outbound row — sent OR held (a held row is already on its way)."""
    from models import Message
    row = (session.query(Message.created_at)
           .filter(Message.user_id == user_id, Message.direction == "out",
                   Message.message_type.in_(tuple(GATED_TYPES)),
                   Message.delivery_status.in_(("sent", "held", "queued")))
           .order_by(Message.created_at.desc()).first())
    if not row or not row[0]:
        return None
    return (now - row[0].replace(tzinfo=None)).total_seconds()


def broadcast_count_today(session, user, now: datetime | None = None) -> int:
    from models import Message
    from timefmt import local_day_bounds
    day0, day1 = local_day_bounds(user, now=now) if now else local_day_bounds(user)
    return (session.query(Message.id)
            .filter(Message.user_id == user.id, Message.direction == "out",
                    Message.message_type.in_(tuple(BROADCAST_TYPES)),
                    Message.delivery_status.in_(("sent", "held", "queued")),
                    Message.created_at >= day0, Message.created_at < day1)
            .count())


def silence_downgraded(user, session, now: datetime | None = None) -> bool:
    """True when the quiet check-in went unanswered for OUTBOX_SILENCE_GRACE_H hours and they
    are still quiet: the broadcast stream stands down until they speak. Flag-gated OFF."""
    if not getattr(config, "OUTBOX_SILENCE_DOWNGRADE_ENABLED", False):
        return False
    now = now or _now()
    from models import Message
    from stale_skip import days_quiet
    try:
        dq = days_quiet(user, session, now=now)
    except Exception as e:  # noqa: BLE001
        logger.warning("OUTBOX_QUIET_DAYS_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return False
    limit = float(getattr(config, "REMINDER_MAX_QUIET_DAYS", 5))
    if dq is None or dq <= limit:
        return False
    checkin = (session.query(Message.created_at)
               .filter(Message.user_id == user.id, Message.direction == "out",
                       Message.message_type == "reminder_checkin")
               .order_by(Message.created_at.desc()).first())
    if not checkin or not checkin[0]:
        return False
    grace = timedelta(hours=float(getattr(config, "OUTBOX_SILENCE_GRACE_H", 48)))
    if now - checkin[0].replace(tzinfo=None) < grace:
        return False
    answered = (session.query(Message.id)
                .filter(Message.user_id == user.id, Message.direction == "in",
                        Message.created_at > checkin[0]).first())
    return answered is None


def decide(user_id: int | None, message_type: str | None, *, phone: str | None = None,
           now: datetime | None = None) -> Verdict:
    """The verdict for one proactive send. Never raises; any failure → SEND."""
    if not user_id or not gated(message_type):
        return SEND
    now = now or _now()
    try:
        from models import get_session, User
        session = get_session()
        try:
            user = session.get(User, user_id)
            if user is None:
                return SEND
            # 4. silence downgrade (flag OFF by default) — broadcast only
            if is_broadcast(message_type) and silence_downgraded(user, session, now):
                return Verdict("drop", "silence_downgrade")
            # 3. daily budget — broadcast only
            if is_broadcast(message_type):
                cap = int(getattr(config, "OUTBOX_DAILY_MAX", 6))
                if cap > 0 and broadcast_count_today(session, user, now) >= cap:
                    return Verdict("drop", "daily_budget")
            # 1. live thread
            live_s = int(getattr(config, "OUTBOX_LIVE_THREAD_MIN", 3)) * 60
            if live_s > 0:
                if _in_flight(phone or getattr(user, "phone", None)):
                    return Verdict("hold", "live_thread", live_s)
                age = _last_inbound_age_s(session, user_id, now)
                if age is not None and age < live_s:
                    return Verdict("hold", "live_thread", max(15, int(live_s - age) + 10))
            # 2. merge window
            merge_s = int(getattr(config, "OUTBOX_MERGE_WINDOW_S", 60))
            if merge_s > 0:
                age = _last_gated_out_age_s(session, user_id, now)
                if age is not None and age < merge_s:
                    return Verdict("hold", "merge_window", max(15, int(merge_s - age) + 5))
            return SEND
        finally:
            session.close()
    except Exception as e:  # noqa: BLE001 — the gate can only ever make cued quieter, never silent by accident
        logger.warning("OUTBOX_DECIDE_FAILED user=%s type=%s err=%s", user_id, message_type, e)
        return SEND


def spoke_since(session, user_id: int, since: datetime) -> bool:
    from models import Message
    return (session.query(Message.id)
            .filter(Message.user_id == user_id, Message.direction == "in", Message.created_at > since)
            .first()) is not None
