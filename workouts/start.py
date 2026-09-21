"""
start_workout_session — the coach's "push day" move (Phase 3) and its fallback
(Phase 5). Infers the template from the split pointer when not named, builds the
session, marks it active, then:
  iMessage user → ONE text ("push day. 4 sets bench, then the usual. tap as you
  go — text me if a set goes different.") + the card. Never a per-set prompt.
  SMS / breaker tripped / card refused → one message per exercise
  ("bench · 185 × 5 × 4"); each message's id is stored on the exercise's sets as
  provider_message_ref so a 👍 on it marks them done (Phase 5).
"""

from __future__ import annotations

import logging

from models import get_session, User, WorkoutSession, SetLog
from sms import send_sms, _resolve_channel
from workouts.plan import build_session
from workouts.templates import normalize_template_key, TEMPLATES
from workouts.session_ops import active_session_id

logger = logging.getLogger("cued.workouts")


def infer_template(user) -> str:
    """Named day → itself. Else the day AFTER the split pointer in the user's cycle;
    no pointer → the cycle's first day; no cycle → full_body."""
    from split_pointer import SPLIT_CYCLES, _normalize_system
    cycle = SPLIT_CYCLES.get(_normalize_system(user.current_split or user.confirmed_training_split or "")) or []
    if not cycle:
        return "full_body"
    last = user.split_pointer_day
    if last in cycle:
        nxt = cycle[(cycle.index(last) + 1) % len(cycle)]
    else:
        nxt = cycle[0]
    return normalize_template_key(nxt) or "full_body"


def _fmt(w) -> str:
    return f"{float(w):g}"


def intro_line(ws_state_exercises: list, key: str) -> str:
    lead = ws_state_exercises[0] if ws_state_exercises else None
    first = f"{len(lead['sets'])} sets {lead['label']}" if lead else key
    return f"{key.replace('_', ' ')} day. {first}, then the usual. tap as you go — text me if a set goes different."


def start_workout_session(user_id: int, template_key: str | None = None) -> dict:
    """→ {"session_id", "template_key", "surface": "card"|"messages", "sets"}.
    Raises ValueError on an unknown template or an already-open session."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            raise ValueError("user not found")
        open_id = active_session_id(user_id)
        if open_id:
            raise ValueError(f"a session is already open (#{open_id}) — finish or abandon it first")
        key = normalize_template_key(template_key) if template_key else infer_template(user)
        if not key:
            raise ValueError(f"unknown template {template_key!r}")
        phone = user.phone
    finally:
        session.close()

    ws = build_session(user, key)
    session = get_session()
    try:
        row = session.get(WorkoutSession, ws.id)
        row.status = "active"
        from card_page import _utcnow
        row.started_at = _utcnow()
        session.commit()
        from card_page import build_state
        state = build_state(session, row)
    finally:
        session.close()

    surface = "messages"
    if _resolve_channel(user_id) == "imessage":
        from workouts.card import send_workout_card
        from photon_cards import CardError
        send_sms(phone, intro_line(state["exercises"], key), user_id=user_id, message_type="workout_intro")
        try:
            send_workout_card(ws.id)
            surface = "card"
        except CardError as e:
            logger.warning("WORKOUT_CARD_REFUSED user=%s session=%s err=%s — per-exercise messages instead", user_id, ws.id, e)
    if surface == "messages":
        _send_exercise_messages(user_id, phone, ws.id, state, intro=(_resolve_channel(user_id) != "imessage"))
    logger.info("WORKOUT_SESSION_STARTED user=%s session=%s template=%s surface=%s sets=%s",
                user_id, ws.id, key, surface, state["set_count"])
    return {"session_id": ws.id, "template_key": key, "surface": surface, "sets": state["set_count"]}


def _send_exercise_messages(user_id: int, phone: str, session_id: int, state: dict, *, intro: bool) -> None:
    """Phase 5: one message per exercise; its provider id lands on that exercise's sets."""
    if intro:
        send_sms(phone, intro_line(state["exercises"], state["session"]["template_key"]) + " 👍 an exercise when it's done as planned.",
                 user_id=user_id, message_type="workout_intro")
    for ex in state["exercises"]:
        sets = ex["sets"]
        if not sets:
            continue
        w, r = sets[0]["planned_weight"], sets[0]["planned_reps"]
        # bodyweight: "pushup · 10 × 3" (reps × sets) — never "0 × 10 × 3"
        text = (f"{ex['label']} · {r} × {len(sets)}" if not (w or 0)
                else f"{ex['label']} · {_fmt(w)} × {r} × {len(sets)}")
        sid = send_sms(phone, text, user_id=user_id, message_type="workout_exercise")
        if sid:
            session = get_session()
            try:
                for s in session.query(SetLog).filter(SetLog.session_id == session_id, SetLog.exercise == ex["slug"]).all():
                    s.provider_message_ref = str(sid)
                session.commit()
            finally:
                session.close()
