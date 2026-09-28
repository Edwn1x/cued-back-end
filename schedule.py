"""
schedule.py — read-only calendar primitives for the proactive calendar assistant.

The Event table already holds the user's real schedule (source gcal / bcourses /
canvas for synced calendars + due dates, source model for logged items). events.py
owns the day-windowed readers the loop already uses; this module adds the three
CROSS-DOMAIN primitives the heartbeat needs to turn that passive calendar into
proactive material:

  • free_blocks    — the timed gaps in today's/tomorrow's schedule, inside waking
                     hours, long enough to be a candidate gym/study window.
  • deadline_items — upcoming assignment/exam events (bcourses/canvas "due:" items
                     + gcal/model items that read like a deadline), sorted, with a
                     density/cluster count.
  • high_load_soon — True when an exam or a dense cluster of deadlines is within
                     ~48h (used to ease training intensity + soften the tone).

Everything here is READ-ONLY on the Event store (never writes, never touches the
gcal / bcourses / canvas sync path or the google_health write path) and FAIL-OPEN:
any error or missing calendar data returns an empty result, never raises — a
calendar read must never crash a heartbeat tick.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from events import CALENDAR_SOURCES, event_end

logger = logging.getLogger("cued.schedule")

DEFAULT_TZ = "America/Los_Angeles"
_DEFAULT_WAKE = (8, 0)
_DEFAULT_SLEEP = (23, 0)

# A deadline reads as one of these anywhere in its title/text; bcourses & canvas
# assignments are additionally stored with a literal "due: " prefix (see
# integrations/bcourses.py, integrations/canvas.py). Precision-biased — a false
# positive here only makes the coach mention a non-deadline event as due.
_DEADLINE_RE = re.compile(
    r"\b(due|deadline|exam|midterm|finals?|quiz|test|hw\s*\d*|homework|"
    r"p-?set|problem\s+set|assignment|project\s+due|paper|essay|submission|turn\s+in)\b",
    re.IGNORECASE,
)
# The subset that means "high-stakes assessment" — an exam within 48h eases training
# and softens the tone even when it's the ONLY deadline (no cluster needed).
_EXAM_RE = re.compile(r"\b(exam|midterm|finals?|quiz|\btest\b)\b", re.IGNORECASE)


# ─── dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class FreeBlock:
    """A timed gap in the schedule, naive-UTC [start, end). `minutes` is its length."""
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class Deadline:
    """An upcoming deadline event, naive-UTC `when` (its start/due instant)."""
    id: int
    title: str
    when: datetime
    all_day: bool
    is_exam: bool

    def days_until(self, now: datetime) -> float:
        return (self.when - now).total_seconds() / 86400

    def hours_until(self, now: datetime) -> float:
        return (self.when - now).total_seconds() / 3600


# ─── time helpers (read-only; mirror timefmt conventions) ─────────────────────

def _tz(user) -> ZoneInfo:
    try:
        from timefmt import resolve_tz
        return resolve_tz(user)
    except Exception:
        try:
            return ZoneInfo((getattr(user, "user_timezone", None) or DEFAULT_TZ))
        except Exception:
            return ZoneInfo(DEFAULT_TZ)


def _now_naive_utc(now=None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def _ref_local(user, now=None) -> datetime:
    aware = (now if (now is not None and now.tzinfo is not None)
             else (now.replace(tzinfo=timezone.utc) if now is not None
                   else datetime.now(timezone.utc)))
    return aware.astimezone(_tz(user))


_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
_HOUR_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)


def _parse_clock(s, default) -> tuple[int, int]:
    """Best-effort (hour, minute) from a profile time field, else `default`."""
    if not s:
        return default
    m = _HHMM_RE.match(str(s))
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
        return (h, mm) if (0 <= h <= 23 and 0 <= mm <= 59) else default
    m = _HOUR_RE.search(str(s).strip().lower())
    if not m:
        return default
    h = int(m.group(1))
    mm = int(m.group(2)) if m.group(2) else 0
    ap = (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return (h, mm) if (0 <= h <= 23 and 0 <= mm <= 59) else default


def _waking_window(user, local_date) -> tuple[datetime, datetime]:
    """(wake, sleep) as aware-local instants for the waking day starting on `local_date`.
    Defaults 08:00 / 23:00. A sleep at/before the wake hour (e.g. 00:30) is after
    midnight, so the window runs into the next date."""
    tz = _tz(user)
    wake = _parse_clock(getattr(user, "wake_time", None), _DEFAULT_WAKE)
    sleep = _parse_clock(getattr(user, "sleep_time", None), _DEFAULT_SLEEP)
    w = datetime(local_date.year, local_date.month, local_date.day, wake[0], wake[1], tzinfo=tz)
    s = datetime(local_date.year, local_date.month, local_date.day, sleep[0], sleep[1], tzinfo=tz)
    if s <= w:
        s += timedelta(days=1)
    return w, s


def _to_naive_utc(aware: datetime) -> datetime:
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _merge(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge overlapping/adjacent [start, end) intervals (naive UTC), sorted by start."""
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda p: p[0])
    out = [intervals[0]]
    for s, e in intervals[1:]:
        ls, le = out[-1]
        if s <= le:
            out[-1] = (ls, max(le, e))
        else:
            out.append((s, e))
    return out


# ─── primitives ───────────────────────────────────────────────────────────────

def _timed_events(user_id: int, session, lo: datetime, hi: datetime) -> list:
    """Active, TIMED calendar events overlapping [lo, hi) (naive UTC). All-day events
    are excluded — they don't occupy a clock window."""
    from models import active, Event
    rows = (active(session, Event, user_id=user_id)
            .filter(Event.source.in_(CALENDAR_SOURCES),
                    Event.all_day.isnot(True),
                    Event.occurred_at.isnot(None),
                    Event.occurred_at < hi)
            .order_by(Event.occurred_at).limit(200).all())
    return [e for e in rows if event_end(e) > lo]


def timed_schedule(user_id: int, session=None, *, horizon_hours: int = None, now=None) -> list:
    """Active TIMED calendar events over the next `horizon_hours` (naive-UTC ordered).
    The presence check the free-window signals use to stay inert on an empty calendar.
    Read-only; fail-open to []."""
    horizon_hours = horizon_hours or config.CALENDAR_FREE_BLOCK_HORIZON_HOURS
    own = session is None
    try:
        from models import get_session
        session = session or get_session()
        now_utc = _now_naive_utc(now)
        return _timed_events(user_id, session, now_utc, now_utc + timedelta(hours=horizon_hours))
    except Exception as e:  # noqa: BLE001
        logger.warning("TIMED_SCHEDULE_FAILED user=%s err=%s", user_id, e)
        return []
    finally:
        if own and session is not None:
            session.close()


def busy_runs(user_id: int, session=None, *, horizon_hours: int = None, now=None) -> list[tuple[datetime, datetime]]:
    """Merged back-to-back busy intervals (naive UTC) over the horizon — a run longer
    than an eating gap is 'eat before it' material (meal timing). Read-only; fail-open []."""
    horizon_hours = horizon_hours or config.CALENDAR_FREE_BLOCK_HORIZON_HOURS
    own = session is None
    try:
        from models import get_session
        session = session or get_session()
        now_utc = _now_naive_utc(now)
        hi = now_utc + timedelta(hours=horizon_hours)
        return _merge([(max(e.occurred_at, now_utc), event_end(e))
                       for e in _timed_events(user_id, session, now_utc, hi)])
    except Exception as e:  # noqa: BLE001
        logger.warning("BUSY_RUNS_FAILED user=%s err=%s", user_id, e)
        return []
    finally:
        if own and session is not None:
            session.close()


def free_blocks(user_id: int, session=None, *, horizon_hours: int = None,
                min_minutes: int = None, now=None) -> list[FreeBlock]:
    """Candidate gym/study windows: the timed gaps in the user's schedule over the next
    `horizon_hours` (default ~36h → today + tomorrow), clipped to waking hours, at least
    `min_minutes` long (default config.CALENDAR_FREE_BLOCK_MIN_MINUTES). Read-only;
    fail-open to []. A day with no timed events yields the whole waking window(s) as
    free blocks — callers that only want to speak when there's a real schedule should
    check for events first."""
    horizon_hours = horizon_hours or config.CALENDAR_FREE_BLOCK_HORIZON_HOURS
    min_minutes = min_minutes or config.CALENDAR_FREE_BLOCK_MIN_MINUTES
    own = session is None
    try:
        from models import get_session, User
        session = session or get_session()
        user = session.get(User, user_id)
        if not user:
            return []
        now_utc = _now_naive_utc(now)
        hi = now_utc + timedelta(hours=horizon_hours)
        busy = _merge([(max(e.occurred_at, now_utc), event_end(e))
                       for e in _timed_events(user_id, session, now_utc, hi)])

        # Waking segments (naive UTC) across the horizon, each clipped to [now, hi).
        local = _ref_local(user, now)
        segments: list[tuple[datetime, datetime]] = []
        for off in range(-1, (horizon_hours // 24) + 2):
            w, s = _waking_window(user, (local + timedelta(days=off)).date())
            seg_lo = max(_to_naive_utc(w), now_utc)
            seg_hi = min(_to_naive_utc(s), hi)
            if seg_hi - seg_lo >= timedelta(minutes=min_minutes):
                segments.append((seg_lo, seg_hi))
        segments = _merge(segments)

        blocks: list[FreeBlock] = []
        for seg_lo, seg_hi in segments:
            cursor = seg_lo
            for b_lo, b_hi in busy:
                if b_hi <= seg_lo or b_lo >= seg_hi:
                    continue
                if b_lo - cursor >= timedelta(minutes=min_minutes):
                    blocks.append(FreeBlock(cursor, b_lo))
                cursor = max(cursor, b_hi)
            if seg_hi - cursor >= timedelta(minutes=min_minutes):
                blocks.append(FreeBlock(cursor, seg_hi))
        return sorted(blocks, key=lambda b: b.start)
    except Exception as e:  # noqa: BLE001
        logger.warning("FREE_BLOCKS_FAILED user=%s err=%s", user_id, e)
        return []
    finally:
        if own and session is not None:
            session.close()


def _is_deadline(ev) -> tuple[bool, bool]:
    """(is_deadline, is_exam) for an event, from its title/text. bcourses & canvas
    assignments carry a 'due: ' prefix; gcal/model items match the keyword floor."""
    text = (getattr(ev, "title", None) or getattr(ev, "raw_text", None) or "").strip()
    if not text:
        return False, False
    low = text.lower()
    is_dl = low.startswith("due:") or bool(_DEADLINE_RE.search(text))
    return is_dl, bool(_EXAM_RE.search(text))


def deadline_items(user_id: int, session=None, *, days: int = None, now=None) -> list[Deadline]:
    """Upcoming deadlines (assignments/exams) within `days` (default
    config.CALENDAR_DEADLINE_DAYS), sorted soonest-first. Read-only; fail-open to []."""
    days = days or config.CALENDAR_DEADLINE_DAYS
    own = session is None
    try:
        from models import get_session, active, Event
        session = session or get_session()
        now_utc = _now_naive_utc(now)
        hi = now_utc + timedelta(days=days)
        rows = (active(session, Event, user_id=user_id)
                .filter(Event.source.in_(CALENDAR_SOURCES),
                        Event.occurred_at.isnot(None),
                        Event.occurred_at >= now_utc,
                        Event.occurred_at < hi)
                .order_by(Event.occurred_at).limit(200).all())
        out: list[Deadline] = []
        for e in rows:
            is_dl, is_exam = _is_deadline(e)
            if not is_dl:
                continue
            title = (getattr(e, "title", None) or getattr(e, "raw_text", None) or "deadline").strip()
            out.append(Deadline(id=e.id, title=title[:120], when=e.occurred_at,
                                all_day=bool(getattr(e, "all_day", False)), is_exam=is_exam))
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("DEADLINE_ITEMS_FAILED user=%s err=%s", user_id, e)
        return []
    finally:
        if own and session is not None:
            session.close()


def deadlines_within(items: list[Deadline], hours: float, *, now=None) -> list[Deadline]:
    """The subset of `items` due within `hours` from now."""
    now_utc = _now_naive_utc(now)
    horizon = now_utc + timedelta(hours=hours)
    return [d for d in items if d.when <= horizon]


def cluster_count(items: list[Deadline], *, hours: float = None, now=None) -> int:
    """How many deadlines fall within `hours` from now (default the high-load window) —
    the density figure the radar quotes ('3 things due this week')."""
    hours = hours if hours is not None else config.CALENDAR_HIGH_LOAD_HOURS
    return len(deadlines_within(items, hours, now=now))


def high_load_soon(user_id: int, session=None, *, now=None, items: list[Deadline] = None) -> bool:
    """True if an EXAM or a DENSE cluster of deadlines lands within
    config.CALENDAR_HIGH_LOAD_HOURS (~48h). Used to ease training intensity and soften
    the coaching tone. Read-only; fail-open to False. Pass `items` to reuse a list
    deadline_items already returned (avoids a second query)."""
    try:
        if items is None:
            items = deadline_items(user_id, session, now=now)
        soon = deadlines_within(items, config.CALENDAR_HIGH_LOAD_HOURS, now=now)
        if any(d.is_exam for d in soon):
            return True
        return len(soon) >= config.CALENDAR_HIGH_LOAD_CLUSTER
    except Exception as e:  # noqa: BLE001
        logger.warning("HIGH_LOAD_SOON_FAILED user=%s err=%s", user_id, e)
        return False
