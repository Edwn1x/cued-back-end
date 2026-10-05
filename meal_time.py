"""
Meal eaten_at hints → a clock instant, resolved in CODE (the model never computes times).

Live 2026-10-03 02:32 PT: "also ate a Rice Krispie before my run earlier" (the run was
logged 01:33) → log_meal wrote the row at 02:32 (now) and put the timing into the
DESCRIPTION ("rice krispie treat (pre-run)"). Day totals were right; intra-day timing was
wrong — the meal-gap signal, pre/post-workout nutrition reads and "what did I eat before
the gym" all saw it at 02:32. Explicit past DAYS were already handled (`date` on
log_meal / manage_log); past TIMES within the day were not.

The model passes the user's own words (or a local 'HH:MM') as `eaten_at_hint`; this
module turns it into a naive-UTC `eaten_at` on the right LOCAL nutrition day. The grammar
(case-insensitive, 'at/around/about/~/ish' ignored):

  clock      '13:00', '1am', '2:30pm', 'at 1' (bare hour → the latest reading ≤ now),
             'noon', 'midnight'
  workout    'before my run|workout|lift|gym|session…' → most recent workout's start
             − MEAL_PRE_WORKOUT_OFFSET_MIN; 'after my …' → its finish
             + MEAL_POST_WORKOUT_OFFSET_MIN (start + 60 when it has no finish). The
             anchor is a WorkoutSession (started_at/finished_at) or a legacy Workout row
             (date; + duration_min when logged) that started today or within the last
             MEAL_WORKOUT_ANCHOR_LOOKBACK_HOURS. No anchor → 'earlier' (now − 60).
  relative   'an hour ago', '2 hours ago', '30 min ago', 'half an hour ago', 'a couple
             hours ago', 'an hour and a half ago'; 'earlier' / 'a bit ago' / 'a while
             ago' → now − 60; 'just now' → now
  named      'this morning'/'breakfast' → 09:00; 'brunch' → 11:00; 'lunch'/'midday' →
             12:30; 'afternoon' → 15:00; 'dinner'/'evening'/'tonight' → 19:00; 'last
             night'/'night' → 21:00; 'before bed'/'late night' → 23:00; 'when I woke up'
             → the user's wake_time (08:00 if unset)
  day prefix 'last night' / 'yesterday …' → the PREVIOUS local day (only when the caller
             passed no explicit day); 'yesterday' alone → noon yesterday

Guarantees: the result never leaves the reference nutrition day (clamped to the day's
window — a 00:30 'an hour ago' lands at the day start, not yesterday); on today it never
runs ahead of now (a clock form tolerates +30 min of skew, everything else is ≤ now).
Unrecognized → `when is None` plus a note so the coach can ask for a clock time.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

import config

logger = logging.getLogger(__name__)


@dataclass
class HintResult:
    when: datetime | None          # naive UTC; None when the hint wasn't understood
    day: date | None               # the LOCAL nutrition day `when` sits on
    kind: str                      # clock | workout | relative | named | unrecognized
    implied_prev_day: bool = False  # the hint itself said 'last night' / 'yesterday'
    note: str | None = None        # a tool-result note for the coach (unrecognized / no anchor)
    local_hm: str | None = None    # 'HH:MM' in the user's tz, for the tool result


_FILLER_RE = re.compile(
    r"\b(at|around|about|roughly|like|maybe|sometime|sometimes|probably|i think|ish|"
    r"today|this|the|my|our)\b|~")
_PREV_DAY_RE = re.compile(r"\b(last\s*night|yesterday|yday)\b")
_WORKOUT_WORD = (r"(?:run|running|jog|jogging|workout|work\s*out|lift|lifting|lifts|gym|"
                 r"session|training|train|practice|ride|bike|biking|cycling|swim|swimming|"
                 r"cardio|game|match|walk|hike|exercise|exercising)")
_PRE_WORKOUT_RE = re.compile(r"\b(?:before|pre|prior\s+to|ahead\s+of)\b.*?\b" + _WORKOUT_WORD + r"\b")
_POST_WORKOUT_RE = re.compile(r"\b(?:after|post|following)\b.*?\b" + _WORKOUT_WORD + r"\b")

_NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
              "six": 6, "couple": 2, "couple of": 2, "a couple": 2, "a couple of": 2,
              "few": 3, "a few": 3, "half": 0.5, "half an": 0.5, "half a": 0.5}
_DELTA_RE = re.compile(
    r"\b(?P<n>\d+(?:\.\d+)?|a couple of|a couple|couple of|couple|a few|few|half an|half a|"
    r"half|an|a|one|two|three|four|five|six)\s*(?P<half>and a half\s*)?"
    r"(?P<unit>hours?|hrs?|h|minutes?|mins?|m)\b(?:\s*and a half)?\s*(?:ago|back|earlier|before)\b")
_HALF_AFTER_RE = re.compile(r"\band a half\b")
_EARLIER_RE = re.compile(
    r"^(?:earlier|a bit ago|a bit earlier|a little bit ago|a little earlier|a while ago|"
    r"a little while ago|some time ago|a while back|bit ago|while ago|earlier on)$")
_JUST_NOW_RE = re.compile(r"^(?:just now|now|right now|a minute ago|a min ago)$")
_CLOCK_RE = re.compile(r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)\b")
_CLOCK_24_RE = re.compile(r"\b(?P<h>\d{1,2}):(?P<m>\d{2})\b")
_BARE_HOUR_RE = re.compile(r"^(?P<h>\d{1,2})$")

# named windows → local time (searched, so 'this morning around breakfast' still lands)
_NAMED = (
    (re.compile(r"\b(?:before|pre)\s*[- ]?\s*(?:bed|bedtime|sleep|sleeping|i slept)\b|\bbedtime\b|\blate\s*night\b|\blate\b$"), time(23, 0)),
    (re.compile(r"\b(?:woke|wake|waking|got up|getting up|first thing)\b"), "wake"),
    (re.compile(r"\bbrunch\b"), time(11, 0)),
    (re.compile(r"\b(?:morning|breakfast|brekkie|bfast)\b"), time(9, 0)),
    (re.compile(r"\b(?:lunch|lunchtime|midday|mid-day)\b"), time(12, 30)),
    (re.compile(r"\bnoon\b"), time(12, 0)),
    (re.compile(r"\bmidnight\b"), time(0, 0)),
    (re.compile(r"\bafternoon\b"), time(15, 0)),
    (re.compile(r"\b(?:dinner|dinnertime|supper|evening|tonight)\b"), time(19, 0)),
    (re.compile(r"\bnight\b"), time(21, 0)),
)


def _norm(hint: str) -> str:
    h = (hint or "").strip().lower()
    h = h.replace("’", "'").replace("-", " ")
    h = re.sub(r"(\d)\s*(am|pm)\b", r"\1\2", h)       # '2 pm' → '2pm'
    h = re.sub(r"\b(\d{1,2})\s*ish\b", r"\1", h)       # '2ish' → '2'
    h = _FILLER_RE.sub(" ", h)
    h = re.sub(r"[.,!?]+$", "", h)
    return re.sub(r"\s+", " ", h).strip()


def _wake_time(user) -> time:
    raw = (getattr(user, "wake_time", None) or "").strip()
    m = re.match(r"^(\d{1,2}):(\d{2})", raw)
    if m:
        try:
            return time(int(m.group(1)) % 24, int(m.group(2)) % 60)
        except ValueError:
            pass
    return time(8, 0)


def parse_hint(hint: str, user=None):
    """The hint's INTENT, no clock yet: (kind, payload, implied_prev_day) or None.
    kind ∈ clock (payload=time | ('bare', hour)) | workout ('pre'|'post') |
    relative (minutes back) | named (time)."""
    h = _norm(hint)
    if not h:
        return None
    prev = bool(_PREV_DAY_RE.search(h))
    if prev:
        bare_last_night = bool(re.fullmatch(r"last\s*night", h))
        h = _PREV_DAY_RE.sub(" ", h)
        h = re.sub(r"\s+", " ", h).strip()
        if bare_last_night:
            return ("named", time(21, 0), True)
        if not h or h in ("at", "on"):
            return ("named", time(12, 0), True)       # 'yesterday' alone → noon yesterday
        if h in ("late",):
            return ("named", time(23, 0), True)

    if _PRE_WORKOUT_RE.search(h):
        return ("workout", "pre", prev)
    if _POST_WORKOUT_RE.search(h):
        return ("workout", "post", prev)

    m = _DELTA_RE.search(h)
    if m:
        n_raw = m.group("n")
        try:
            n = float(n_raw)
        except ValueError:
            n = float(_NUM_WORDS.get(n_raw, 1))
        unit = m.group("unit")
        if m.group("half") or _HALF_AFTER_RE.search(h):
            n += 0.5
        minutes = n * 60 if unit.startswith("h") else n
        return ("relative", int(round(minutes)), prev)
    if _EARLIER_RE.match(h):
        return ("relative", 60, prev)
    if _JUST_NOW_RE.match(h):
        return ("relative", 0, prev)

    m = _CLOCK_RE.search(h)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m") or 0)
        ap = m.group("ap").replace(".", "")
        if hh == 12:
            hh = 0
        if ap == "pm":
            hh += 12
        if 0 <= hh < 24 and 0 <= mm < 60:
            return ("clock", time(hh, mm), prev)
        return None
    m = _CLOCK_24_RE.search(h)
    if m:
        hh, mm = int(m.group("h")), int(m.group("m"))
        if 0 <= hh < 24 and 0 <= mm < 60:
            if hh > 12 or hh == 0:
                return ("clock", time(hh, mm), prev)
            return ("clock", ("bare", hh, mm), prev)   # '1:30' — am or pm, the latest ≤ now
        return None
    m = _BARE_HOUR_RE.match(h)
    if m:
        hh = int(m.group("h"))
        if 0 <= hh < 24:
            if hh > 12 or hh == 0:
                return ("clock", time(hh, 0), prev)
            return ("clock", ("bare", hh, 0), prev)
        return None

    for rx, t in _NAMED:
        if rx.search(h):
            return ("named", _wake_time(user) if t == "wake" else t, prev)
    return None


def _workout_anchor(user_id: int, window_start: datetime, upper: datetime, now_utc: datetime):
    """(start, finish|None) of the most recent workout that started inside the reference
    day's window (and not after `upper`) or within the lookback hours before now — a
    WorkoutSession (the card) or a legacy Workout row (text-logged run/lift)."""
    from models import get_session, Workout, WorkoutSession, active
    lookback = now_utc - timedelta(hours=config.MEAL_WORKOUT_ANCHOR_LOOKBACK_HOURS)
    lo = min(window_start, lookback)
    best = None
    session = get_session()
    try:
        for ws in (session.query(WorkoutSession)
                   .filter(WorkoutSession.user_id == user_id,
                           WorkoutSession.status.in_(("active", "done")),
                           WorkoutSession.started_at.isnot(None),
                           WorkoutSession.started_at >= lo,
                           WorkoutSession.started_at <= upper).all()):
            start, finish = ws.started_at, ws.finished_at
            if best is None or start > best[0]:
                best = (start, finish)
        for w in (active(session, Workout, user_id=user_id)
                  .filter(Workout.date.isnot(None), Workout.date >= lo, Workout.date <= upper).all()):
            dur = 0
            for ex in (w.exercises or []):
                try:
                    dur += float((ex or {}).get("duration_min") or 0)
                except (TypeError, ValueError, AttributeError):
                    pass
            finish = w.date + timedelta(minutes=dur) if dur > 0 else None
            if best is None or w.date > best[0]:
                best = (w.date, finish)
    finally:
        session.close()
    return best


def resolve_eaten_at_hint(user, hint: str, *, now_utc: datetime, ref_day: date | None = None) -> HintResult:
    """Resolve `hint` for `user` into a naive-UTC eaten_at.

    `now_utc` — naive UTC 'now' (callers pin it in tests).
    `ref_day` — the LOCAL nutrition day the meal belongs to when the caller already knows
    it (an explicit `date`, or the row being edited). None → today, or yesterday when the
    hint itself says so ('last night', 'yesterday …')."""
    from timefmt import local_day_bounds, resolve_tz
    tz = resolve_tz(user)
    parsed = parse_hint(hint, user)
    if parsed is None:
        return HintResult(when=None, day=None, kind="unrecognized",
                          note=f"couldn't read the time '{hint}' — logged as now; say a clock time to fix")
    kind, payload, prev = parsed

    now_aware = now_utc.replace(tzinfo=timezone.utc)
    today_start, _ = local_day_bounds(user, now=now_utc)
    today_local = today_start.replace(tzinfo=timezone.utc).astimezone(tz).date()
    if ref_day is None:
        ref_day = today_local - timedelta(days=1) if prev else today_local
    noon = datetime(ref_day.year, ref_day.month, ref_day.day, 12, 0, tzinfo=tz)
    w_start, w_end = local_day_bounds(user, now=noon)       # naive UTC window of the ref day
    is_today = w_start <= now_utc < w_end
    upper = min(now_utc, w_end - timedelta(minutes=1)) if is_today else w_end - timedelta(minutes=1)
    tolerance = timedelta(0)
    note = None

    def _on_day(t: time) -> datetime:
        local = datetime.combine(ref_day, t, tzinfo=tz)
        cand = local.astimezone(timezone.utc).replace(tzinfo=None)
        if cand < w_start:            # before the day's reset hour → it's the small hours of the NEXT date
            cand = (local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        return cand

    if kind == "clock":
        tolerance = timedelta(minutes=30)
        if isinstance(payload, tuple):            # bare hour: am or pm, whichever is the latest ≤ upper
            _b, hh, mm = payload
            cands = sorted({_on_day(time(hh % 12, mm)), _on_day(time((hh % 12) + 12, mm))})
            fitting = [c for c in cands if c <= upper + tolerance]
            when = fitting[-1] if fitting else cands[0]
        else:
            when = _on_day(payload)
    elif kind == "named":
        when = _on_day(payload)
    elif kind == "relative":
        if not is_today:
            # 'an hour ago' only means something relative to now — on a past day it's noise
            return HintResult(when=None, day=None, kind="unrecognized",
                              note=(f"'{hint}' only works for today — kept the default time on "
                                    f"{ref_day.isoformat()}; say a clock time to fix"))
        when = now_utc - timedelta(minutes=payload)
    else:  # workout
        anchor = _workout_anchor(user.id, w_start, upper, now_utc)
        if anchor is None:
            when = now_utc - timedelta(minutes=60)
            note = (f"no logged workout found to anchor '{hint}' — logged as ~1h ago; "
                    "say a clock time to fix")
        else:
            start, finish = anchor
            if payload == "pre":
                when = start - timedelta(minutes=config.MEAL_PRE_WORKOUT_OFFSET_MIN)
            elif finish is not None:
                when = finish + timedelta(minutes=config.MEAL_POST_WORKOUT_OFFSET_MIN)
            else:
                when = start + timedelta(minutes=60)

    # Never leave the reference day; never run ahead of now (beyond the clock tolerance).
    if when < w_start:
        when = w_start
    if when >= w_end:
        when = w_end - timedelta(minutes=1)
    if is_today and when > now_utc + tolerance:
        when = now_utc
    local_hm = when.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%H:%M")
    day_start, _ = local_day_bounds(user, now=when)
    day = day_start.replace(tzinfo=timezone.utc).astimezone(tz).date()
    return HintResult(when=when, day=day, kind=kind, implied_prev_day=prev, note=note, local_hm=local_hm)
