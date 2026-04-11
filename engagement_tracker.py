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

from models import get_session, User, Message

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
