"""
Episodic Events — deterministic inbound detection + local-day reader.

Mirrors the safety pre-pass split (memory.detect_safety_signals /
apply_safety_signals_task): a NARROW regex floor here, the Phase 2 model layer on
top for recall. The floor is biased toward PRECISION, not recall, because the
error costs are asymmetric:

  - false negative on went_to_gym  -> one redundant nudge (mildly annoying)
  - false positive on went_to_gym  -> suppresses a legitimate accountability nudge
                                      AND (later) feeds a wrong split-pointer
                                      advance. Much worse.

So we match only unambiguous present/past-completed phrasings and deliberately
skip futures, hypotheticals, and prior-day references. Phrases that mention the
domain but don't match are logged as EVENT_NEAR_MISS — that corpus is what the
Phase 2 model uses to cover recall.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from models import get_session, User, Event

logger = logging.getLogger("cued.events")

DEFAULT_TZ = "America/Los_Angeles"
IN_CLASS_DEFAULT_MINUTES = 90  # when no end time is stated

# ── went_to_gym: completed only ("just got back", "already went", "just lifted")
_GYM_WENT_RE = re.compile(
    r"\b("
    r"just\s+(?:got\s+(?:back|out)\s+(?:from|of)\s+the\s+gym"
    r"|finished\s+(?:my\s+)?(?:workout|lift(?:ing)?)"
    r"|hit\s+the\s+gym|worked\s+out|did\s+my\s+workout|lifted)"
    r"|already\s+(?:went|hit\s+the\s+gym|worked\s+out|lifted)"
    r"|(?:back|done)\s+(?:from|with)\s+(?:the\s+gym|my\s+workout)"
    r"|got\s+(?:my\s+)?(?:workout|lift)\s+(?:in|done)"
    r")\b",
    re.IGNORECASE,
)

# Prior-day / non-today markers that disqualify a "went" match (keeps precision).
_PRIOR_DAY_RE = re.compile(r"\b(yesterday|last\s+night|last\s+week|the\s+other\s+day|earlier\s+this\s+week)\b",
                           re.IGNORECASE)

# ── in_class: "in class", optionally "till/until 2(:30)(pm)"
_IN_CLASS_RE = re.compile(
    r"\bin\s+class\b(?:[^.!?\n]*?\b(?:till|until|til|to)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?",
    re.IGNORECASE,
)

# Domain keywords used only to detect near-misses (something gym/class-ish that
# the precise floor didn't catch) for the Phase 2 recall corpus.
_NEAR_MISS_RE = re.compile(r"\b(gym|workout|work\s*out|lift(?:ing|ed)?|class)\b", re.IGNORECASE)


def detect_event_signals(message: str) -> list[dict]:
    """Pure function. Return a list of detected event specs (may be empty).

    went_to_gym -> {"event_type": "went_to_gym"}
    in_class    -> {"event_type": "in_class", "end": (hour:int, minute:int, ampm:str|None) | None}
    """
    text = message or ""
    out: list[dict] = []

    if _GYM_WENT_RE.search(text) and not _PRIOR_DAY_RE.search(text):
        out.append({"event_type": "went_to_gym"})

    m = _IN_CLASS_RE.search(text)
    if m:
        end = None
        if m.group(1):
            end = (int(m.group(1)),
                   int(m.group(2)) if m.group(2) else 0,
                   (m.group(3) or "").lower() or None)
        out.append({"event_type": "in_class", "end": end})

    return out


def _resolve_class_end_utc(end_spec, user_tz: ZoneInfo, now_local: datetime) -> datetime:
    """Resolve an in_class end spec to naive UTC in the user's local day."""
    if not end_spec:
        return (now_local.astimezone(timezone.utc).replace(tzinfo=None)
                + timedelta(minutes=IN_CLASS_DEFAULT_MINUTES))
    hour, minute, ampm = end_spec
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    elif ampm is None and hour <= 7:
        hour += 12  # a bare "till 2" from a student is almost always 2pm
    hour = min(hour, 23)
    end_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return end_local.astimezone(timezone.utc).replace(tzinfo=None)


def record_event(user_id: int, event_type: str, *, ends_at=None, source="regex",
                 raw_text: str = None, occurred_at=None) -> int:
    """Append one Event. Returns the new event id."""
    session = get_session()
    try:
        ev = Event(user_id=user_id, event_type=event_type, ends_at=ends_at,
                   source=source, raw_text=(raw_text or "")[:1000])
        if occurred_at is not None:
            ev.occurred_at = occurred_at
        session.add(ev)
        session.commit()
        return ev.id
    finally:
        session.close()


def apply_event_signals_task(user_id: int, message: str):
    """Synchronous inbound floor: detect + persist nudge-critical events. Regex
    only (no LLM), idempotent enough for the nudge use case (a second identical
    'already went' just adds another same-day event; readers dedupe by type)."""
    signals = detect_event_signals(message)
    if not signals:
        if _NEAR_MISS_RE.search(message or ""):
            logger.info("EVENT_NEAR_MISS user_id=%s text=%r", user_id, (message or "")[:160])
        return

    session = get_session()
    try:
        user = session.get(User, user_id)
        tz = ZoneInfo((user.user_timezone if user else None) or DEFAULT_TZ)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)
    finally:
        session.close()
    now_local = datetime.now(tz)

    for sig in signals:
        ends_at = None
        if sig["event_type"] == "in_class":
            ends_at = _resolve_class_end_utc(sig.get("end"), tz, now_local)
        eid = record_event(user_id, sig["event_type"], ends_at=ends_at, raw_text=message)
        logger.info("EVENT_DETECTED user_id=%s type=%s event_id=%s ends_at=%s",
                    user_id, sig["event_type"], eid, ends_at)

        # A completed gym visit is a "user confirms a completed day" trigger for
        # the split pointer. A day named in the same message ("...did legs") is
        # confirmed; otherwise code infers the next day. Write-time only.
        if sig["event_type"] == "went_to_gym":
            from split_pointer import advance_split_pointer, parse_named_split_day
            advance_split_pointer(user_id, named_day=parse_named_split_day(message))


def _local_day_window_utc(user) -> tuple[datetime, datetime]:
    """[start, end) of the user's LOCAL calendar day, as naive UTC."""
    try:
        tz = ZoneInfo((user.user_timezone if user else None) or DEFAULT_TZ)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)
    midnight_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight_local.astimezone(timezone.utc).replace(tzinfo=None)
    end = (midnight_local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def todays_events(user_id: int, event_type: str = None) -> list:
    """Events that occurred during the user's LOCAL today (soft-deleted excluded).
    Optionally filter by type. Goes through models.active so a manage_log delete
    actually removes the event from every reader — the loop context AND the heartbeat —
    not just from the list. Without this the coach could honestly say "deleted" while
    the row still drove context."""
    from models import active
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return []
        start, end = _local_day_window_utc(user)
        q = (active(session, Event, user_id=user_id)
             .filter(Event.occurred_at >= start, Event.occurred_at < end))
        if event_type:
            q = q.filter(Event.event_type == event_type)
        return q.order_by(Event.occurred_at.desc()).all()
    finally:
        session.close()


def event_end(ev) -> datetime:
    """Effective end of an event, naive UTC: ends_at, else occurred_at + the same
    default duration in_class uses. Lifecycle state (upcoming/passed) is COMPUTED
    from this + now — never stored, so it can't drift from the datetimes."""
    if ev.ends_at:
        return ev.ends_at
    return ev.occurred_at + timedelta(minutes=IN_CLASS_DEFAULT_MINUTES)


def _now_naive_utc(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def upcoming_events(user_id: int, days: int = 7, *, now=None) -> list:
    """Model-logged events after the user's local end-of-today, within `days` local
    days ahead — the forward reader todays_events never had (an event for Friday
    was invisible until Friday). Today's own events stay in todays_events."""
    from models import active
    from timefmt import local_day_bounds
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return []
        _start, end_today = local_day_bounds(user, now=now)
        horizon = end_today + timedelta(days=days)
        return (active(session, Event, user_id=user_id)
                .filter(Event.source == "model",
                        Event.occurred_at >= end_today,
                        Event.occurred_at < horizon)
                .order_by(Event.occurred_at).all())
    finally:
        session.close()


def recently_passed_events(user_id: int, hours: int = 48, *, now=None) -> list:
    """Model-logged events whose effective end is in [now − hours, now) AND before
    the user's local start of today (today's passed events render in TODAY'S EVENTS
    with a PASSED mark instead). This is the bounded follow-up window: inside it a
    passed event is "how'd it go" material; past it the event retires — a passed
    event must NEVER read as upcoming again."""
    from models import active
    from timefmt import local_day_bounds
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return []
        start_today, _end = local_day_bounds(user, now=now)
        now_utc = _now_naive_utc(now)
        window_start = now_utc - timedelta(hours=hours)
        rows = (active(session, Event, user_id=user_id)
                .filter(Event.source == "model",
                        # effective end >= window_start bounds occurred_at below
                        # by window_start - the default duration; a day of margin.
                        Event.occurred_at >= window_start - timedelta(days=1),
                        Event.occurred_at < start_today)
                .order_by(Event.occurred_at).all())
        return [ev for ev in rows
                if window_start <= event_end(ev) < now_utc
                and event_end(ev) < start_today]
    finally:
        session.close()


def in_class_now(user_id: int) -> bool:
    """True if there's a regex-FLOOR in_class event today whose end is still in the
    future. This is a deterministic hard gate (high-precision regex source only): a
    model-sourced event (source='model', e.g. a logged 'summit') deliberately does NOT
    trip it — model events inform proactive JUDGMENT (context), floor events hard-gate.
    See rewrite/phase-4 (heartbeat) + the log_event burn-in fix."""
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    for ev in todays_events(user_id, "in_class"):
        if ev.source == "regex" and ev.ends_at and ev.ends_at > now_utc:
            return True
    return False


def went_to_gym_today(user_id: int) -> bool:
    return len(todays_events(user_id, "went_to_gym")) > 0
