"""
Gym beats (series §2.5, §2.7). A module PROPOSES; code decides under the usual
guardrails (quiet hours, active conversation, stack, daily cap via the heartbeat's
guardrail_reason) plus: at most one gym beat per user per day, only during RSF
hours, only with a planned session. Nothing here sends on its own outside the
scheduler sweep.

  dead/light + planned today + not trained yet  → 'gym's dead right now. quick <template>?'
  line_on   + planned in the next 2h           → opt-in on: queue.join → D2 numbers
                                                  opt-in off/never asked: the 2.7 ask (once)
                                                  join fails for ANY reason: D1 (the link)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config
from models import get_session, User, Message, WorkoutSession, is_workout_confirmed_today
from sms import send_sms
import occupancy
from integrations.rsf import is_open, TZ
from integrations.waitwell.client import join as queue_join, QueueUnavailable, PUBLIC_URL, open_ticket

logger = logging.getLogger("cued.gym_beats")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_WALK_MIN = 10


@dataclass
class Beat:
    kind: str            # dead | line_d1 | line_d2 | optin_ask
    text: str
    message_type: str


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local(user, at=None):
    tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
    at = at or datetime.now(timezone.utc)
    return (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(tz)


def planned_today(user, session, now_local) -> bool:
    """A training day: an open/planned session today, or today's weekday in their
    confirmed days. Unknown days ('4-5') → not planned (never fake it)."""
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    if session.query(WorkoutSession.id).filter(WorkoutSession.user_id == user.id, WorkoutSession.date >= day_start,
                                                WorkoutSession.status.in_(("planned", "active"))).first():
        return True
    days = (user.confirmed_training_days or "").lower()
    return DAYS[now_local.weekday()] in [d.strip()[:3] for d in days.split(",") if d.strip()]


def session_within(user, hours: float, now_local) -> bool:
    """Their usual lift time is within the next `hours` (and not passed)."""
    t = user.confirmed_workout_time or user.workout_time
    if not t or not re.match(r"^\d{1,2}:\d{2}$", t):
        return False
    h, m = map(int, t.split(":"))
    planned = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    return timedelta(0) <= (planned - now_local) <= timedelta(hours=hours)


def gym_beat_sent_today(session, user, now_local) -> bool:
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    return session.query(Message.id).filter(Message.user_id == user.id, Message.direction == "out",
                                            Message.created_at >= day_start,
                                            Message.message_type.like("gym_%")).first() is not None


def optin_asked_ever(session, user) -> bool:
    return session.query(Message.id).filter(Message.user_id == user.id, Message.direction == "out",
                                            Message.message_type == "gym_optin_ask").first() is not None


def walk_min_for(user, session) -> int | None:
    """§3: the last location signal if < 20 min old, else None (caller assumes 10)."""
    from models import Signal
    since = _utcnow() - timedelta(minutes=20)
    row = (session.query(Signal).filter(Signal.user_id == user.id, Signal.kind == "location", Signal.ts >= since)
           .order_by(Signal.ts.desc()).first())
    if row and isinstance(row.payload, dict) and row.payload.get("walk_min_to_rsf") is not None:
        return int(row.payload["walk_min_to_rsf"])
    return None


def _template_for(user) -> str:
    from workouts.start import infer_template
    return infer_template(user).replace("_", " ")


def d1_text(reading: dict, walk_min: int | None) -> str:
    pct = reading["pct"]
    if walk_min is not None:
        return (f"rsf is at {pct}%, line's on. join now → {PUBLIC_URL} — you're ~{walk_min} min out so "
                f"you'll clear it about when you get there.")
    return f"rsf is at {pct}%, line's on. join now → {PUBLIC_URL} — leave in about 10 min and you'll clear it around when you get there."


def d2_text(wait_min: int, now_local, walk_min: int | None) -> str:
    up = now_local + timedelta(minutes=wait_min)
    if walk_min is not None:
        leave = up - timedelta(minutes=walk_min)
        return (f"line at rsf is {wait_min} min. put you in the virtual queue — you're up at "
                f"{up.strftime('%-I:%M')}, leave by {leave.strftime('%-I:%M')}.")
    leave_in = max(wait_min - DEFAULT_WALK_MIN, 0)
    return (f"line at rsf is {wait_min} min. put you in the virtual queue — you're up at "
            f"{up.strftime('%-I:%M')}, leave in about {leave_in} min.")


OPTIN_ASK = ("rsf line's on. want me to handle the queue for you from now on when it's packed? "
             "i'll use your number so their texts come to you.")


def propose(user, session, now_utc: datetime | None = None) -> Beat | None:
    """Pure decision (no sends) except queue_join, which is the D2 action itself."""
    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = _local(user, now_utc)
    if not is_open(now_local):
        return None
    reading = occupancy.now(now_utc.replace(tzinfo=None))
    if not reading:
        return None
    if gym_beat_sent_today(session, user, now_local):
        return None
    if not planned_today(user, session, now_local) or is_workout_confirmed_today(user.id):
        return None
    walk = walk_min_for(user, session)

    if reading["line_on"] and session_within(user, 2.0, now_local):
        if user.queue_opt_in:
            if open_ticket(user.id):
                return None
            try:
                t = queue_join(user.id, (user.name or "").split(" ")[0] or "cued", user.phone)
                wait = t.est_wait_min if t.est_wait_min is not None else (reading.get("est_wait_min") or 25)
                return Beat("line_d2", d2_text(int(wait), now_local, walk), "gym_line_d2")
            except QueueUnavailable as e:
                logger.info("GYM_BEAT_D1_FALLBACK user=%s reason=%s", user.id, e)
                return Beat("line_d1", d1_text(reading, walk), "gym_line_d1")
        if not optin_asked_ever(session, user):
            return Beat("optin_ask", OPTIN_ASK, "gym_optin_ask")
        return Beat("line_d1", d1_text(reading, walk), "gym_line_d1")

    if reading["label"] in ("dead", "light"):
        return Beat("dead", f"gym's dead right now. quick {_template_for(user)}?", "gym_dead")
    return None


def sweep(now_utc: datetime | None = None) -> int:
    """Scheduler: every 15 min during RSF hours. Guardrails in code; then send."""
    if not config.RSF_BEATS_ENABLED:
        return 0
    from heartbeat import guardrail_reason
    sent = 0
    session = get_session()
    try:
        users = session.query(User).filter(User.onboarding_step >= 3).all()
        for user in users:
            try:
                reason = guardrail_reason(user, session)
                if reason:
                    continue
                beat = propose(user, session, now_utc)
                if not beat:
                    continue
                send_sms(user.phone, beat.text, user_id=user.id, message_type=beat.message_type)
                sent += 1
                logger.info("GYM_BEAT_SENT user=%s kind=%s", user.id, beat.kind)
            except Exception as e:  # noqa: BLE001 — one user never stops the sweep
                logger.error("GYM_BEAT_FAILED user=%s err=%s", user.id, e, exc_info=True)
    finally:
        session.close()
    return sent


# ─── replies to the opt-in ask, 'handle the line', 'not going' ──────────────

YES_RE = re.compile(r"^\s*(yes|yeah|yea|yep|ya|sure|ok|okay|bet|do it|go for it|please|yes please|handle it)\W*$", re.I)
NO_RE = re.compile(r"^\s*(no|nah|nope|no thanks|i'?m good|im good|not now|don'?t)\W*$", re.I)
HANDLE_RE = re.compile(r"handle the (?:line|queue) for me|put me in (?:the )?(?:line|queue) from now on|always (?:join|handle) the (?:line|queue)", re.I)
STOP_RE = re.compile(r"stop (?:handling|joining) the (?:line|queue)|don'?t (?:handle|join) the (?:line|queue)", re.I)
NOT_GOING_RE = re.compile(r"^\s*(not going|skip it|skipping|can'?t make it|not gonna make it|leave the (?:line|queue))\W*$", re.I)


def handle_text(user_id: int, text: str) -> str | None:
    """Deterministic replies. Returns the one line, or None (normal turn)."""
    if not config.RSF_BEATS_ENABLED or not text or len(text) > 80:
        return None
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return None
        if HANDLE_RE.search(text):
            user.queue_opt_in = True; session.commit()
            logger.info("QUEUE_OPT_IN user=%s via=text", user_id)
            return "bet — when rsf's packed and you've got a session coming up, i'll put you in the line and text you the leave-by."
        if STOP_RE.search(text):
            user.queue_opt_in = False; session.commit()
            return "ok, i'll just send you the link when the line's on."
        last = (session.query(Message).filter(Message.user_id == user_id, Message.direction == "out")
                .order_by(Message.id.desc()).first())
        if last and last.message_type == "gym_optin_ask":
            if YES_RE.match(text):
                user.queue_opt_in = True; session.commit()
                logger.info("QUEUE_OPT_IN user=%s via=ask", user_id)
                return "bet — i'll handle it from now on. joining you now."
            if NO_RE.match(text):
                return f"no stress — here's the link when you want it: {PUBLIC_URL}"
        if NOT_GOING_RE.match(text):
            t = open_ticket(user_id)
            if t:
                from integrations.waitwell.client import leave
                leave(t.ticket_id)
                return "ok, dropped you from the line — ignore their texts."
    finally:
        session.close()
    return None


def poll_open_tickets() -> int:
    """Scheduler: every 60s while any ticket is open (stop on summon / after 90 min)."""
    from integrations.waitwell.client import status, mark_summoned, leave
    from models import QueueTicket
    n = 0
    session = get_session()
    try:
        tickets = session.query(QueueTicket).filter(QueueTicket.status == "open").all()
        rows = [(t.id, t.user_id, t.ticket_id, t.joined_at) for t in tickets]
    finally:
        session.close()
    for _id, uid, tid, joined in rows:
        if joined and _utcnow() - joined > timedelta(minutes=90):
            leave(tid)
            continue
        try:
            st = status(tid)
        except QueueUnavailable:
            continue
        if st.get("summoned"):
            mark_summoned(tid)
            session = get_session()
            try:
                user = session.get(User, uid); phone = user.phone
            finally:
                session.close()
            send_sms(phone, "you're up at rsf. 10 min to get to the weight room door.", user_id=uid, message_type="gym_summon")
            n += 1
    return n
