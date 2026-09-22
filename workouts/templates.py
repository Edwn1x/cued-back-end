"""
Session templates: what a day looks like with no history. Weights are the
template DEFAULTS (beginner-safe, plate-loadable); the plan overrides them from
the user's completed sets. `plate_step` is the smallest progression: 5 lb for
upper-body lifts, 10 lb for lower-body barbell lifts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExerciseTemplate:
    slug: str            # canonical, stable ("bench_press")
    label: str           # display ("bench press")
    sets: int
    reps: int
    default_weight: float
    plate_step: float    # 5 upper / 10 lower
    rep_step: int = 0    # bodyweight progression: +reps when every planned rep was hit


def _ex(slug, label, sets, reps, weight, step):
    return ExerciseTemplate(slug, label, sets, reps, float(weight), float(step))


def _bw(slug, label, sets, reps, rep_step=2):
    """A bodyweight movement: no load, no plate step; progression is reps."""
    return ExerciseTemplate(slug, label, sets, reps, 0.0, 0.0, rep_step)


TEMPLATES: dict[str, list[ExerciseTemplate]] = {
    "push": [
        _ex("bench_press", "bench press", 4, 5, 135, 5),
        _ex("incline_db_press", "incline db press", 3, 10, 40, 5),
        _ex("cable_fly", "cable fly", 3, 12, 20, 5),
        _ex("tricep_pushdown", "tricep pushdown", 3, 12, 40, 5),
    ],
    "pull": [
        _ex("deadlift", "deadlift", 3, 5, 185, 10),
        _ex("barbell_row", "barbell row", 4, 8, 115, 5),
        _ex("lat_pulldown", "lat pulldown", 3, 10, 100, 5),
        _ex("face_pull", "face pull", 3, 15, 30, 5),
        _ex("barbell_curl", "barbell curl", 3, 10, 45, 5),
    ],
    "legs": [
        _ex("squat", "squat", 4, 5, 155, 10),
        _ex("romanian_deadlift", "romanian deadlift", 3, 8, 135, 10),
        _ex("leg_press", "leg press", 3, 10, 180, 10),
        _ex("leg_curl", "leg curl", 3, 12, 70, 5),
        _ex("calf_raise", "calf raise", 3, 15, 90, 10),
    ],
    "upper": [
        _ex("bench_press", "bench press", 4, 5, 135, 5),
        _ex("barbell_row", "barbell row", 4, 8, 115, 5),
        _ex("overhead_press", "overhead press", 3, 8, 75, 5),
        _ex("lat_pulldown", "lat pulldown", 3, 10, 100, 5),
        _ex("barbell_curl", "barbell curl", 2, 10, 45, 5),
        _ex("tricep_pushdown", "tricep pushdown", 2, 12, 40, 5),
    ],
    "lower": [
        _ex("squat", "squat", 4, 5, 155, 10),
        _ex("romanian_deadlift", "romanian deadlift", 3, 8, 135, 10),
        _ex("leg_press", "leg press", 3, 10, 180, 10),
        _ex("leg_curl", "leg curl", 3, 12, 70, 5),
        _ex("calf_raise", "calf raise", 3, 15, 90, 10),
    ],
    "full_body": [
        _ex("squat", "squat", 3, 5, 155, 10),
        _ex("bench_press", "bench press", 3, 5, 135, 5),
        _ex("barbell_row", "barbell row", 3, 8, 115, 5),
        _ex("overhead_press", "overhead press", 2, 8, 75, 5),
        _ex("romanian_deadlift", "romanian deadlift", 2, 8, 135, 10),
    ],
}

# Bodyweight-only users (signup radio `equipment=bodyweight` — "in my room, no gym").
# Same split keys, so the split pointer / infer_template work unchanged; every set is
# reps-only (default_weight 0). Live 2026-09-14: a bodyweight 16-year-old got the
# barbell full-body card (squat/bench/row/OHP/RDL) and never touched it.
BODYWEIGHT_TEMPLATES: dict[str, list[ExerciseTemplate]] = {
    "push": [
        _bw("pushup", "pushup", 4, 10),
        _bw("pike_pushup", "pike pushup", 3, 8),
        _bw("chair_dip", "chair dip", 3, 10),
        _bw("diamond_pushup", "diamond pushup", 2, 8),
    ],
    "pull": [
        _bw("inverted_row", "inverted row (table or towel)", 4, 10),
        _bw("superman", "superman", 3, 12),
        _bw("reverse_snow_angel", "reverse snow angel", 3, 12),
        _bw("plank", "plank (sec)", 3, 30, 10),
    ],
    "legs": [
        _bw("air_squat", "air squat", 4, 15),
        _bw("reverse_lunge", "reverse lunge (each leg)", 3, 10),
        _bw("glute_bridge", "glute bridge", 3, 15),
        _bw("wall_sit", "wall sit (sec)", 3, 30, 10),
        _bw("bodyweight_calf_raise", "calf raise", 3, 20, 5),
    ],
    "upper": [
        _bw("pushup", "pushup", 4, 10),
        _bw("inverted_row", "inverted row (table or towel)", 4, 10),
        _bw("pike_pushup", "pike pushup", 3, 8),
        _bw("chair_dip", "chair dip", 3, 10),
    ],
    "lower": [
        _bw("air_squat", "air squat", 4, 15),
        _bw("reverse_lunge", "reverse lunge (each leg)", 3, 10),
        _bw("glute_bridge", "glute bridge", 3, 15),
        _bw("wall_sit", "wall sit (sec)", 3, 30, 10),
        _bw("bodyweight_calf_raise", "calf raise", 3, 20, 5),
    ],
    "full_body": [
        _bw("air_squat", "air squat", 3, 15),
        _bw("pushup", "pushup", 3, 10),
        _bw("reverse_lunge", "reverse lunge (each leg)", 3, 10),
        _bw("glute_bridge", "glute bridge", 3, 15),
        _bw("plank", "plank (sec)", 3, 30, 10),
    ],
}

BODYWEIGHT_EQUIPMENT = {"bodyweight", "none", "no_equipment"}


# ── Body-part days (bro splits) ──────────────────────────────────────────────
# Live 2026-09-22 (user 43): "chest and biceps, back and triceps, legs and shoulders"
# was stored as current_split="bro_split" — a value the onboarding extractor is
# allowed to write — and the card fell to full_body because nothing below the label
# knew what a bro split is. A day here is COMPOSED from parts: "chest_biceps" is the
# chest block then the biceps block, so any grouping a person names gets their card,
# not a guess. Part order in the key is the user's; part names are canonical.
PART_TEMPLATES: dict[str, list[ExerciseTemplate]] = {
    "chest": [
        _ex("bench_press", "bench press", 4, 5, 135, 5),
        _ex("incline_db_press", "incline db press", 3, 10, 40, 5),
        _ex("cable_fly", "cable fly", 3, 12, 20, 5),
        _ex("pec_deck", "pec deck", 3, 12, 60, 5),
    ],
    "back": [
        _ex("barbell_row", "barbell row", 4, 8, 115, 5),
        _ex("lat_pulldown", "lat pulldown", 3, 10, 100, 5),
        _ex("seated_cable_row", "seated cable row", 3, 10, 80, 5),
        _ex("face_pull", "face pull", 3, 15, 30, 5),
    ],
    "shoulders": [
        _ex("overhead_press", "overhead press", 3, 8, 75, 5),
        _ex("lateral_raise", "lateral raise", 3, 12, 10, 5),
        _ex("rear_delt_fly", "rear delt fly", 3, 15, 15, 5),
        _ex("front_raise", "front raise", 2, 12, 10, 5),
    ],
    "biceps": [
        _ex("barbell_curl", "barbell curl", 3, 10, 45, 5),
        _ex("hammer_curl", "hammer curl", 3, 10, 25, 5),
        _ex("preacher_curl", "preacher curl", 2, 12, 40, 5),
    ],
    "triceps": [
        _ex("tricep_pushdown", "tricep pushdown", 3, 12, 40, 5),
        _ex("skullcrusher", "skullcrusher", 3, 10, 40, 5),
        _ex("overhead_tricep_extension", "overhead tricep extension", 2, 12, 30, 5),
    ],
    "legs": [
        _ex("squat", "squat", 4, 5, 155, 10),
        _ex("romanian_deadlift", "romanian deadlift", 3, 8, 135, 10),
        _ex("leg_press", "leg press", 3, 10, 180, 10),
        _ex("leg_curl", "leg curl", 3, 12, 70, 5),
        _ex("calf_raise", "calf raise", 3, 15, 90, 10),
    ],
}
# "arms" is biceps + triceps; "core" rides along as a short finisher.
PART_TEMPLATES["arms"] = PART_TEMPLATES["biceps"][:2] + PART_TEMPLATES["triceps"][:2]
PART_TEMPLATES["core"] = [_bw("plank", "plank (sec)", 3, 30, 10), _bw("hanging_leg_raise", "hanging leg raise", 3, 10)]

BODYWEIGHT_PART_TEMPLATES: dict[str, list[ExerciseTemplate]] = {
    "chest": [_bw("pushup", "pushup", 4, 10), _bw("diamond_pushup", "diamond pushup", 3, 8), _bw("chair_dip", "chair dip", 3, 10)],
    "back": [_bw("inverted_row", "inverted row (table or towel)", 4, 10), _bw("superman", "superman", 3, 12),
             _bw("reverse_snow_angel", "reverse snow angel", 3, 12)],
    "shoulders": [_bw("pike_pushup", "pike pushup", 3, 8), _bw("reverse_snow_angel", "reverse snow angel", 3, 12),
                  _bw("wall_handstand_hold", "wall handstand hold (sec)", 3, 20, 10)],
    "biceps": [_bw("underhand_inverted_row", "underhand inverted row", 3, 10), _bw("towel_curl", "towel curl", 3, 12)],
    "triceps": [_bw("diamond_pushup", "diamond pushup", 3, 8), _bw("chair_dip", "chair dip", 3, 10)],
    "legs": BODYWEIGHT_TEMPLATES["legs"],
    "core": [_bw("plank", "plank (sec)", 3, 30, 10), _bw("lying_leg_raise", "lying leg raise", 3, 12)],
}
BODYWEIGHT_PART_TEMPLATES["arms"] = BODYWEIGHT_PART_TEMPLATES["biceps"][:1] + BODYWEIGHT_PART_TEMPLATES["triceps"]

# How people say the parts. Longest match first at parse time.
PART_ALIASES: dict[str, str] = {
    "chest": "chest", "pecs": "chest", "pec": "chest",
    "back": "back", "lats": "back",
    "shoulders": "shoulders", "shoulder": "shoulders", "delts": "shoulders", "delt": "shoulders",
    "biceps": "biceps", "bicep": "biceps", "bis": "biceps", "bi": "biceps",
    "triceps": "triceps", "tricep": "triceps", "tris": "triceps", "tri": "triceps",
    "arms": "arms", "arm": "arms",
    "legs": "legs", "leg": "legs", "quads": "legs", "hamstrings": "legs", "hams": "legs", "glutes": "legs",
    "core": "core", "abs": "core", "ab": "core",
}
# Per-part cap when a day is composed of several: the first part is the day's focus.
_COMPOSE_CAP = (4, 3, 2)


def is_composed_key(key: str | None) -> bool:
    """True for a body-part day ("chest_biceps", "arms"), False for a global key."""
    return bool(key) and key not in TEMPLATES and all(p in PART_TEMPLATES for p in key.split("_"))


def compose_day(key: str, *, bodyweight: bool = False) -> list[ExerciseTemplate]:
    """A body-part day's exercises: each part's block in the key's order, capped so a
    two-part day is ~7 movements, not 9. Duplicate slugs across parts are dropped."""
    src = BODYWEIGHT_PART_TEMPLATES if bodyweight else PART_TEMPLATES
    parts = key.split("_")
    out: list[ExerciseTemplate] = []
    seen: set[str] = set()
    for i, part in enumerate(parts):
        cap = _COMPOSE_CAP[min(i, len(_COMPOSE_CAP) - 1)] if len(parts) > 1 else 99
        for t in src.get(part, [])[:cap]:
            if t.slug not in seen:
                seen.add(t.slug)
                out.append(t)
    return out


def day_label(key: str | None) -> str:
    """Human form of a day key for captions/intros: "chest + biceps", "push", "full body"."""
    if not key:
        return "workout"
    if is_composed_key(key):
        return " + ".join(key.split("_"))
    return key.replace("_", " ")


_PART_WORD_RE = re.compile(r"[a-z]+")
# Day separators, strongest first. The strongest one present that yields ≥2 pieces
# wins, so "chest/bis, back/tris, legs" splits on commas (slash joins parts) while
# "push/pull/legs" splits on slashes.
_SEP_LEVELS = (r"\n|;|\bthen\b|→|->", r",", r"/|\|")
_LEAD_NOISE = {"and", "with", "plus", "day", "days", "n", "&", "i", "do", "run", "a", "my", "is", "the", "split"}


def day_key_from_phrase(phrase: str) -> str | None:
    """"chest and bis" → "chest_biceps"; "push" → "push"; junk → None. Global split
    names win ("push", "full body"); otherwise every word must be a body part."""
    words = _PART_WORD_RE.findall((phrase or "").lower())
    words = [w for w in words if w not in _LEAD_NOISE]
    if not words:
        return None
    joined = "_".join(words)
    if joined in ALIASES or joined in TEMPLATES:
        return normalize_template_key(joined)
    parts: list[str] = []
    for w in words:
        p = PART_ALIASES.get(w)
        if not p:
            return None
        if p not in parts:
            parts.append(p)
    return "_".join(parts)


def parse_day_list(text: str) -> list[str] | None:
    """A stated split — "chest and biceps, back and triceps, legs and shoulders",
    "push/pull/legs", "chest, back, shoulders, arms, legs" — as an ordered list of
    day keys, or None when any piece isn't a day (precision over recall: a wrong
    cycle steers every card). Needs at least two days."""
    if not text:
        return None
    pieces: list[str] = []
    for sep in _SEP_LEVELS:
        pieces = [p.strip() for p in re.split(sep, text, flags=re.IGNORECASE) if p and p.strip()]
        if len(pieces) >= 2:
            break
    if len(pieces) < 2:
        return None
    days: list[str] = []
    for piece in pieces:
        k = day_key_from_phrase(piece)
        if not k:
            sub = parse_day_list(piece) if any(c in piece for c in ",/|") else None
            if not sub:
                return None
            days.extend(sub)
            continue
        parts = k.split("_")
        # "back and triceps and legs and shoulders" (live, user 43): four+ parts joined
        # only by "and" are consecutive PAIRS, not one day. An odd run is ambiguous →
        # not guessed.
        if len(parts) >= 4 and piece.lower().count(" and ") >= len(parts) - 1:
            if len(parts) % 2:
                return None
            days.extend("_".join(parts[i:i + 2]) for i in range(0, len(parts), 2))
        else:
            days.append(k)
    return days if len(days) >= 2 else None


def _custom_template(entry) -> ExerciseTemplate | None:
    """One row of users.custom_templates → ExerciseTemplate, or None if malformed.
    Slugs are canonicalized (lowercase, underscores) so set-text updates and PR
    history match the card. Malformed rows are skipped, never raised: the card must
    still open for a user whose routine JSON has one bad line."""
    if not isinstance(entry, dict):
        return None
    slug = str(entry.get("slug") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not slug:
        return None
    try:
        sets = int(entry.get("sets") or 0)
        reps = int(entry.get("reps") or 0)
        weight = float(entry.get("default_weight") or 0)
        step = float(entry.get("plate_step") if entry.get("plate_step") is not None else 5)
        rep_step = int(entry.get("rep_step") or 0)
    except (TypeError, ValueError):
        return None
    if sets <= 0 or reps <= 0:
        return None
    label = str(entry.get("label") or slug.replace("_", " ")).strip()
    return ExerciseTemplate(slug, label, sets, reps, weight, step, rep_step)


def custom_templates_for(user) -> dict[str, list[ExerciseTemplate]]:
    """The user's own routine days from users.custom_templates, validated. Only known
    day keys (push/pull/legs/upper/lower/full_body or a body-part day such as
    chest_biceps) are honored; a day whose rows are all malformed is dropped so the
    global template still covers it."""
    raw = getattr(user, "custom_templates", None)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[ExerciseTemplate]] = {}
    for key, rows in raw.items():
        k = normalize_template_key(key)
        if not k or not isinstance(rows, list):
            continue
        exs = [t for t in (_custom_template(r) for r in rows) if t is not None]
        if exs:
            out[k] = exs
    return out


def templates_for(user) -> dict[str, list[ExerciseTemplate]]:
    """The template set for THIS user, by the typed equipment column (code decides,
    never the model): bodyweight → BODYWEIGHT_TEMPLATES, everything else → TEMPLATES.
    A user's own routine (users.custom_templates) overrides per split day — the
    card shows THEIR push day, not the generic one; days they didn't give fall back."""
    eq = (getattr(user, "equipment", None) or "").strip().lower()
    base = BODYWEIGHT_TEMPLATES if eq in BODYWEIGHT_EQUIPMENT else TEMPLATES
    custom = custom_templates_for(user)
    if not custom:
        return base
    return {**base, **custom}


def _all_templates():
    yield from TEMPLATES.values()
    yield from BODYWEIGHT_TEMPLATES.values()
    yield from PART_TEMPLATES.values()
    yield from BODYWEIGHT_PART_TEMPLATES.values()


# Aliases the split pointer / a user might use.
ALIASES = {"chest_back": "upper", "shoulders_arms": "upper", "leg": "legs", "fullbody": "full_body"}

# Loose name matching for legacy `Workout.exercises` rows and typed text ("bench",
# "incline", "flys"). First match wins; keys are lowercase substrings.
NAME_HINTS: list[tuple[str, str]] = [
    # bodyweight first — longer hints win in slug_for_name ("air squat" beats "squat",
    # "inverted row" beats "row")
    ("push up", "pushup"), ("pushup", "pushup"), ("push-up", "pushup"),
    ("pike", "pike_pushup"), ("diamond", "diamond_pushup"), ("dip", "chair_dip"),
    ("inverted row", "inverted_row"), ("superman", "superman"), ("snow angel", "reverse_snow_angel"),
    ("plank", "plank"), ("air squat", "air_squat"), ("lunge", "reverse_lunge"),
    ("glute bridge", "glute_bridge"), ("bridge", "glute_bridge"), ("wall sit", "wall_sit"),
    ("incline", "incline_db_press"), ("bench", "bench_press"), ("fly", "cable_fly"),
    ("pushdown", "tricep_pushdown"), ("tricep", "tricep_pushdown"),
    ("romanian", "romanian_deadlift"), ("rdl", "romanian_deadlift"), ("deadlift", "deadlift"),
    ("row", "barbell_row"), ("pulldown", "lat_pulldown"), ("pull down", "lat_pulldown"),
    ("face", "face_pull"), ("curl", "barbell_curl"), ("leg curl", "leg_curl"),
    ("squat", "squat"), ("leg press", "leg_press"), ("calf", "calf_raise"),
    ("ohp", "overhead_press"), ("overhead", "overhead_press"), ("shoulder press", "overhead_press"),
    # body-part day movements (PART_TEMPLATES) — longer hints beat "curl"/"overhead"/"row"
    ("hammer", "hammer_curl"), ("preacher", "preacher_curl"), ("lateral", "lateral_raise"),
    ("rear delt", "rear_delt_fly"), ("front raise", "front_raise"), ("skull", "skullcrusher"),
    ("pec deck", "pec_deck"), ("seated row", "seated_cable_row"), ("cable row", "seated_cable_row"),
    ("overhead tricep", "overhead_tricep_extension"), ("overhead extension", "overhead_tricep_extension"),
]


def normalize_template_key(key: str | None) -> str | None:
    """A global day ("push"), an alias ("fullbody"), or a body-part day whose parts
    are all known ("chest_biceps", "chest+bis" → "chest_biceps"); else None."""
    if not key:
        return None
    k = key.strip().lower().replace("-", "_").replace(" ", "_").replace("+", "_").replace("&", "_")
    k = ALIASES.get(k, k)
    if k in TEMPLATES:
        return k
    parts = [PART_ALIASES.get(p) for p in k.split("_") if p and p not in _LEAD_NOISE]
    if parts and all(parts):
        deduped: list[str] = []
        for p in parts:
            if p not in deduped:
                deduped.append(p)
        return "_".join(deduped)
    return None


def day_template(user, key: str) -> list[ExerciseTemplate]:
    """THE day's exercises for this user: their own routine day if they gave one,
    else the global day, else a body-part day composed from parts. Raises KeyError
    for a key normalize_template_key wouldn't return."""
    t = templates_for(user)
    if key in t:
        return t[key]
    if is_composed_key(key):
        eq = (getattr(user, "equipment", None) or "").strip().lower()
        return compose_day(key, bodyweight=eq in BODYWEIGHT_EQUIPMENT)
    raise KeyError(key)


def slug_for_name(name: str | None) -> str | None:
    """Map a free-text exercise name to a template slug, or None."""
    if not name:
        return None
    n = name.strip().lower()
    # leg curl must beat "curl"; check multi-word hints first by length
    for hint, slug in sorted(NAME_HINTS, key=lambda h: -len(h[0])):
        if hint in n:
            return slug
    return None


def label_for_slug(slug: str) -> str:
    for exs in _all_templates():
        for e in exs:
            if e.slug == slug:
                return e.label
    return slug.replace("_", " ")


def plate_step_for_slug(slug: str) -> float:
    for exs in _all_templates():
        for e in exs:
            if e.slug == slug:
                return e.plate_step
    return 5.0


def is_bodyweight_slug(slug: str) -> bool:
    return any(e.slug == slug and e.default_weight == 0
               for exs in (*BODYWEIGHT_TEMPLATES.values(), *BODYWEIGHT_PART_TEMPLATES.values(), PART_TEMPLATES["core"])
               for e in exs)
