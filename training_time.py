"""
Training time, per day (founder, 2026-10-09, change 6): "we should make that option
multiple selections since users can have different preferences for different days."

The form may now send `workout_time` (or `workout_times`) as
  - one value              "evening" / "17:30"                 → as before
  - a list of values       ["morning", "evening"]              → slots: they lift at either
  - a day → value map      {"mon": "morning", "tue": "17:30"}  → by_day: a time per day

users.workout_time keeps ONE primary clock (the most common / the first) so every older
reader still works; users.workout_times holds the structure:
  {"slots": ["08:00", "18:00"]}  or  {"by_day": {"mon": "08:00", "tue": "17:30"}}
Readers that care about the day go through slot_for(user, "mon"); prose goes through
describe(user).
"""

from __future__ import annotations

import re
from collections import Counter

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
TIME_WORDS = {"morning": "08:00", "afternoon": "14:00", "evening": "18:00", "night": "20:00"}
_WORD_FOR = {"08:00": "mornings", "14:00": "afternoons", "18:00": "evenings", "20:00": "nights"}
_DAY_ALIASES = {"monday": "mon", "tuesday": "tue", "wednesday": "wed", "thursday": "thu", "friday": "fri",
                "saturday": "sat", "sunday": "sun", "tues": "tue", "weds": "wed", "thur": "thu", "thurs": "thu"}


def normalize_time(value) -> str | None:
    """'morning' → '08:00'; '5:30' pm-less clock → '05:30' (as typed); invalid → None."""
    t = str(value or "").strip().lower()
    if not t:
        return None
    if t in TIME_WORDS:
        return TIME_WORDS[t]
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m or not (0 <= int(m.group(1)) <= 23 and 0 <= int(m.group(2)) <= 59):
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def normalize_day(value) -> str | None:
    d = str(value or "").strip().lower()
    d = _DAY_ALIASES.get(d, d[:3])
    return d if d in DAYS else None


def parse_form_value(value) -> tuple[str | None, dict | None] | None:
    """→ (primary HH:MM, structure or None); None when any part is invalid."""
    if isinstance(value, dict):
        by_day = {}
        for k, v in value.items():
            d, t = normalize_day(k), normalize_time(v)
            if not d or not t:
                return None
            by_day[d] = t
        if not by_day:
            return None
        primary = Counter(by_day[d] for d in DAYS if d in by_day).most_common(1)[0][0]
        if len(set(by_day.values())) == 1:
            return primary, None              # one time every day: just the primary
        return primary, {"by_day": by_day}
    if isinstance(value, (list, tuple)):
        slots = []
        for v in value:
            t = normalize_time(v)
            if not t:
                return None
            if t not in slots:
                slots.append(t)
        if not slots:
            return None
        if len(slots) == 1:
            return slots[0], None
        return slots[0], {"slots": slots}
    t = normalize_time(value)
    return (t, None) if t else None


def _clock(hhmm: str) -> str:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm or "")
    if not m:
        return hhmm or ""
    h, mm = int(m.group(1)), int(m.group(2))
    suffix = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}{suffix}" if mm == 0 else f"{h12}:{mm:02d}{suffix}"


def _word(hhmm: str, *, plural: bool = True) -> str:
    w = _WORD_FOR.get(hhmm)
    if w:
        return w if plural else w[:-1]
    return _clock(hhmm)


def slot_for(user, day: str | None = None) -> str | None:
    """The HH:MM (or phrase) they train on `day` ('mon'); the primary when no per-day."""
    wt = getattr(user, "workout_times", None) or {}
    if day and isinstance(wt.get("by_day"), dict) and wt["by_day"].get(day):
        return wt["by_day"][day]
    return getattr(user, "confirmed_workout_time", None) or getattr(user, "workout_time", None)


def describe(user) -> str:
    """Prose for the summary / profile: 'in the evenings' | 'mornings or evenings' |
    'mornings mon/wed, evenings tue/thu' | 'at 5:30pm'. '' when unknown."""
    wt = getattr(user, "workout_times", None) or {}
    by_day = wt.get("by_day") if isinstance(wt.get("by_day"), dict) else None
    if by_day:
        groups: dict[str, list[str]] = {}
        for d in DAYS:
            if d in by_day:
                groups.setdefault(by_day[d], []).append(d)
        return ", ".join(f"{_word(t)} {'/'.join(ds)}" for t, ds in groups.items())
    slots = wt.get("slots") if isinstance(wt.get("slots"), list) else None
    if slots and len(slots) > 1:
        return " or ".join(_word(t) for t in slots)
    t = slot_for(user)
    if not t:
        return ""
    w = _WORD_FOR.get(t)
    if w:
        return f"in the {w}"
    if re.fullmatch(r"\d{1,2}:\d{2}", t):
        return f"at {_clock(t)}"
    return t
