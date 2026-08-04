"""
Meal-history matcher — macro-accuracy Phase B (the user-history moat source).
=============================================================================
"Has this user logged something like this before?" answered deterministically in
code; the model judges whether the surfaced prior fits THIS serving. Descriptions
are free text with no repeatable phrasing (and Phase A adds portion annotations),
so matching is token-set Jaccard over normalized content words — calibrated so
"chicken and rice bowl" finds "chicken and rice" but "chicken and quinoa" does NOT
(the dangerous direction is too-loose: a wrong prior silently applied). See
rewrite/macro-accuracy/INVESTIGATION.md §2.
"""

from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone

from models import get_session, Meal, active

# Words that describe amount/size, not the food itself — Phase A deliberately puts
# portions in descriptions ("chicken breast ~6oz, rice ~2 cups"); they must not
# defeat repeat detection.
_UNIT_WORDS = {
    "oz", "ounce", "ounces", "g", "gram", "grams", "kg", "lb", "lbs", "pound",
    "pounds", "cup", "cups", "tbsp", "tsp", "ml", "l", "liter", "serving",
    "servings", "slice", "slices", "piece", "pieces", "scoop", "scoops",
    "small", "medium", "large", "half", "quarter", "whole",
}
_STOPWORDS = {
    "a", "an", "the", "of", "with", "and", "or", "on", "in", "at", "to", "for",
    "from", "my", "some", "plus", "w", "about", "roughly", "around", "like",
}

_TOKEN_RE = re.compile(r"[a-z]+")

MATCH_THRESHOLD = 0.6
HISTORY_DAYS = 120
HISTORY_ROW_CAP = 400
MAX_MATCHES = 3


def normalize_tokens(description: str) -> frozenset[str]:
    """Free text -> content-token set. Numbers, units, and stopwords drop out;
    a light plural fold ('tenders'/'tender') is applied to both sides equally.
    Public: Phase C matches dining-menu item names with the same normalization."""
    tokens = _TOKEN_RE.findall((description or "").lower())
    out = set()
    for t in tokens:
        if t in _UNIT_WORDS or t in _STOPWORDS:
            continue
        if len(t) > 3 and t.endswith("s"):
            t = t[:-1]
        out.add(t)
    return frozenset(out)


def jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _median_int(values: list[int]):
    if not values:
        return None
    return int(round(statistics.median(values)))


def match_meal_history(user_id: int, description: str, *, days: int = HISTORY_DAYS,
                       threshold: float = MATCH_THRESHOLD, session=None) -> list[dict]:
    """Top matches from THIS user's active meal history for a new description.

    Each match is one repeat-group: {description (most recent phrasing), count,
    last_eaten_at, calories, protein_g, carbs_g, fat_g (medians over macro-bearing
    rows; None when no row carried the number), score}. Empty list when there's no
    confident match — the caller falls through to the next estimation rung, never
    to a guess.
    """
    query = normalize_tokens(description)
    if not query:
        return []

    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    own = session is None
    s = session or get_session()
    try:
        rows = (active(s, Meal, user_id=user_id)
                .filter(Meal.eaten_at >= cutoff)
                .order_by(Meal.eaten_at.desc())
                .limit(HISTORY_ROW_CAP).all())
        groups: dict[frozenset, dict] = {}
        for m in rows:
            tokens = normalize_tokens(m.description or "")
            score = jaccard(query, tokens)
            if score < threshold:
                continue
            g = groups.get(tokens)
            if g is None:
                g = groups[tokens] = {
                    "description": m.description, "last_eaten_at": m.eaten_at,
                    "score": score, "count": 0,
                    "_cal": [], "_pro": [], "_carb": [], "_fat": [],
                }
            g["count"] += 1
            if m.calories is not None:
                g["_cal"].append(m.calories)
            if m.protein_g is not None:
                g["_pro"].append(m.protein_g)
            if m.carbs_g is not None:
                g["_carb"].append(m.carbs_g)
            if m.fat_g is not None:
                g["_fat"].append(m.fat_g)
    finally:
        if own:
            s.close()

    matches = []
    for g in groups.values():
        matches.append({
            "description": g["description"], "count": g["count"],
            "last_eaten_at": g["last_eaten_at"], "score": g["score"],
            "calories": _median_int(g["_cal"]), "protein_g": _median_int(g["_pro"]),
            "carbs_g": _median_int(g["_carb"]), "fat_g": _median_int(g["_fat"]),
        })
    matches.sort(key=lambda m: (-m["score"], -m["count"]))
    return matches[:MAX_MATCHES]
