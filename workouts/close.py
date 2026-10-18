"""Closing a session: the summary text (the site card as a message) + the honest
sent through send_sms (routes blue/green like any message)."""

from __future__ import annotations

import logging

import config
from models import get_session, User, WorkoutSession
from sms import send_sms
from workouts.summary import summarize, format_summary

logger = logging.getLogger("cued.workouts")


def session_summary_text(session_id: int) -> str | None:
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        if not ws:
            return None
        s = summarize(session, ws)
    finally:
        session.close()
    if s["sets_done"] == 0:
        return None
    # No closer line: the summary is the card + the total, nothing after (the
    # "no app opened" pitch was marketing copy inside the product — voice rewrite).
    return format_summary(s)


# One line after the summary the FIRST time they ever close a session. Code-sent: the
# summary is code-only and nothing warm followed user 40's first workout (2026-10-18 00:53)
# until he asked "so how'd i do". Plain, no promise about a schedule.
FIRST_WORKOUT_LINE = "first one in the books. showing up was the hard part, the rest is just reps"


def send_first_workout_line(session_id: int) -> bool:
    """After the summary: if this is the user's first DONE session, send FIRST_WORKOUT_LINE."""
    if not getattr(config, "FIRST_WORKOUT_LINE_ENABLED", True):
        return False
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        if not ws or ws.status != "done":
            return False
        n_done = session.query(WorkoutSession.id).filter(WorkoutSession.user_id == ws.user_id,
                                                        WorkoutSession.status == "done").count()
        user = session.get(User, ws.user_id)
        if n_done != 1 or not user:
            return False
        phone, user_id = user.phone, user.id
    finally:
        session.close()
    send_sms(phone, FIRST_WORKOUT_LINE, user_id=user_id, message_type="workout_summary")
    logger.info("WORKOUT_FIRST_LINE_SENT user=%s session=%s", user_id, session_id)
    return True


def send_session_summary(session_id: int) -> bool:
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        user = session.get(User, ws.user_id) if ws else None
        if not ws or not user:
            return False
        phone, user_id = user.phone, user.id
    finally:
        session.close()
    text = session_summary_text(session_id)
    if not text:
        logger.info("WORKOUT_SUMMARY_SKIPPED user=%s session=%s reason=no_sets_done", user_id, session_id)
        return False
    send_sms(phone, text, user_id=user_id, message_type="workout_summary")
    logger.info("WORKOUT_SUMMARY_SENT user=%s session=%s", user_id, session_id)
    return True
