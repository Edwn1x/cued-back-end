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


def _ex(slug, label, sets, reps, weight, step):
    return ExerciseTemplate(slug, label, sets, reps, float(weight), float(step))


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

# Aliases the split pointer / a user might use.
ALIASES = {"chest_back": "upper", "shoulders_arms": "upper", "leg": "legs", "fullbody": "full_body"}

# Loose name matching for legacy `Workout.exercises` rows and typed text ("bench",
# "incline", "flys"). First match wins; keys are lowercase substrings.
NAME_HINTS: list[tuple[str, str]] = [
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
    for exs in TEMPLATES.values():
        for e in exs:
            if e.slug == slug:
                return e.label
    return slug.replace("_", " ")


def plate_step_for_slug(slug: str) -> float:
    for exs in TEMPLATES.values():
        for e in exs:
            if e.slug == slug:
                return e.plate_step
    return 5.0
