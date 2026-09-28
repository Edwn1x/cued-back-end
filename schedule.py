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


# ─── deterministic rundown ──────────────────────────────────────────────────────
#
# The rundown is the fix for the 2026-09-28 "rest of the week" bug: the model had all
# 21 events in context (incl. a Friday CS61C deadline + the weekend) but truncated the
# list for brevity, dropped the tail, and asserted "that's the week." Nothing was
# missing from the DATA — completeness was left to model summarization and it failed.
#
# So completeness is computed HERE, in code, and handed to the coach as finished text:
#   • ALL deadlines in the window are enumerated in a dedicated section and can NEVER
#     be dropped (a message-length cap only ever trims *routine* one-off events).
#   • events are grouped by the user's LOCAL day (naive-UTC storage → user tz), so a
#     late-night event can't land on the wrong calendar day (this repo's recurring
#     UTC-vs-local bug).
#   • duplicate copies of the same event across calendars are de-duplicated.
#   • recurring classes/blocks collapse to one "Mon/Wed/Fri 11–12" line instead of
#     being listed under every day.
# Read-only over the Event store; fail-open (any error → an honest short string).

_RUNDOWN_DEFAULT_DAYS = 7


@dataclass
class RundownEvent:
    id: int
    title: str
    start: datetime            # naive UTC
    end: datetime | None       # naive UTC (effective end may differ; see event_end)
    all_day: bool
    is_deadline: bool
    is_exam: bool
    passed: bool
    source: str


def _to_local(user, dt: datetime):
    from timefmt import to_local
    return to_local(dt, user)


def _fmt_hm(user, dt: datetime) -> str:
    return _to_local(user, dt).strftime("%-I:%M %p").lstrip("0")


def _fmt_daylabel(user, dt: datetime) -> str:
    d = _to_local(user, dt)
    return f"{d:%a %b} {d.day}"


def _fmt_span(user, e: RundownEvent) -> str:
    if e.all_day:
        return "all day"
    if e.end and e.end > e.start:
        return f"{_fmt_hm(user, e.start)}–{_fmt_hm(user, e.end)}"
    return _fmt_hm(user, e.start)


def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (t or "").lower())


def _local_midnight_utc(tz: ZoneInfo, d) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)


def resolve_window(phrase: str, user, *, days: int = None, now=None) -> tuple[datetime, datetime, str, bool]:
    """Map a natural window phrase → (lo, hi) naive-UTC bounds, a human label, and a
    `deadlines_only` flag. Sensible defaults: "this week"/"rest of the week" run through
    the coming Sunday (local); an unrecognised phrase → the next
    config.SCHEDULE_RUNDOWN_DEFAULT_DAYS days. An explicit `days` always wins."""
    now_utc = _now_naive_utc(now)
    tz = _tz(user)
    today = now_utc.replace(tzinfo=timezone.utc).astimezone(tz).date()

    def mid(d):
        return _local_midnight_utc(tz, d)

    if days:
        d = max(1, int(days))
        return now_utc, now_utc + timedelta(days=d), f"the next {d} days", False

    p = (phrase or "").strip().lower()
    wd = today.weekday()                                  # Mon=0 … Sun=6
    next_monday = today + timedelta(days=(7 - wd) or 7)   # start of next week (local)

    if "next week" in p:
        return mid(next_monday), mid(next_monday + timedelta(days=7)), "next week", False
    if "rest of" in p and ("week" in p or "day" not in p):
        return mid(today), mid(next_monday), "the rest of the week", False
    if "tomorrow" in p:
        d = today + timedelta(days=1)
        return mid(d), mid(d + timedelta(days=1)), "tomorrow", False
    if "today" in p or "tonight" in p:
        return mid(today), mid(today + timedelta(days=1)), "today", False
    if "week" in p:                                       # "this week" / "my week"
        return mid(today), mid(next_monday), "this week", False
    if "due" in p or "deadline" in p:                     # "what's due" (no week word)
        d = config.CALENDAR_DEADLINE_DAYS
        return now_utc, now_utc + timedelta(days=d), "what's due", True

    d = config.SCHEDULE_RUNDOWN_DEFAULT_DAYS
    return now_utc, now_utc + timedelta(days=d), f"the next {d} days", False


def _any_calendar_connected(user_id: int, session) -> bool:
    """Read-only: does the user have any connected calendar provider (gcal/bcourses/
    canvas)? Distinguishes "nothing scheduled" from "you never connected a calendar"."""
    try:
        from models import Integration
        row = (session.query(Integration)
               .filter(Integration.user_id == user_id,
                       Integration.provider.in_(("gcal", "bcourses", "canvas")),
                       Integration.status == "connected")
               .first())
        return row is not None
    except Exception:  # noqa: BLE001
        return False


def collect_rundown(user_id: int, *, lo: datetime, hi: datetime, now=None, session=None) -> list[RundownEvent]:
    """All active calendar events overlapping [lo, hi), de-duplicated across calendars,
    sorted by start. Dedup key = (local day, local start clock / all-day, normalised
    title) — this collapses the two gcal copies of an event into one. Read-only; []
    on error."""
    own = session is None
    try:
        from models import get_session, active, Event
        session = session or get_session()
        now_utc = _now_naive_utc(now)
        rows = (active(session, Event, user_id=user_id)
                .filter(Event.source.in_(CALENDAR_SOURCES),
                        Event.occurred_at.isnot(None),
                        Event.occurred_at >= lo,
                        Event.occurred_at < hi)
                .order_by(Event.occurred_at).limit(500).all())
        out: list[RundownEvent] = []
        seen: set = set()
        for e in rows:
            is_dl, is_exam = _is_deadline(e)
            title = (getattr(e, "title", None) or getattr(e, "raw_text", None)
                     or getattr(e, "event_type", None) or "event").strip()[:160]
            all_day = bool(getattr(e, "all_day", False))
            # dedup key: same instant (naive UTC == same local instant) + title. All-day
            # copies key on the date; timed copies on the exact start.
            key = (e.occurred_at.date() if all_day else None,
                   None if all_day else e.occurred_at.replace(microsecond=0),
                   all_day, _norm_title(title))
            if key in seen:
                continue
            seen.add(key)
            out.append(RundownEvent(
                id=e.id, title=title, start=e.occurred_at,
                end=getattr(e, "ends_at", None), all_day=all_day,
                is_deadline=is_dl, is_exam=is_exam,
                passed=(event_end(e) < now_utc), source=e.source))
        return out
    except Exception as ex:  # noqa: BLE001
        logger.warning("COLLECT_RUNDOWN_FAILED user=%s err=%s", user_id, ex)
        return []
    finally:
        if own and session is not None:
            session.close()


def _split_recurring(user, events: list[RundownEvent]) -> tuple[list, list[RundownEvent]]:
    """Partition non-deadline events into (recurring_groups, one_off). A group is
    recurring when the same title at the same local clock time appears on ≥2 distinct
    local days in the window — collapse it to one 'Mon/Wed/Fri 11–12' line instead of
    listing it under every day. Deadlines are never passed in here."""
    from collections import OrderedDict
    groups: "OrderedDict[tuple, list[RundownEvent]]" = OrderedDict()
    for e in events:
        local = _to_local(user, e.start)
        clock = "allday" if e.all_day else local.strftime("%H:%M")
        groups.setdefault((_norm_title(e.title), clock), []).append(e)

    recurring, one_off = [], []
    for _key, evs in groups.items():
        local_days = sorted({_to_local(user, e.start).date() for e in evs})
        if len(local_days) >= 2:
            reps = sorted(evs, key=lambda e: e.start)
            wds = [_to_local(user, e.start) for e in reps]
            # unique weekday abbreviations in week order
            seen_wd, day_abbr = set(), []
            for w in sorted(wds, key=lambda d: (d.weekday())):
                a = w.strftime("%a")
                if a not in seen_wd:
                    seen_wd.add(a)
                    day_abbr.append(a)
            recurring.append({
                "title": reps[0].title,
                "days_str": "/".join(day_abbr),
                "time_str": _fmt_span(user, reps[0]),
            })
        else:
            one_off.extend(evs)
    return recurring, one_off


def format_rundown(user, events: list[RundownEvent], *, label: str, lo: datetime,
                   hi: datetime, now=None, deadlines_only: bool = False) -> str:
    """Render a complete, day-grouped rundown. Deadlines are ALWAYS enumerated in full;
    only routine one-off events are ever capped (config.SCHEDULE_RUNDOWN_MAX_EVENTS)."""
    lo_l = _to_local(user, lo)
    hi_l = _to_local(user, hi - timedelta(seconds=1))
    rng = f"{lo_l:%a %b} {lo_l.day}"
    if (hi_l.year, hi_l.month, hi_l.day) != (lo_l.year, lo_l.month, lo_l.day):
        rng += f" – {hi_l:%a %b} {hi_l.day}"
    lines = [f"{label} ({rng}):"]

    deadlines = sorted([e for e in events if e.is_deadline], key=lambda e: e.start)
    others = [e for e in events if not e.is_deadline]

    if deadlines:
        lines.append("")
        lines.append("deadlines:")
        for e in deadlines:
            day = _fmt_daylabel(user, e.start)
            mark = " (passed)" if e.passed else ""
            if e.all_day:
                lines.append(f"• {day} — {e.title}{mark}")
            else:
                lines.append(f"• {day}, {_fmt_hm(user, e.start)} — {e.title}{mark}")

    if deadlines_only:
        return "\n".join(lines)

    recurring, one_off = _split_recurring(user, others)
    if recurring:
        lines.append("")
        lines.append("regular:")
        for g in recurring:
            lines.append(f"• {g['title']} — {g['days_str']} {g['time_str']}")

    if one_off:
        cap = config.SCHEDULE_RUNDOWN_MAX_EVENTS
        one_off = sorted(one_off, key=lambda e: e.start)
        overflow = 0
        if len(one_off) > cap:
            overflow = len(one_off) - cap
            one_off = one_off[:cap]
        # group by local day, preserving chronological order
        from collections import OrderedDict
        by_day: "OrderedDict[tuple, list[RundownEvent]]" = OrderedDict()
        for e in one_off:
            d = _to_local(user, e.start)
            by_day.setdefault((d.year, d.month, d.day), []).append(e)
        for (_y, _m, _d), evs in by_day.items():
            lines.append("")
            lines.append(f"{_fmt_daylabel(user, evs[0].start)}:")
            for e in evs:
                mark = " (passed)" if e.passed else ""
                lines.append(f"• {_fmt_span(user, e)} — {e.title}{mark}")
        if overflow:
            lines.append(f"• (+{overflow} more)")

    return "\n".join(lines)


def build_rundown(user_id: int, phrase: str = None, *, days: int = None,
                  now=None, session=None) -> str:
    """The one entry point the coach's tool calls: a COMPLETE, day-grouped, deadline-
    safe rundown for the requested window, as finished text to relay. Honest on an empty
    window; offers to connect if no calendar is linked at all. Read-only; fail-open."""
    if not config.SCHEDULE_RUNDOWN_ENABLED:
        return ""
    own = session is None
    try:
        from models import get_session, User
        session = session or get_session()
        user = session.get(User, user_id)
        if not user:
            return "i can't see your calendar right now."
        lo, hi, label, deadlines_only = resolve_window(phrase, user, days=days, now=now)
        events = collect_rundown(user_id, lo=lo, hi=hi, now=now, session=session)
        if deadlines_only:
            events = [e for e in events if e.is_deadline]
        if not events:
            if not _any_calendar_connected(user_id, session):
                return ("your calendar isn't connected yet, so i can't see your schedule — "
                        "want me to send the link to hook it up?")
            if deadlines_only:
                return f"nothing due in {label}."
            return f"nothing on your calendar for {label}."
        return format_rundown(user, events, label=label, lo=lo, hi=hi,
                              now=now, deadlines_only=deadlines_only)
    except Exception as e:  # noqa: BLE001
        logger.warning("BUILD_RUNDOWN_FAILED user=%s err=%s", user_id, e)
        return "i hit a snag pulling your schedule — try me again in a sec?"
    finally:
        if own and session is not None:
            session.close()
