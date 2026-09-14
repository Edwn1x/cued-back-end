"""
The card in the thread — sending it, and refreshing its bubble captions in place.

Design (founder, 2026-09-14, after the live-card test): the bubble is a static
preview (caption / subcaption / trailing caption) that we EDIT as the session
moves; tapping it opens the full logger in the Spectrum extension's sheet (the
overlay). No live WebView in the bubble → no scroll fight, no clipping.
The page itself lives on the site (cued.fit/card.html?t=…), like profile.html.
"""

from __future__ import annotations

import logging
import threading

from models import get_session, User, WorkoutSession, Message
from card_page import card_url, build_state

logger = logging.getLogger("cued.workouts")

TEMPLATE_INTRO = {
    "push": "bench, then the usual",
    "pull": "deadlift, then the usual",
    "legs": "squat, then the usual",
    "upper": "bench and rows, then the usual",
    "lower": "squat, then the usual",
    "full_body": "squat, bench, rows",
}


def card_layout(state: dict) -> dict:
    """What the bubble says. Caption + subcaption only: on the phone the trailing
    caption rendered glued to the title ('push · mon7,085 lb'), so progress lives
    in the subcaption. Always at least a caption (the SDK refuses an empty layout)."""
    s = state["session"]
    key = (s["template_key"] or "workout").replace("_", " ")
    caption = f"{key} · {s['weekday']}"
    done, total, vol = state["done_count"], state["set_count"], state["volume_lb"]
    if s["status"] == "done":
        sub = f"done · {vol:,} lb — tap for the log"
    elif done:
        sub = f"{done}/{total} sets · {vol:,} lb — tap to log"
    else:
        lead = next((e for e in state["exercises"]), None)
        first = f"{len(lead['sets'])} sets {lead['label']}" if lead else key
        sub = f"{first}, then the usual — tap to start"
    return {"caption": caption, "subcaption": sub, "summary": f"{key} day"}


def _version(ws: WorkoutSession) -> int:
    import time
    return int(time.time())


def send_workout_card(session_id: int) -> dict:
    """Send the session as a card (static layout, tap → overlay). Stores
    card_session + card_message_id on the session and logs the outbound
    Message row (channel imessage, message_type workout_card). Raises CardError
    on refusal so the caller can fall over (Phase 5)."""
    from photon_cards import send_card
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        if not ws:
            raise ValueError("no such session")
        user = session.get(User, ws.user_id)
        state = build_state(session, ws)
        phone, user_id = user.phone, user.id
        url = card_url(user_id, ws.id, version=_version(ws))
        layout = card_layout(state)
    finally:
        session.close()
    r = send_card(phone, url, live=False, layout=layout)
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        ws.card_session = r.get("card_session")
        ws.card_message_id = r.get("provider_message_id")
        session.add(Message(user_id=user_id, direction="out", body=f"[workout card: {layout['caption']}]",
                            message_type="workout_card", channel="imessage",
                            provider_sid=r.get("provider_message_id"), delivery_status="sent"))
        session.commit()
    finally:
        session.close()
    logger.info("WORKOUT_CARD_SENT user=%s session=%s id=%s", user_id, session_id, r.get("provider_message_id"))
    return r


def refresh_card(session_id: int) -> bool:
    """Edit the bubble's captions in place to match the session. Best-effort:
    a failure is logged and never trips the breaker (the card is a convenience;
    the rows are the truth)."""
    from photon_cards import update_card, CardError
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        if not ws or not ws.card_session:
            return False
        user = session.get(User, ws.user_id)
        state = build_state(session, ws)
        phone, cs, uid = user.phone, dict(ws.card_session), user.id
        url = card_url(uid, ws.id, version=_version(ws))
        layout = card_layout(state)
    finally:
        session.close()
    try:
        update_card(phone, cs, url, live=False, layout=layout)
        logger.info("WORKOUT_CARD_REFRESHED user=%s session=%s sub=%s", uid, session_id, layout["subcaption"])
        return True
    except CardError as e:
        logger.warning("WORKOUT_CARD_REFRESH_FAILED user=%s session=%s err=%s", uid, session_id, e)
        return False


def refresh_card_async(session_id: int) -> None:
    threading.Thread(target=refresh_card, args=(session_id,), daemon=True).start()
