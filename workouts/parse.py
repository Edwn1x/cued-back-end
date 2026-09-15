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
ONLY_R = re.compile(r"^\s*(?:only|just|barely)\s+(?:got|did|hit|made|managed\s+)?\s*(\d+)\W*$", re.I)  # only got 3 (a MISS at planned weight)
LB_R = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lb|lbs)\b", re.I)
# structured multi-set forms (capture weight, sets, reps) — the sentences a person types when
# they narrate a whole exercise, not a single terse deviation.
W_S_R = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lbs?)?\s*(?:for|x|×|@)?\s*(\d+)\s*sets?\s*(?:of|x|×|@|for)?\s*(\d+)\s*(?:reps?)?", re.I)  # 135 for 3 sets 7 reps / 3 sets of 8
S_R_AT_W = re.compile(r"(\d+)\s*(?:sets?\s*(?:of\s*)?|x|×)\s*(\d+)\s*(?:reps?\s*)?(?:at|@)\s*(\d+(?:\.\d+)?)", re.I)  # 3 sets of 7 at 135 / 3x7 at 135
W_X_S_X_R = re.compile(r"(\d+(?:\.\d+)?)\s*[x×]\s*(\d+)\s*[x×]\s*(\d+)\b", re.I)  # 135x3x8 (weight × sets × reps)
SETS_OR_REPS_WORD = re.compile(r"\b(?:sets?|reps?)\b", re.I)


@dataclass
class SetUpdate:
    kind: str                      # "set" | "skip" | "close"
    exercise: str | None = None    # slug when named
    set_index: int | None = None   # 0-based when "set 3" was said
    weight: float | None = None
    reps: int | None = None
    sets: int = 1                  # >1 for a structured multi-set report ("135 for 3 sets 7 reps")
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

    # Structured multi-set forms FIRST (weight, sets, reps), so "135 for 3 sets 7 reps"
    # is never misread by the greedy terse WxR as "135 for 3".
    m = W_X_S_X_R.search(body)
    if m:
        return SetUpdate("set", exercise=ex, set_index=set_index, weight=float(m.group(1)), sets=int(m.group(2)), reps=int(m.group(3)), raw=t)
    m = W_S_R.search(body)
    if m:
        return SetUpdate("set", exercise=ex, set_index=set_index, weight=float(m.group(1)), sets=int(m.group(2)), reps=int(m.group(3)), raw=t)
    m = S_R_AT_W.search(body)
    if m:
        return SetUpdate("set", exercise=ex, set_index=set_index, weight=float(m.group(3)), sets=int(m.group(1)), reps=int(m.group(2)), raw=t)

    # A bare "set(s)"/"rep(s)" word that didn't match a structured form is a whole-exercise
    # narration the terse parser can't safely structure ("Got 7 for my second set", "3 sets
    # today"). Hand it to the model, which has the conversational weight — never guess.
    if SETS_OR_REPS_WORD.search(body):
        return None

    m = W_X_R.search(body)
    if m:
        return SetUpdate("set", exercise=ex, set_index=set_index, weight=float(m.group(1)), reps=int(m.group(2)), raw=t)
    m = R_AT_W.search(body)
    if m:
        return SetUpdate("set", exercise=ex, set_index=set_index, weight=float(m.group(2)), reps=int(m.group(1)), raw=t)
    m = ONLY_R.match(body)
    if m:
        # A MISS at the planned weight ("only got 3"). Plain "got 5" is NOT here — its weight
        # was set in conversation, which code can't see, so the model logs it instead.
        return SetUpdate("set", exercise=ex, set_index=set_index, weight=None, reps=int(m.group(1)), raw=t)
    return None
