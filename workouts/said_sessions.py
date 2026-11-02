"""
A session they only SAID — "hitting the gym now, just doing upper", card declined, no sets —
becomes a `workouts` row once it has clearly happened, so the next briefings count it.

Live 2026-10-30 17:18 PT (user 51): "Hitting the gym now, just doing upper" → "want me to send
a card" → "Nah I got it". The inbound path marked the day confirmed and set session_state
at_gym, but nothing wrote a Workout row and the split pointer never moved; the next three
briefings read the `workouts` table and said "pull's next, been 6 days / a week since push /
pull's been 8 days". On Monday the coach conceded "friday was the upper day" in text and
still wrote nothing.

The sweep (every SAID_SESSION_SWEEP_MINUTES): an at_gym state that carries what they said,
older than SAID_SESSION_SETTLE_MIN, with no Workout row and no card session on that local
day, is written as a completed session with no sets ("said they were at the gym"). A named
day in their own cycle moves the split pointer (confirmed); an off-cycle name ("upper" on a
PPL) or no name leaves the pointer alone — the row is the record, the pointer is a guess we
don't make. Said-for-later ("gym at 6", "later", "tonight") never settles: the time is a
plan, not a session.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta

import config
from models import get_session, User, Workout, WorkoutSession, active, clear_session_state

logger = logging.getLogger("cued.said_sessions")

# A clock time or a later-today word: a plan for later, not "I'm there now".
_LATER_RE = re.compile(
    r"(?:\b(?:at|around|by|till|until)\s+(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|noon|midnight)\b)"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b"
    r"|\b(?:later|tonight|this (?:evening|afternoon)|after (?:class|work|lecture|lab|dinner|lunch|data|practice|school)"
    r"|in (?:a|an|\d+)\s*(?:min|mins|minutes|hr|hrs|hour|hours|bit)|tmrw|tomorrow)\b",
    re.I,
)


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def said_for_later(text: str) -> bool:
    return bool(_LATER_RE.search(text or ""))


def _local_day_bounds(user, at_naive_utc: datetime):
    from timefmt import local_day_bounds
    return local_day_bounds(user, now=at_naive_utc.replace(tzinfo=timezone.utc))


def settle_said_sessions(now: datetime | None = None) -> int:
    """Write the row for every settled said-session. Returns the number written."""
    if not getattr(config, "SAID_SESSION_SETTLE_ENABLED", True):
        return 0
    now = now or _naive_utcnow()
    settle = timedelta(minutes=int(getattr(config, "SAID_SESSION_SETTLE_MIN", 90)))
    session = get_session()
    try:
        users = session.query(User).filter(User.session_state.isnot(None), User.active.is_(True)).all()
        todo = []
        for u in users:
            st = u.session_state or {}
            said = (st.get("said") or "").strip()
            if st.get("status") != "at_gym" or not said or st.get("settled"):
                continue
            try:
                started = datetime.fromisoformat(st["started_at"])
            except (KeyError, ValueError, TypeError):
                continue
            started = started.astimezone(timezone.utc).replace(tzinfo=None) if started.tzinfo else started
            if now - started < settle:
                continue
            todo.append((u.id, said, started))
    finally:
        session.close()

    written = 0
    for uid, said, started in todo:
        if _settle_one(uid, said, started, now):
            written += 1
    return written


def _mark(uid: int, how: str):
    session = get_session()
    try:
        u = session.get(User, uid)
        if u and isinstance(u.session_state, dict):
            from sqlalchemy.orm.attributes import flag_modified
            u.session_state = dict(u.session_state, settled=how)
            flag_modified(u, "session_state")
            session.commit()
    finally:
        session.close()


def _settle_one(uid: int, said: str, started: datetime, now: datetime) -> bool:
    if said_for_later(said):
        logger.info("SAID_SESSION_SKIPPED user=%s reason=said_for_later said=%r", uid, said[:80])
        _mark(uid, "later")
        return False
    session = get_session()
    try:
        user = session.get(User, uid)
        if not user:
            return False
        d0, d1 = _local_day_bounds(user, started)
        has_row = active(session, Workout, uid).filter(Workout.date >= d0, Workout.date < d1).first()
        if has_row:
            _mark(uid, "already_logged")
            return False
        card = (session.query(WorkoutSession.id)
                .filter(WorkoutSession.user_id == uid, WorkoutSession.date >= d0, WorkoutSession.date < d1,
                        WorkoutSession.status.in_(("active", "done"))).first())
        if card:
            logger.info("SAID_SESSION_SKIPPED user=%s reason=card_session said=%r", uid, said[:80])
            _mark(uid, "card")
            return False
        from split_pointer import parse_named_split_day, cycle_for
        named = parse_named_split_day(said)
        cycle = cycle_for(user)
        in_cycle = bool(named and named in cycle)
        w = Workout(user_id=uid, workout_type=named or "logged", exercises=[], completed=True, date=started,
                    user_notes=f"said they were at the gym ({said[:120]!r}), no sets given")
        session.add(w)
        session.commit()
        wid = w.id
    finally:
        session.close()

    pointer = None
    if in_cycle:
        from split_pointer import advance_split_pointer, get_split_pointer
        before = get_split_pointer(uid)
        pointer = advance_split_pointer(uid, named_day=named, at=started)
        if pointer and pointer != before:
            from agent_tools import _record_pointer_advance
            _record_pointer_advance(wid, before, pointer)
    clear_session_state(uid)
    logger.info("SAID_SESSION_SETTLED user=%s workout_id=%s day=%s in_cycle=%s pointer=%s said=%r",
                uid, wid, named, in_cycle, pointer, said[:80])
    return True
