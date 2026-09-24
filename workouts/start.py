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
import re

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


def _when(workout_time) -> str:
    """'18:00' → '6pm', '18:30' → '6:30pm'; a description ('afternoon') as-is; else ''."""
    t = (workout_time or "").strip()
    if not t:
        return ""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m:
        return t
    h, mm = int(m.group(1)), int(m.group(2))
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}" if mm == 0 else f"{h12}:{mm:02d}{suffix}"


def intro_line(ws_state_exercises: list, key: str, *, first: bool = False, estimated: bool = False,
               setup: bool = False, workout_time=None) -> str:
    """The one text before the card. `first` = their first card ever: there is no
    "usual" yet, so say where the numbers came from and point at the edit affordance.
    `estimated` = nothing known about their lifts, the loads are from their stats.
    `setup` = the onboarding setup step: they're at home, not at the gym — the card is
    to open now (install the extension, check the numbers) so they're set for later."""
    lead = ws_state_exercises[0] if ws_state_exercises else None
    loaded = bool(lead and lead["sets"] and (lead["sets"][0].get("planned_weight") or 0))
    if setup:
        when = _when(workout_time)
        later = f" so ur set for {when}" if when else " so ur set for when u lift"
        if loaded:
            w = float(lead["sets"][0]["planned_weight"])
            src = ("weights are my best guess from ur stats" if estimated else "weights are off what u told me")
            return (f"here's ur first card, {day_label(key)} day. starting u at {_fmt(w)} on {lead['label']} — {src}. "
                    f"tap it now{later}. fix any number that's off, i'll remember.")
        return f"here's ur first card, {day_label(key)} day. tap it now{later}."
    if first and loaded:
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


def _untouched(session, session_id: int) -> bool:
    """No completed set on the session — a card they only looked at (the onboarding
    setup card, a day they never started). Not something to refuse a new session over,
    and its bubble is worth reusing rather than stacking a second one."""
    return session.query(SetLog.id).filter(SetLog.session_id == session_id, SetLog.done.is_(True)).first() is None


def start_workout_session(user_id: int, template_key: str | None = None, *, no_anchors: bool = False,
                          setup: bool = False) -> dict:
    """→ {"session_id", "template_key", "surface": "card"|"messages"|"refused", "sets", "first", "setup"}.
    Raises ValueError on an unknown template or an already-open session, and
    NeedsAnchors (a ValueError) on a trained user's first loaded card when nothing
    is known about their lifts — unless `no_anchors` (they don't know / said start light).
    `setup` = the onboarding setup step (workouts/card_setup.py): 'tap it now' intro, no
    per-exercise fallback. An UNTOUCHED planned session (a card they only looked at) is
    retired and its bubble edited in place to the new day instead of refusing."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            raise ValueError("user not found")
        open_id = active_session_id(user_id)
        reuse_from = None
        if open_id:
            # Only a PLANNED (never started — the setup card) untouched session yields;
            # an active one is the normal guard even if untouched, else a double
            # "starting push" re-sends the whole day (13 texts on SMS).
            old = session.get(WorkoutSession, open_id)
            if old.status != "planned" or not _untouched(session, open_id):
                raise ValueError(f"a session is already open (#{open_id}) — finish or abandon it first")
            reuse_from = open_id
        else:
            # The newest session timed out untouched (the 6h abandon sweep) but its bubble
            # is still in the thread → edit that bubble to today's card instead.
            latest = (session.query(WorkoutSession).filter(WorkoutSession.user_id == user_id)
                      .order_by(WorkoutSession.id.desc()).first())
            if latest is not None and latest.status == "abandoned" and latest.card_session and _untouched(session, latest.id):
                reuse_from = latest.id
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
        # No second ask when they already answered it for the card being replaced.
        if estimated and not no_anchors and not reuse_from and _wants_anchor_ask(session, user, key):
            logger.info("WORKOUT_SESSION_NEEDS_ANCHORS user=%s key=%s setup=%s", user_id, key, setup)
            from workouts.calibrate import set_pending_card
            set_pending_card(user_id, key, setup=setup)   # the answer (set_lift_anchors) sends this day's card in code
            raise NeedsAnchors(_anchor_ask_text(user))
        if reuse_from:
            old = session.get(WorkoutSession, reuse_from)
            if old.status in ("planned", "active"):
                old.status = "abandoned"
                session.commit()
            logger.info("WORKOUT_SESSION_REPLANNED user=%s old=%s key=%s", user_id, reuse_from, key)
        phone, workout_time = user.phone, user.workout_time
    finally:
        session.close()

    from workouts.calibrate import pop_pending_card
    pop_pending_card(user_id)                   # a card is going out; any parked ask is moot
    ws = build_session(user, key)
    session = get_session()
    try:
        row = session.get(WorkoutSession, ws.id)
        from card_page import _utcnow
        if not setup:                       # the setup card is planned, not started — they're at home
            row.status = "active"
            row.started_at = _utcnow()
        session.commit()
        from card_page import build_state
        state = build_state(session, row)
    finally:
        session.close()

    surface = "messages"
    if _resolve_channel(user_id) == "imessage":
        from workouts.card import send_workout_card
        from workouts import card_setup
        from photon_cards import CardError
        # Extension framing (once in full, then a one-liner) until the card has ever been opened.
        card_setup.send_extension_intro_if_due(user_id, phone)
        send_sms(phone, intro_line(state["exercises"], key, first=first, estimated=estimated,
                                   setup=setup, workout_time=workout_time),
                 user_id=user_id, message_type="workout_intro")
        try:
            send_workout_card(ws.id, reuse_from=reuse_from)
            surface = "card"
        except CardError as e:
            logger.warning("WORKOUT_CARD_REFUSED user=%s session=%s err=%s — %s", user_id, ws.id, e,
                           "setup: one line, no exercise texts" if setup else "per-exercise messages instead")
        if surface == "card":
            card_setup.send_breakdown_if_due(user_id, phone)   # the tour, once ever
    if surface == "messages":
        if setup:
            from workouts import card_setup
            card_setup.setup_card_refused(user_id, phone, ws.id)
            surface = "refused"
        else:
            _send_exercise_messages(user_id, phone, ws.id, state, intro=(_resolve_channel(user_id) != "imessage"),
                                    first=first, estimated=estimated)
    logger.info("WORKOUT_SESSION_STARTED user=%s session=%s template=%s surface=%s sets=%s first=%s estimated=%s setup=%s reuse_from=%s",
                user_id, ws.id, key, surface, state["set_count"], first, estimated, setup, reuse_from)
    return {"session_id": ws.id, "template_key": key, "surface": surface, "sets": state["set_count"],
            "first": first, "estimated": estimated, "setup": setup}


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
