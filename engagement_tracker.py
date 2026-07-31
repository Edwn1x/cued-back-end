"""
Engagement Tracker — Cued
==========================
Manages decay logic for unresponsive users.

Tiers based on unanswered_count:
  0-1  FULL       — all touchpoints, all questions
  2-3  QUIET      — all touchpoints, no questions (drop post_workout check-in)
  4-5  MINIMAL    — morning_briefing + one meal + evening_wrap only
  6+   NUDGE      — one low-pressure message per day, no schedule

Content (meals, workouts) keeps coming in FULL and QUIET.
What decays is questions and check-in frequency.
"""

from datetime import datetime, timezone

from models import get_session, User, Message


def has_unanswered_outbound(user_id: int) -> bool:
    """
    Return True if the last outbound message sent TODAY has not received a reply.
    Prevents back-to-back unsolicited messages when the user hasn't responded.
    Only looks at today's window — a stale unanswered message from yesterday
    shouldn't block today's scheduled touchpoints.

    Lives here (a coach-free outbound-gating helper) rather than in scheduler.py so
    the proactive path — the heartbeat — can use it without the single-agent brain's
    import closure reaching the legacy templated scheduler (and through it, coach.py).
    See rewrite/phase-6/INVESTIGATION.md (new-path isolation).
    """
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        last_out_today = (
            session.query(Message)
            .filter(
                Message.user_id == user_id,
                Message.direction == "out",
                Message.created_at >= today_start,
            )
            .order_by(Message.created_at.desc())
            .first()
        )
        if not last_out_today:
            return False  # nothing sent today, no block

        last_in_today = (
            session.query(Message)
            .filter(
                Message.user_id == user_id,
                Message.direction == "in",
                Message.created_at >= today_start,
            )
            .order_by(Message.created_at.desc())
            .first()
        )
        if not last_in_today:
            return True  # sent today, no reply today
        return last_out_today.created_at > last_in_today.created_at
    finally:
        session.close()


# message_types that count as PROACTIVE (coach-initiated) for anti-stack. The
# heartbeat is the only proactive system during burn-in; legacy briefings are
# deliberately excluded so a legacy outbound can never wedge the heartbeat silent
# (Item 1 point 4). Reactive replies (freeform/meal/workout/…) are never here.
PROACTIVE_MESSAGE_TYPES = {"heartbeat"}


def has_unanswered_proactive(user_id: int, window_minutes: int) -> bool:
    """Anti-STACK gate for the heartbeat: return True only when the MOST RECENT
    outbound is a proactive nudge that is (a) still unanswered and (b) within
    `window_minutes` of now — i.e. sending again would stack a second unanswered
    proactive message on top of a fresh first.

    Distinct from `has_unanswered_outbound` (the legacy scheduler's type-blind,
    stay-mute-until-spoken-to gate). Here the clear condition is TIME-ELAPSE: past
    the window a fresh opening is allowed with no user reply required — that is the
    fix for the unanswered_gap deadlock. A reactive reply or a legacy briefing as
    the most-recent outbound never blocks (they aren't PROACTIVE_MESSAGE_TYPES), so
    the coach answering the user can't mute its own initiation."""
    session = get_session()
    try:
        last_out = (
            session.query(Message)
            .filter(Message.user_id == user_id, Message.direction == "out")
            .order_by(Message.created_at.desc())
            .first()
        )
        if not last_out or last_out.message_type not in PROACTIVE_MESSAGE_TYPES:
            return False
        # answered? any inbound after this proactive outbound clears the stack
        reply = (
            session.query(Message)
            .filter(Message.user_id == user_id, Message.direction == "in",
                    Message.created_at > last_out.created_at)
            .first()
        )
        if reply:
            return False
        # within the anti-stack window? (naive-UTC column; mirror heartbeat's convention)
        age_min = ((datetime.now(timezone.utc)
                    - last_out.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 60)
        return age_min < window_minutes
    finally:
        session.close()


# Which touchpoints are allowed per tier
TIER_TOUCHPOINTS = {
    "FULL":    {"morning_briefing", "breakfast", "meal_suggestion", "pre_workout",
                "workout_request", "post_workout", "evening_wrap"},
    "QUIET":   {"morning_briefing", "breakfast", "meal_suggestion", "pre_workout",
                "workout_request", "evening_wrap"},  # post_workout dropped
    "MINIMAL": {"morning_briefing", "meal_suggestion", "evening_wrap"},
    "NUDGE":   {"nudge"},  # custom single daily message
}

# Scheduler job names that map to touchpoint names (from scheduler.py)
SCHEDULER_TO_TOUCHPOINT = {
    "morning":      "morning_briefing",
    "breakfast":    "breakfast",
    "lunch":        "meal_suggestion",
    "pre_workout":  "pre_workout",
    "post_workout": "post_workout",
    "dinner":       "meal_suggestion",
    "evening":      "evening_wrap",
}


def get_tier(unanswered_count: int) -> str:
    if unanswered_count >= 6:
        return "NUDGE"
    elif unanswered_count >= 4:
        return "MINIMAL"
    elif unanswered_count >= 2:
        return "QUIET"
    return "FULL"


def should_send(user: User, message_type: str) -> bool:
    """Return True if this message_type is allowed to fire for this user's current tier."""
    tier = get_tier(user.unanswered_count or 0)
    touchpoint = SCHEDULER_TO_TOUCHPOINT.get(message_type, message_type)
    return touchpoint in TIER_TOUCHPOINTS[tier]


def is_question_type(message_type: str) -> bool:
    """Return True if this message type involves asking the user something."""
    return message_type in ("post_workout", "evening_wrap", "freeform")


def increment_unanswered(user_id: int):
    """Increment unanswered_count if no reply came in since the last outbound message."""
    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user:
            return

        # Find the last outbound message
        last_out = (
            session.query(Message)
            .filter(Message.user_id == user_id, Message.direction == "out")
            .order_by(Message.created_at.desc())
            .first()
        )
        if not last_out:
            return

        # Check if any inbound reply came after it
        reply = (
            session.query(Message)
            .filter(
                Message.user_id == user_id,
                Message.direction == "in",
                Message.created_at > last_out.created_at,
            )
            .first()
        )

        if not reply:
            user.unanswered_count = (user.unanswered_count or 0) + 1
            session.commit()

    finally:
        session.close()


def reset_unanswered(user_id: int):
    """Reset unanswered_count to 0 when user sends any reply."""
    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if user and (user.unanswered_count or 0) > 0:
            user.unanswered_count = 0
            session.commit()
    finally:
        session.close()
