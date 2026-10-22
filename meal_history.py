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
# Name-anchored repeat matching ("same overnight oats") — every content token of the
# NAME must sit inside the prior's description. Jaccard can't do this: the user says
# two words, the logged row lists seven ingredients (live 2026-10-22, user 51: 'same
# overnight oats but with strawberries instead of blueberries' vs 'overnight oats w/
# milk, blueberries, honey + scoop vanilla whey' = 0.2 → no match → the whey was lost).
CONTAIN_THRESHOLD = 1.0
CONTAIN_THRESHOLD_LONG = 0.75   # a 4+-token name may miss one token
SHORT_NAME_MAX_TOKENS = 3
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


def containment(query: frozenset, tokens: frozenset) -> float:
    """Share of the QUERY's tokens present in the prior — 'overnight oats' ⊂ 'overnight
    oats w/ milk, blueberries, honey + whey' scores 1.0 where Jaccard scores 0.2."""
    if not query or not tokens:
        return 0.0
    return len(query & tokens) / len(query)


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
    matches = _grouped_matches(user_id, query, jaccard, threshold, days=days, session=session)
    if not matches and len(query) in (2, SHORT_NAME_MAX_TOKENS):
        # A short name that Jaccard can't reach ("overnight oats" vs an ingredient-listed
        # prior) — fall through to containment. Never for one word ("chicken" would match
        # every chicken dish); that path exists only behind an explicit "same/usual"
        # (match_repeat_meal).
        matches = _grouped_matches(user_id, query, containment, CONTAIN_THRESHOLD,
                                   days=days, session=session, rank_by_count=True)
    return matches[:MAX_MATCHES]


def match_repeat_meal(user_id: int, name: str, *, days: int = HISTORY_DAYS,
                      session=None) -> list[dict]:
    """The user said this is the SAME meal as before and named it — find the prior by
    name containment, ranked by how often they've logged it (their 'usual'), then
    recency. One word is allowed here because the user asserted the repeat. Empty list
    when nothing in their history carries the name — the caller stays silent."""
    query = normalize_tokens(name)
    if not query:
        return []
    thr = CONTAIN_THRESHOLD if len(query) <= SHORT_NAME_MAX_TOKENS else CONTAIN_THRESHOLD_LONG
    matches = _grouped_matches(user_id, query, containment, thr, days=days, session=session,
                               rank_by_count=True)
    return matches[:MAX_MATCHES]


def _grouped_matches(user_id: int, query: frozenset, scorer, threshold: float, *,
                     days: int, session=None, rank_by_count: bool = False) -> list[dict]:
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
            score = scorer(query, tokens)
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
    if rank_by_count:
        matches.sort(key=lambda m: (-m["score"], -m["count"],
                                    -(m["last_eaten_at"].timestamp() if m["last_eaten_at"] else 0)))
    else:
        matches.sort(key=lambda m: (-m["score"], -m["count"]))
    return matches
