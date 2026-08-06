"""
timefmt — the single rendering boundary between naive-UTC storage and user-local display.

STORAGE CONVENTION (do not change): every timestamp column stores NAIVE UTC. On the prod
UTC server the aware-UTC column defaults round-trip to naive UTC; readers add tzinfo=UTC
when they need an aware value (the local disposable PG stores the same default as naive
LOCAL — a test-only artifact, see rewrite/phase-4 INVESTIGATION). This module is the ONE
place that converts stored naive-UTC → the user's zone for display, and computes the
user's local-day window. Keeping tz logic here means twenty readers don't each re-derive
it (and get it 7 hours wrong).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger("cued.timefmt")

DEFAULT_TZ = "America/Los_Angeles"


def resolve_tz(user) -> ZoneInfo:
    """The user's zone as a ZoneInfo (IANA name, never a fixed offset — the beta crosses
    the Nov DST transition). Logs when the default is used or the stored name is bad."""
    name = (getattr(user, "user_timezone", None) or "").strip()
    if not name:
        logger.warning("TIMEFMT_DEFAULT_TZ user=%s — no user_timezone, using %s",
                       getattr(user, "id", "?"), DEFAULT_TZ)
        return ZoneInfo(DEFAULT_TZ)
    try:
        return ZoneInfo(name)
    except Exception:
        logger.warning("TIMEFMT_BAD_TZ user=%s tz=%r — falling back to %s",
                       getattr(user, "id", "?"), name, DEFAULT_TZ)
        return ZoneInfo(DEFAULT_TZ)


def _as_aware_utc(dt: datetime) -> datetime:
    """A stored naive-UTC datetime → aware UTC. Already-aware inputs pass through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def to_local(dt: datetime, user) -> datetime:
    return _as_aware_utc(dt).astimezone(resolve_tz(user))


def local_day_bounds(user, *, now: datetime = None) -> tuple[datetime, datetime]:
    """[start, end) of the user's LOCAL calendar day, as NAIVE UTC — the window every
    'today' reader shares (meals, events, totals). `now` is an optional aware/naive-UTC
    reference instant (defaults to real now); useful for tests and for a fixed clock."""
    tz = resolve_tz(user)
    ref = _as_aware_utc(now) if now else datetime.now(timezone.utc)
    midnight = ref.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight.astimezone(timezone.utc).replace(tzinfo=None)
    end = (midnight + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def _hm(local: datetime) -> str:
    return local.strftime("%I:%M %p").lstrip("0")  # "9:51 PM", not "09:51 PM"


def _abbrev(local: datetime) -> str:
    return local.strftime("%Z") or "UTC"           # "PDT" / "PST", DST-correct via zoneinfo


def _humanize(delta: timedelta) -> str:
    secs = delta.total_seconds()
    ago = secs >= 0
    secs = abs(secs)
    if secs < 3600:
        n, unit = max(1, round(secs / 60)), "m"
    elif secs < 86400:
        n, unit = round(secs / 3600), "h"
    else:
        n, unit = round(secs / 86400), "d"
    return f"{n}{unit} ago" if ago else f"in {n}{unit}"


def render_time(dt: datetime, user, *, relative: bool = True, now: datetime = None) -> str:
    """Stored naive-UTC → user-local, labeled. Hybrid format: '9:51 PM PDT (2h ago)'.
    relative=False drops the '(… ago/from now)' tail — use it for future/date-only times."""
    local = to_local(dt, user)
    base = f"{_hm(local)} {_abbrev(local)}"
    if not relative:
        return base
    now_utc = _as_aware_utc(now) if now else datetime.now(timezone.utc)
    return f"{base} ({_humanize(now_utc - _as_aware_utc(dt))})"


def render_date(dt: datetime, user) -> str:
    """User-local calendar date, e.g. 'Mon Jul 21'. For day-granular rows (workouts)."""
    local = to_local(dt, user)
    return f"{local:%a %b} {local.day}"


# Day-level relative terms only — precision over recall, the same bias as the
# events.py regex floor. Week-level terms ("this weekend/week") are deliberately
# excluded v1: a wrong annotation is worse than a missing one. `(?!['’]s)` keeps
# possessives ("today's workout") unannotated.
_DEIXIS_RE = re.compile(
    r"\b(?:"
    r"(?P<tom>tomorrow(?:\s+(?:morning|afternoon|evening|night))?)"
    r"|(?P<yest>yesterday(?:\s+(?:morning|afternoon|evening))?|last\s+night)"
    r"|(?P<today>today|tonight|this\s+(?:morning|afternoon|evening))"
    r")\b(?!['’]s)",
    re.IGNORECASE,
)


def resolve_deixis(text: str, user, *, now: datetime = None) -> str:
    """Annotate day-level relative-time words with the resolved absolute date in the
    user's local zone: 'midterm tomorrow' → 'midterm tomorrow (Fri Aug 7)'.

    This is the deterministic floor under every durable-text writer (episodic digest,
    remember tool, legacy extraction, safety snippets): stored text carrying a bare
    'today' gets re-resolved by the model against NOW, not the note's date — a real
    past event resurfaces as current. Annotation, not replacement, so meaning
    survives a rare mis-resolve; idempotent (a term already followed by '(' is
    skipped), so writers can safely re-apply it."""
    if not text:
        return text
    tz = resolve_tz(user)
    ref = _as_aware_utc(now) if now else datetime.now(timezone.utc)
    local_today = ref.astimezone(tz).date()

    def _annotate(m):
        phrase = m.group(0)
        if text[m.end():].lstrip().startswith("("):
            return phrase
        if m.group("tom"):
            d = local_today + timedelta(days=1)
        elif m.group("yest"):
            d = local_today - timedelta(days=1)
        else:
            d = local_today
        return f"{phrase} ({d:%a %b} {d.day})"

    return _DEIXIS_RE.sub(_annotate, text)


def now_anchor(user, *, now: datetime = None) -> str:
    """The explicit local clock at the top of context — the thing the model computes
    relative time against. e.g. 'Right now: Tuesday, July 21, 2026, 2:11 PM PDT
    (America/Los_Angeles)'."""
    tz = resolve_tz(user)
    local = (_as_aware_utc(now) if now else datetime.now(timezone.utc)).astimezone(tz)
    return (f"Right now: {local:%A, %B} {local.day}, {local.year}, "
            f"{_hm(local)} {_abbrev(local)} ({tz.key})")
