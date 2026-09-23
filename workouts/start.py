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
from workouts.templates import normalize_template_key, TEMPLATES, day_label, day_template
from workouts.session_ops import active_session_id

logger = logging.getLogger("cued.workouts")


NO_SPLIT = {None, "", "none"}


def infer_template(user) -> str | None:
    """Named day → itself. Else the day AFTER the split pointer in the user's cycle;
    no pointer → the cycle's first day. No split at all (never trained / asked us to
    build one) → full_body, the starting program. A split we can't map (a "custom"
    routine with no days, an unknown label) → None: the caller tells the model, who
    asks what days they run. Live 2026-09-22 (user 43): a stated bro split silently
    became a full-body card."""
    from split_pointer import cycle_for
    cycle = [k for k in (normalize_template_key(d) for d in cycle_for(user)) if k]
    if not cycle:
        split = (user.current_split or user.confirmed_training_split or "").strip().lower()
        return "full_body" if split in NO_SPLIT else None
    last = user.split_pointer_day
    if last in cycle:
        return cycle[(cycle.index(last) + 1) % len(cycle)]
    return cycle[0]


def _fmt(w) -> str:
    return f"{float(w):g}"


def intro_line(ws_state_exercises: list, key: str, *, first: bool = False, estimated: bool = False) -> str:
    """The one text before the card. `first` = their first card ever: there is no
    "usual" yet, so say where the numbers came from and point at the edit affordance.
    `estimated` = nothing known about their lifts, the loads are from their stats."""
    lead = ws_state_exercises[0] if ws_state_exercises else None
    if first and lead and lead["sets"] and (lead["sets"][0].get("planned_weight") or 0):
        w = float(lead["sets"][0]["planned_weight"])
        src = ("first card, so the weights are my best guess from ur stats" if estimated
               else "first card, weights are off what u told me")
        return (f"{day_label(key)} day. starting u at {_fmt(w)} on {lead['label']} — {src}. "
                f"tap a set and change the number if it's off, i'll remember.")
    lead_txt = f"{len(lead['sets'])} sets {lead['label']}" if lead else key
    return f"{day_label(key)} day. {lead_txt}, then the usual. tap as you go — text me if a set goes different."


def _is_first_card(session, user_id: int) -> bool:
    """No session with a completed set yet — a planned-and-abandoned card doesn't count."""
    from models import SetLog
    q = (session.query(SetLog.id).join(WorkoutSession, SetLog.session_id == WorkoutSession.id)
         .filter(WorkoutSession.user_id == user_id, SetLog.done.is_(True)))
    return not session.query(q.exists()).scalar()


class NeedsAnchors(ValueError):
    """First loaded card for someone who trains: ask what they lift before guessing."""


def _wants_anchor_ask(session, user, key: str) -> bool:
    """True when this would be the user's FIRST loaded card and they've told us
    nothing about what they lift. Everyone gets asked — a beginner too (founder,
    2026-09-23: "some kind of anchor weight is still helpful even for complete
    beginners"); only the WORDING differs (see _anchor_ask_text). A bodyweight day
    has nothing to anchor."""
    from workouts.calibrate import has_any_lift_evidence
    if not any(t.default_weight for t in day_template(user, key)):
        return False
    return not has_any_lift_evidence(session, user)


def _anchor_ask_text(user) -> str:
    """The tool's instruction for the ask, by level. Trained: their working numbers.
    New: ANY number they have — the heaviest they've benched / squatted / leg-pressed,
    even the empty bar counts — and an easy out."""
    lvl = (user.experience or "").strip().lower()
    if lvl in ("intermediate", "advanced"):
        what = "what they bench and squat for ~5 (or the main lifts of today's day)"
    else:
        what = ("whether they have ANY number — the heaviest they've benched, squatted or leg-pressed, "
                "even just the empty bar (45), or a dumbbell they've pressed — friend tone, not a form, "
                "and make 'no clue' an easy answer")
    return (f"first card and nothing is known about what they lift — ask in ONE line {what}, then "
            f"set_lift_anchors with what they say (the card goes out on its own) — or, if they don't "
            f"know / say just start light, call start_workout_session with no_anchors=true")


def start_workout_session(user_id: int, template_key: str | None = None, *, no_anchors: bool = False) -> dict:
    """→ {"session_id", "template_key", "surface": "card"|"messages", "sets", "first"}.
    Raises ValueError on an unknown template or an already-open session, and
    NeedsAnchors (a ValueError) on a trained user's first loaded card when nothing
    is known about their lifts — unless `no_anchors` (they don't know / said start light)."""
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
            if template_key:
                raise ValueError(f"unknown template {template_key!r}")
            raise ValueError("their split isn't mapped to days yet — ask which days they run "
                             "(e.g. chest+bis / back+tris / legs+shoulders) and save it with "
                             "save_routine(split_days=[...]), or have them name today's day")
        from workouts.calibrate import has_any_lift_evidence
        first = _is_first_card(session, user_id)
        estimated = first and not has_any_lift_evidence(session, user)
        if estimated and not no_anchors and _wants_anchor_ask(session, user, key):
            logger.info("WORKOUT_SESSION_NEEDS_ANCHORS user=%s key=%s", user_id, key)
            from workouts.calibrate import set_pending_card
            set_pending_card(user_id, key)      # the answer (set_lift_anchors) sends this day's card in code
            raise NeedsAnchors(_anchor_ask_text(user))
        phone = user.phone
    finally:
        session.close()

    from workouts.calibrate import pop_pending_card
    pop_pending_card(user_id)                   # a card is going out; any parked ask is moot
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
        send_sms(phone, intro_line(state["exercises"], key, first=first, estimated=estimated),
                 user_id=user_id, message_type="workout_intro")
        try:
            send_workout_card(ws.id)
            surface = "card"
        except CardError as e:
            logger.warning("WORKOUT_CARD_REFUSED user=%s session=%s err=%s — per-exercise messages instead", user_id, ws.id, e)
    if surface == "messages":
        _send_exercise_messages(user_id, phone, ws.id, state, intro=(_resolve_channel(user_id) != "imessage"),
                                first=first, estimated=estimated)
    logger.info("WORKOUT_SESSION_STARTED user=%s session=%s template=%s surface=%s sets=%s first=%s estimated=%s",
                user_id, ws.id, key, surface, state["set_count"], first, estimated)
    return {"session_id": ws.id, "template_key": key, "surface": surface, "sets": state["set_count"],
            "first": first, "estimated": estimated}


def _send_exercise_messages(user_id: int, phone: str, session_id: int, state: dict, *, intro: bool,
                            first: bool = False, estimated: bool = False) -> None:
    """Phase 5: one message per exercise; its provider id lands on that exercise's sets."""
    if intro:
        send_sms(phone, intro_line(state["exercises"], state["session"]["template_key"], first=first, estimated=estimated)
                 + " 👍 an exercise when it's done as planned.",
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
