"""
Session templates: what a day looks like with no history. Weights are the
template DEFAULTS (beginner-safe, plate-loadable); the plan overrides them from
the user's completed sets. `plate_step` is the smallest progression: 5 lb for
upper-body lifts, 10 lb for lower-body barbell lifts.
"""

from __future__ import annotations

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
    split keys (push/pull/legs/upper/lower/full_body) are honored; a day whose rows
    are all malformed is dropped so the global template still covers it."""
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
]


def normalize_template_key(key: str | None) -> str | None:
    if not key:
        return None
    k = key.strip().lower().replace("-", "_").replace(" ", "_")
    k = ALIASES.get(k, k)
    return k if k in TEMPLATES else None


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
    return any(e.slug == slug for exs in BODYWEIGHT_TEMPLATES.values() for e in exs)
