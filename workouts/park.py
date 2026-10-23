"""
A workout card goes out when they're going, not when they mention going.

Founder, 2026-10-22: "at 2:59pm when he was talking about his workout, the coach shouldn't
have sent the workout card unless he's actually at the gym ready to work out. it's annoying
and comes off as nagging if he just gets it when he's just letting us know."

Live (user 40, trains ~10:30pm): coach asked "wanna hit today? home or gym, ur call" →
"Gym. Tryouts got canceled" at 2:58pm → full card + two intro bubbles at 2:59pm, 7½ hours
before he'd touch it. Same on 2026-10-18: card at 6:31pm, first set at 11:55pm (the
session-in-progress guards exist because of that gap).

So, in code (the model's judgement here was the bug):
  • `hold_reason` — the start tool is called off a STATEMENT ("gym", "going tonight") when
    their usual start is hours away → the card is PARKED, not sent; the tool result says so
    and the coach just nods. An ASK ("what's my workout", "send the card") or an ARRIVAL
    ("omw", "at the gym", "about to start") always sends, whatever the clock says.
  • `release_parked_card` — the inbound path: a parked card + a whole-message arrival /
    ask line → code sends it, no model turn.
  • `context_line` — the PARKED block the coach sees, so it doesn't re-call the tool every
    turn or claim "ur card's ready".
The parked entry lives in lift_anchors["_parked_card"] (the pending-card store pattern) and
expires with the local day.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, time

import config

logger = logging.getLogger(__name__)

_KEY = "_parked_card"
PARKED_TTL_S = 14 * 3600

# An explicit ask for the card / the plan. Searched, not full-matched; sends whatever the clock says.
ASK_RE = re.compile(
    r"(?:\bcard\b|\bsend\b|\bgive me\b|\bgimme\b|\bpull up\b|\bworkout for\b"
    r"|\bwhat(?:'?s| is| are| do| should)\b[^?.!]{0,40}\b(?:workout|exercises|plan|lifts?|do today|today|tonight)\b)",
    re.I,
)
# "I'm going / I'm there / starting" — an arrival, unless a future marker makes it a plan
# ("going to the gym tonight", "ready for later").
ARRIVAL_RE = re.compile(
    r"(?:\bstart(?:ing)?\b|\bwarm(?:ing)? up\b|\blet'?s go\b|\bgym time\b|\bready\b"
    r"|\bat the (?:gym|rsf|weight ?room)\b|\b(?:i'?m|im) (?:here|at the|in the)\b|\bjust got (?:here|to the|in)\b"
    r"|\bmade it\b|\bpulling (?:up|in)\b|\bparking\b|\bwalking in\b"
    r"|\b(?:heading|headed|going|walking|bouta|boutta|bout to|about to|abt to|finna|gonna|gon|tryna|otw|omw|on my way|ima|imma|leaving)\b"
    r"[\w\s']{0,16}?\b(?:gym|rsf|weight ?room|lift|now|rn|there)\b"
    r"|\b(?:otw|omw|on my way|heading out|headed out|leaving now)\b)",
    re.I,
)
FUTURE_RE = re.compile(
    r"(?:\btonight\b|\blater\b|\bafter\b|\bthis (?:evening|afternoon|arvo)\b|\bat \d{1,2}(?::\d{2})?\s*(?:am|pm)?\b"
    r"|\baround \d|\bin (?:a (?:few|couple)|\d+) (?:hours?|hrs?)\b|\btmrw\b|\btomorrow\b|\bprobably\b|\bmaybe\b|\bplanning\b)",
    re.I,
)


def is_ask_or_arrival(text: str) -> bool:
    body = text or ""
    if ASK_RE.search(body):
        return True
    return bool(ARRIVAL_RE.search(body)) and not FUTURE_RE.search(body)


# The whole message is "I'm going / I'm there / send it" — short, nothing else to answer.
_ARRIVAL_LINE_RE = re.compile(
    r"^\W*(?:(?:yeah|yea|ya|yep|yup|ok|okay|alr|aight|bet|k|also|and)[,!.]?\s+)?"
    r"(?:(?:i'?m |im |just )?(?:at|in) the (?:gym|rsf|weight ?room)(?: now| rn)?"
    r"|(?:i'?m |im )?(?:here|there)(?: now| rn)?|(?:just )?(?:got|made it) (?:here|to the gym|in)"
    r"|(?:heading|headed|going|walking|bouta|boutta|bout to|about to|abt to|finna|gonna|gon|tryna|otw|omw|on my way|ima|imma|leaving)"
    r"[\w\s']{0,14}?(?:to |for |hit |go to |head to )?(?:the )?(?:gym|rsf|weight ?room|lift|now|rn)"
    r"|(?:omw|otw|on my way|heading out|headed out|leaving now|gym time|let'?s go|ready|starting(?: now)?|about to start|warming up)"
    r"|send (?:it|the card|my card|today'?s)|(?:what'?s|whats) (?:my|the|today'?s) (?:workout|card|plan)(?: today)?|card)"
    r"(?:\s+(?:now|rn|right now|soon|in a (?:bit|sec|min)|in \d+(?: ?min)?))?[\s.!?👍💪🔥]*$",
    re.I,
)

_PHRASE_HOURS = {"morning": 8, "mornings": 8, "noon": 12, "lunch": 12, "afternoon": 15, "afternoons": 15,
                 "evening": 18, "evenings": 18, "night": 21, "nights": 21, "late": 22}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def usual_start(user, now_local: datetime) -> time | None:
    """The clock time they usually train today: per-day slot → primary workout_time (HH:MM
    or a phrase like 'evenings') → None when we don't know."""
    from training_time import slot_for
    day = now_local.strftime("%a").lower()
    raw = (slot_for(user, day) or "").strip().lower()
    if not raw:
        return None
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", raw)
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
        if 0 <= h < 24 and 0 <= mm < 60:
            return time(h, mm)
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", raw)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(2) == "pm" else 0)
        return time(h, 0)
    for word, h in _PHRASE_HOURS.items():
        if re.search(rf"\b{word}\b", raw):
            return time(h, 0)
    return None


def hours_until_usual(user, now_local: datetime) -> float | None:
    """Hours from now to today's usual start (negative once it's passed); None if unknown."""
    t = usual_start(user, now_local)
    if t is None:
        return None
    target = now_local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
    return (target - now_local).total_seconds() / 3600.0


def hold_reason(user, text: str | None, *, now_utc: datetime | None = None) -> str | None:
    """Why the card should NOT go out right now, or None to send. Pure."""
    if not getattr(config, "CARD_EARLY_SEND_GATE_ENABLED", True):
        return None
    body = (text or "").strip()
    if not body:                                     # no inbound behind the call (proactive / admin) → send
        return None
    if is_ask_or_arrival(body):
        return None
    from timefmt import to_local
    now_local = to_local(now_utc or _now_utc(), user)
    ahead = hours_until_usual(user, now_local)
    if ahead is None or ahead < float(getattr(config, "CARD_EARLY_SEND_HOURS", 2.0)):
        return None
    t = usual_start(user, now_local)
    return (f"it's {_clock(now_local.time())} and they usually train around {_clock(t)} — "
            f"{ahead:.0f}h away; they're telling you the plan, not asking for the card")


def _clock(t: time) -> str:
    h12 = t.hour % 12 or 12
    suf = "am" if t.hour < 12 else "pm"
    return f"{h12}{suf}" if t.minute == 0 else f"{h12}:{t.minute:02d}{suf}"


def park(user_id: int, *, template_key: str | None, no_anchors: bool = False, exercises: list | None = None,
         reason: str = "") -> None:
    from models import get_session, User
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return
        merged = dict(u.lift_anchors or {}) if isinstance(u.lift_anchors, dict) else {}
        merged[_KEY] = {"key": template_key, "no_anchors": bool(no_anchors), "exercises": exercises or None,
                        "at": _now_utc().isoformat(), "reason": reason}
        u.lift_anchors = merged
        flag_modified(u, "lift_anchors")
        session.commit()
        logger.info("WORKOUT_CARD_PARKED user=%s key=%s one_off=%s reason=%s", user_id, template_key,
                    bool(exercises), reason)
    finally:
        session.close()


def peek(user_id: int) -> dict | None:
    """The parked card if one is fresh; stale entries read as none."""
    from models import get_session, User
    session = get_session()
    try:
        u = session.get(User, user_id)
        entry = (u.lift_anchors or {}).get(_KEY) if u and isinstance(u.lift_anchors, dict) else None
    finally:
        session.close()
    if not isinstance(entry, dict):
        return None
    try:
        age = (_now_utc() - datetime.fromisoformat(str(entry.get("at")))).total_seconds()
    except (TypeError, ValueError):
        return None
    return entry if age <= PARKED_TTL_S else None


def pop(user_id: int) -> dict | None:
    from models import get_session, User
    from sqlalchemy.orm.attributes import flag_modified
    entry = peek(user_id)
    session = get_session()
    try:
        u = session.get(User, user_id)
        if u and isinstance(u.lift_anchors, dict) and _KEY in u.lift_anchors:
            u.lift_anchors = {k: v for k, v in u.lift_anchors.items() if k != _KEY}
            flag_modified(u, "lift_anchors")
            session.commit()
    finally:
        session.close()
    return entry


def is_arrival_line(text: str | None) -> bool:
    body = (text or "").strip()
    return bool(body) and len(body) <= 80 and bool(_ARRIVAL_LINE_RE.match(body))


def release_parked_card(user_id: int, text: str | None) -> dict | None:
    """Inbound path: a parked card + a whole-message "I'm going / I'm there / send it" →
    the card goes out in code (the parked key / one-off list), no model turn. Returns the
    start result, or None when nothing was parked or the line isn't an arrival."""
    if not getattr(config, "CARD_EARLY_SEND_GATE_ENABLED", True) or not is_arrival_line(text):
        return None
    entry = peek(user_id)
    if not entry:
        return None
    from workouts.session_ops import session_in_progress
    try:
        if session_in_progress(user_id):
            pop(user_id)
            return None
    except Exception:  # noqa: BLE001
        pass
    pop(user_id)
    from workouts.start import start_workout_session
    r = start_workout_session(user_id, entry.get("key") or None, no_anchors=bool(entry.get("no_anchors")),
                              one_off=entry.get("exercises") or None)
    logger.info("WORKOUT_CARD_RELEASED user=%s session=%s key=%s trigger=%r", user_id, r.get("session_id"),
                r.get("template_key"), (text or "")[:60])
    return r


def context_line(user) -> str | None:
    """The block the coach sees while a card is parked."""
    if not getattr(config, "CARD_EARLY_SEND_GATE_ENABLED", True):
        return None
    try:
        entry = peek(user.id)
    except Exception:  # noqa: BLE001
        return None
    if not entry:
        return None
    what = "today's card" + (f" ({entry['key']})" if entry.get("key") else "") + (" — a one-off list" if entry.get("exercises") else "")
    return (f"## CARD PARKED\n{what} is built and WAITING — it sends itself the moment they say they're heading "
            f"out / at the gym / about to start, or if they ask for it. Don't call start_workout_session off a "
            f"mention of the gym, don't say the card is 'ready' or 'sent', don't list exercises or weights; when "
            f"they bring up tonight's session, one line is enough (\"see u at 10:30\"). If they're clearly going NOW, "
            f"start_workout_session sends it.")
