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


_HHMM_STRICT_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")

# set_day_reset(0) stores this instead of 0: "the standard midnight day — and don't
# auto-shift me". A plain 0/None column is "unset", which lets the bedtime rule below apply.
EXPLICIT_MIDNIGHT = -1
# A bedtime at/before this local hour counts as an AFTER-MIDNIGHT bedtime (03:00, 00:30);
# 11:00 or later is an evening bedtime → the standard day. Keeps the derived hour ≤ 11.
AUTO_RESET_MAX_BED_HOUR = 10


def _strict_hhmm(s) -> tuple[int, int] | None:
    m = _HHMM_STRICT_RE.match(str(s or ""))
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    return (h, mm) if 0 <= h <= 23 and 0 <= mm <= 59 else None


def auto_day_reset_hour(user) -> int:
    """The rollover hour an AFTER-MIDNIGHT bedtime implies: one hour past their usual
    sleep_time (03:00 → 4, 00:30 → 1), capped at 11. An evening bedtime, a phrase instead
    of a clock, or the flag off → 0 (the standard midnight day, exactly as before).

    Live 2026-10-06 (user 48, sleeps ~3am): a 12:10am burger became "980 for the day" on a
    fresh day and he had to ask for it to be moved to yesterday. The rhythm the day should
    follow was already in the profile."""
    import config
    if not getattr(config, "NUTRITION_DAY_AUTO_RESET_ENABLED", True):
        return 0
    hm = _strict_hhmm(getattr(user, "sleep_time", None))
    if hm is None:
        return 0
    h = hm[0]
    if 0 <= h <= AUTO_RESET_MAX_BED_HOUR:
        return min(h + 1, 11)
    return 0


def day_reset_hour(user) -> int:
    """The local hour the user's nutrition day rolls over. An explicit set_day_reset value
    (1–11) wins; EXPLICIT_MIDNIGHT (-1) pins the standard day; otherwise (0/None) the hour
    is DERIVED from an after-midnight bedtime (auto_day_reset_hour), else 0. A value of
    e.g. 4 means the day runs 4am→4am, so a 12:20am meal counts for the day that STARTED
    yesterday morning (founder 2026-09-16: 'clean slate after I sleep'). Clamped 0–11 so a
    'day' can't run backwards or skip the actual day."""
    try:
        h = int(getattr(user, "day_reset_hour", 0) or 0)
    except (TypeError, ValueError):
        h = 0
    if h == EXPLICIT_MIDNIGHT:
        return 0
    if 1 <= h <= 11:
        return h
    return auto_day_reset_hour(user)


def day_reset_source(user) -> str:
    """'explicit' (they asked), 'auto' (derived from an after-midnight bedtime), or
    'default' (standard midnight day)."""
    try:
        h = int(getattr(user, "day_reset_hour", 0) or 0)
    except (TypeError, ValueError):
        h = 0
    if h == EXPLICIT_MIDNIGHT or 1 <= h <= 11:
        return "explicit"
    return "auto" if auto_day_reset_hour(user) else "default"


def _hour_label(h: int) -> str:
    return "midnight" if h == 0 else f"{h}am"


def describe_day_reset(user) -> str:
    """The prompt's one-line description of WHEN the running total resets: 'local
    midnight', or e.g. '4am local (their day runs 4am→4am: a 12:10am meal still counts for
    the day before — derived from their ~3:00 bedtime)'."""
    h = day_reset_hour(user)
    if h == 0:
        return "local midnight"
    src = day_reset_source(user)
    why = ""
    if src == "auto":
        hm = _strict_hhmm(getattr(user, "sleep_time", None))
        if hm:
            why = f" — derived from their ~{hm[0]}:{hm[1]:02d} bedtime"
    elif src == "explicit":
        why = " — they asked for it"
    lab = _hour_label(h)
    return (f"{lab} local (their day runs {lab}→{lab}: a 12:10am meal still counts for the "
            f"day before{why})")


def local_day_bounds(user, *, now: datetime = None) -> tuple[datetime, datetime]:
    """[start, end) of the user's LOCAL nutrition day, as NAIVE UTC — the window every
    'today' reader shares (meals, events, totals). Rolls at the user's day_reset_hour
    (default 0 = midnight). `now` is an optional aware/naive-UTC reference instant
    (defaults to real now); useful for tests and for a fixed clock."""
    tz = resolve_tz(user)
    ref = _as_aware_utc(now) if now else datetime.now(timezone.utc)
    ref_local = ref.astimezone(tz)
    reset_h = day_reset_hour(user)
    start_local = ref_local.replace(hour=reset_h, minute=0, second=0, microsecond=0)
    if ref_local.hour < reset_h:
        # before today's rollover — we're still in the window that started yesterday
        start_local -= timedelta(days=1)
    start = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end = (start_local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
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
