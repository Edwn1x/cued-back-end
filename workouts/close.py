"""Closing a session: the summary text (the site card as a message) + the honest
sent through send_sms (routes blue/green like any message)."""

from __future__ import annotations

import logging

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
