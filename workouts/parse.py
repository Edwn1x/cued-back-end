"""
parse_set_text — the terse forms a person actually types mid-set.

  190 x4 · 190x4 · 190 for 4 · bench 190 x4 · set 3 190x4 · did 4 at 190 ·
  only got 3 · skipped incline · done / that's it / finished

Returns a SetUpdate (kind: set | skip | close) or None when it isn't a set —
the normal coaching turn proceeds. Targets the next undone set of the exercise
named, else of the exercise most recently referenced (last done set), else the
first exercise with an undone set.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from workouts.templates import slug_for_name

CLOSE_RE = re.compile(r"^\s*(done|finished|finish|that'?s it|thats it|all done|im done|i'?m done|wrapped|wrapped up)\W*$", re.I)
SKIP_RE = re.compile(r"^\s*(skip(?:ped|ping)?|no|dropped)\s+(.+?)\W*$", re.I)
SET_IDX_RE = re.compile(r"\bset\s*(\d+)\b", re.I)
W_X_R = re.compile(r"(\d+(?:\.\d+)?)\s*(?:x|×|\*|for)\s*(\d+)\b", re.I)          # 190x4 · 190 x4 · 190 for 4
R_AT_W = re.compile(r"(?:did|got|hit)?\s*(\d+)\s*(?:at|@)\s*(\d+(?:\.\d+)?)\b", re.I)  # did 4 at 190
ONLY_R = re.compile(r"^\s*(?:only\s+)?(?:got|did|hit|managed)\s+(\d+)\W*$", re.I)     # only got 3
LB_R = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lb|lbs)\b", re.I)


@dataclass
class SetUpdate:
    kind: str                      # "set" | "skip" | "close"
    exercise: str | None = None    # slug when named
    set_index: int | None = None   # 0-based when "set 3" was said
    weight: float | None = None
    reps: int | None = None
    raw: str = ""


def _exercise_in(text: str, known: list[tuple[str, str]]) -> str | None:
    """Slug of an exercise mentioned in text: session labels first (exact-ish), then
    the template hints."""
    t = text.lower()
    for slug, label in known:
        if label and label.lower() in t:
            return slug
        head = (label or "").lower().split(" ")[0]
        if head and len(head) >= 4 and re.search(rf"\b{re.escape(head)}\b", t):
            return slug
    return slug_for_name(text)


def parse_set_text(text: str, session_exercises: list[tuple[str, str]] | None = None) -> SetUpdate | None:
    """`session_exercises`: [(slug, label)] of the active session, for name matching."""
    if not text or len(text) > 120:
        return None
    t = text.strip()
    known = session_exercises or []
    if CLOSE_RE.match(t):
        return SetUpdate("close", raw=t)
    m = SKIP_RE.match(t)
    if m:
        ex = _exercise_in(m.group(2), known)
        if ex:
            return SetUpdate("skip", exercise=ex, raw=t)
        return None
    ex = _exercise_in(t, known)
    idx = SET_IDX_RE.search(t)
    set_index = int(idx.group(1)) - 1 if idx else None
    body = SET_IDX_RE.sub(" ", t)
    m = W_X_R.search(body)
    if m:
        return SetUpdate("set", exercise=ex, set_index=set_index, weight=float(m.group(1)), reps=int(m.group(2)), raw=t)
    m = R_AT_W.search(body)
    if m:
        return SetUpdate("set", exercise=ex, set_index=set_index, weight=float(m.group(2)), reps=int(m.group(1)), raw=t)
    m = ONLY_R.match(body)
    if m:
        return SetUpdate("set", exercise=ex, set_index=set_index, weight=None, reps=int(m.group(1)), raw=t)
    # "bench 190" / "190 lb" with no reps → not enough to log a set
    return None
