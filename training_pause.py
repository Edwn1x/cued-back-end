"""
A training pause the coach can set — "sick", "travelling", "exam week", "not training yet".

Live 2026-10-28 (user 80): a week-long chest cough, "ill tell you once I feel better" — and
that evening the RSF beat said "gym's quiet right now. quick push?". Nothing in code knew.
users.training_paused_until (naive UTC) + training_pause_reason; set by the pause_training
tool (agent_tools), read by gym_beats.propose, reminders._fire_one (the program's "gym"
row), heartbeat._training_gap_signal and the loop's TRAINING PAUSED block. Read-only here
except set_pause.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("cued.training_pause")

MAX_DAYS = 90


def _now(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    return now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now


def is_paused(user, now=None) -> bool:
    until = getattr(user, "training_paused_until", None)
    return bool(until) and until > _now(now)


def describe(user) -> str:
    """'until Sat Nov 1 (chest cough)' in the user's local zone, or ''."""
    until = getattr(user, "training_paused_until", None)
    if not until:
        return ""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(getattr(user, "user_timezone", None) or "America/Los_Angeles")
        day = until.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%a %b %-d")
    except Exception:  # noqa: BLE001
        day = until.date().isoformat()
    reason = (getattr(user, "training_pause_reason", None) or "").strip()
    return f"until {day}" + (f" ({reason})" if reason else "")


def set_pause(user_id: int, days: int, reason: str | None = None, *, now=None) -> dict:
    """days=0 resumes (clears); 1..MAX_DAYS pauses from now. {ok, until, reason} or {error}."""
    from models import get_session, User
    try:
        days = int(days)
    except (TypeError, ValueError):
        return {"error": "days must be a whole number"}
    if days < 0 or days > MAX_DAYS:
        return {"error": f"days must be 0 (resume) to {MAX_DAYS}"}
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return {"error": "user not found"}
        if days == 0:
            u.training_paused_until = None
            u.training_pause_reason = None
            session.commit()
            logger.info("TRAINING_PAUSE_CLEARED user=%s", user_id)
            return {"ok": True, "until": None, "reason": None}
        until = _now(now) + timedelta(days=days)
        u.training_paused_until = until
        u.training_pause_reason = (reason or "").strip()[:100] or None
        session.commit()
        logger.info("TRAINING_PAUSE_SET user=%s days=%s until=%s reason=%r", user_id, days, until, u.training_pause_reason)
        return {"ok": True, "until": until, "reason": u.training_pause_reason, "describe": describe(u)}
    finally:
        session.close()
