"""Stalled-onboarding re-engagement (code-owned, flag-gated).

Live 2026-10-15/16: fifteen real sign-ups in one night; one finished setup. Eleven stalled
mid-intake (most within minutes — a question went out, no answer came back) and NOTHING
contacted them again: the heartbeat only serves onboarded users (step 3), and the
onboarding agent only speaks when they do. Every stalled account sat silent for four days
until the founder nudged a few by hand.

This sweep sends at most TWO nudges per stalled user, ~20h and ~72h after they went quiet,
inside 10:00–21:00 local, one bubble each, always ending in the next plain intake ask so a
reply flows straight back into handle_onboarding_reply. Never to archived, opted-out, or
pending-waitlist users. Idempotent: the count and timing come from the user's own
outbound rows typed MESSAGE_TYPE, so a deploy mid-window can't double-send.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config
from models import get_session, User, Message

logger = logging.getLogger("cued.onboarding_reengage")

MESSAGE_TYPE = "onboarding_nudge"
MAX_NUDGES = 2
LOCAL_HOURS = (10, 21)          # send only between 10:00 and 20:59 local


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local_hour(user, now: datetime) -> int:
    from timefmt import resolve_tz
    return now.replace(tzinfo=timezone.utc).astimezone(resolve_tz(user)).hour


def _nudges(session, user_id: int) -> list:
    return (session.query(Message.created_at)
            .filter(Message.user_id == user_id, Message.direction == "out", Message.message_type == MESSAGE_TYPE)
            .order_by(Message.created_at).all())


def _last_inbound(session, user_id: int):
    return (session.query(Message.created_at)
            .filter(Message.user_id == user_id, Message.direction == "in")
            .order_by(Message.created_at.desc()).first())


def _last_outbound_any(session, user_id: int):
    return (session.query(Message.created_at)
            .filter(Message.user_id == user_id, Message.direction == "out")
            .order_by(Message.created_at.desc()).first())


def _remaining(user) -> list:
    from onboarding_agent import _get_missing_fields
    return _get_missing_fields(user)


def _next_ask(user) -> str:
    from onboarding_agent import _plain, _BIG_ASK_NEVER
    missing = _remaining(user)
    field = next((f for f in missing if f[0] not in _BIG_ASK_NEVER), missing[0] if missing else None)
    return _plain(field) if field else "how's ur day going"


def nudge_text(user, n: int, *, replied: bool) -> str:
    """n = 1 or 2 (which nudge this is). One bubble, ends in the next ask."""
    name = (user.name or "").split()[0] if user.name else ""
    hey = f"hey {name}" if name else "hey"
    if n >= 2:
        return (f"{hey}, last one from me — if u still want in, just reply and we pick up where we left off. "
                f"no stress if not")
    if not replied:
        return (f"{hey}, it's cued — ur spot's still open, setup takes like 2 min. "
                f"wanna start? first up: {_next_ask(user)}?")
    left = len([f for f in _remaining(user) if f[0] != "wake_sleep"]) or 1
    things = "1 quick thing" if left == 1 else f"{left} quick things"
    return f"{hey}, still down to finish setup? {things} left and ur in — {_next_ask(user)}?"


def eligible(session, user, now: datetime) -> tuple[bool, str, int, bool]:
    """(eligible, reason, next_nudge_number, replied_ever)."""
    if (user.onboarding_step or 0) >= 3:
        return False, "onboarded", 0, False
    if user.archived_at or getattr(user, "opted_out", False) or user.waitlist_status == "pending":
        return False, "excluded", 0, False
    if not user.preferred_channel and not user.phone:
        return False, "no_channel", 0, False
    sent = _nudges(session, user.id)
    if len(sent) >= MAX_NUDGES:
        return False, "max_nudges", 0, False
    last_in = _last_inbound(session, user.id)
    last_out = _last_outbound_any(session, user.id)
    if last_out is None:
        return False, "never_contacted", 0, False       # the hook hasn't gone out yet — not ours
    replied = last_in is not None
    quiet_since = max([t[0] for t in (last_in, last_out) if t])   # the thread's last word, either side
    first_gap = timedelta(hours=config.ONBOARDING_REENGAGE_FIRST_HOURS)
    second_gap = timedelta(hours=config.ONBOARDING_REENGAGE_SECOND_HOURS)
    if not sent:
        if now - quiet_since < first_gap:
            return False, "too_soon", 1, replied
        return True, "first", 1, replied
    # second nudge: measured from the first nudge, and only if they stayed silent after it
    if last_in is not None and last_in[0] > sent[-1][0]:
        return False, "replied_after_nudge", 2, replied
    if now - sent[-1][0] < second_gap:
        return False, "too_soon", 2, replied
    return True, "second", 2, replied


def sweep(now: datetime | None = None) -> int:
    if not config.ONBOARDING_REENGAGE_ENABLED:
        return 0
    now = now or _naive_utcnow()
    from sms import send_sms
    session = get_session()
    sent_n = 0
    try:
        users = (session.query(User)
                 .filter(User.onboarding_step < 3, User.archived_at.is_(None))
                 .all())
        for u in users:
            ok, reason, n, replied = eligible(session, u, now)
            if not ok:
                continue
            h = _local_hour(u, now)
            if not (LOCAL_HOURS[0] <= h < LOCAL_HOURS[1]):
                logger.info("ONBOARDING_REENGAGE_DEFERRED user=%s n=%s reason=local_hour=%s", u.id, n, h)
                continue
            text = nudge_text(u, n, replied=replied)
            try:
                send_sms(u.phone, text, user_id=u.id, message_type=MESSAGE_TYPE)
                sent_n += 1
                logger.info("ONBOARDING_REENGAGE_SENT user=%s n=%s replied_before=%s text=%r", u.id, n, replied, text[:90])
            except Exception as e:  # noqa: BLE001 — one failure must not stop the sweep
                logger.warning("ONBOARDING_REENGAGE_FAILED user=%s err=%s", u.id, e)
    finally:
        session.close()
    return sent_n
