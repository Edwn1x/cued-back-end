"""
Starting loads calibrated to the person, not the template.

Live 2026-09-15 (user 33): a 5'0", 137 lb woman who had never trained got the
generic push card — bench 135 × 5 — and did 35. Users 32 and 43 opened a
first card with the same fixed numbers and abandoned it. The template defaults
(bench 135, squat 155, deadlift 185) are one man's novice numbers; everyone got
them.

This module answers "what should the FIRST card say for THIS person" in three
layers, strongest first:

  1. What they've told us or logged for THAT lift (users.lift_anchors — stated
     numbers via set_lift_anchors / onboarding capture — or a completed set).
     Converted to the template's rep target with Epley.
  2. Their level on a RELATED lift: bench history scales incline / fly / pushdown,
     a stated squat scales leg press / leg curl / calf raise. Each loaded movement
     belongs to one of five families (bench, ohp, row, squat, deadlift); the
     family's estimated 1RM comes from the strongest evidence in it, and the
     accessory is a fixed fraction of it.
  3. Strength standards from the profile: 1RM as a fraction of bodyweight by sex
     and training level (untrained / novice / intermediate / advanced ≈ the
     signup radio none / beginner / intermediate / advanced), then the working
     load for the template's reps, then a first-session buffer — start lighter
     than you think.

Loads are always plate-loadable: barbell lifts round to 5 and never below the
bar (45); dumbbells to 5 per hand; stacks and sleds to 5 / 10. A user with NO
profile at all (no sex, no bodyweight) keeps the template default — the old
behaviour, now the explicit fallback rather than the rule.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from workouts.prs import epley_1rm

logger = logging.getLogger("cued.workouts")

BAR = 45.0
FIRST_SESSION_FACTOR = 0.9          # standards are "can do"; the first card should feel doable
DEFAULT_BODYWEIGHT = {"male": 170.0, "female": 140.0}
_LEVELS = ("untrained", "novice", "intermediate", "advanced")
_EXPERIENCE_TO_LEVEL = {"none": "untrained", "beginner": "novice", "intermediate": "intermediate", "advanced": "advanced"}

# Estimated 1RM as a fraction of bodyweight, by sex and level (rounded from the
# usual population strength standards). Families: the barbell lift each accessory
# hangs off. Unknown sex → the mean of the two.
_STANDARDS: dict[str, dict[str, tuple[float, float, float, float]]] = {
    "bench":    {"male": (0.50, 0.75, 1.00, 1.50), "female": (0.25, 0.40, 0.60, 0.85)},
    "squat":    {"male": (0.65, 1.00, 1.40, 1.90), "female": (0.45, 0.70, 1.05, 1.45)},
    "deadlift": {"male": (0.90, 1.25, 1.75, 2.25), "female": (0.55, 0.85, 1.25, 1.65)},
    "ohp":      {"male": (0.35, 0.50, 0.70, 0.90), "female": (0.20, 0.30, 0.45, 0.60)},
    "row":      {"male": (0.45, 0.65, 0.90, 1.15), "female": (0.30, 0.45, 0.65, 0.85)},
}

# slug → (family, fraction, unit). fraction=None: the lift IS the family's barbell
# movement, working load = Epley(1RM, reps). Otherwise the working load at the
# template's reps is `fraction × family 1RM` (already a working-set number — an
# accessory's rep target doesn't move it much). unit: rounding + floor class.
#   barbell → 5 lb, floor 45 · dumbbell → 5 lb per hand, floor 5
#   stack   → 5 lb, floor 10  · sled → 10 lb, floor 45
_LIFTS: dict[str, tuple[str, float | None, str]] = {
    # bench family
    "bench_press": ("bench", None, "barbell"),
    "incline_db_press": ("bench", 0.28, "dumbbell"),
    "cable_fly": ("bench", 0.15, "stack"),
    "pec_deck": ("bench", 0.45, "stack"),
    "tricep_pushdown": ("bench", 0.30, "stack"),
    "skullcrusher": ("bench", 0.30, "barbell_light"),
    "overhead_tricep_extension": ("bench", 0.22, "dumbbell"),
    # ohp family
    "overhead_press": ("ohp", None, "barbell"),
    "lateral_raise": ("ohp", 0.12, "dumbbell"),
    "front_raise": ("ohp", 0.12, "dumbbell"),
    "rear_delt_fly": ("ohp", 0.15, "dumbbell"),
    # row family (pull)
    "barbell_row": ("row", None, "barbell"),
    "lat_pulldown": ("row", 0.80, "stack"),
    "seated_cable_row": ("row", 0.70, "stack"),
    "face_pull": ("row", 0.25, "stack"),
    "barbell_curl": ("row", 0.35, "barbell_light"),
    "hammer_curl": ("row", 0.20, "dumbbell"),
    "preacher_curl": ("row", 0.30, "barbell_light"),
    # squat family
    "squat": ("squat", None, "barbell"),
    "leg_press": ("squat", 1.50, "sled"),
    "leg_curl": ("squat", 0.30, "stack"),
    "leg_extension": ("squat", 0.40, "stack"),
    "calf_raise": ("squat", 0.60, "sled"),
    # deadlift family
    "deadlift": ("deadlift", None, "barbell"),
    "romanian_deadlift": ("deadlift", 0.65, "barbell"),
}

# Unknown movements (a pasted routine's own slugs) get the generic starting load
# scaled by how the user's profile compares to the man the defaults were written
# for (male novice) — an untrained woman's "shoulder press 30" becomes 15, not 30.
_REFERENCE = ("male", "novice")

_UNIT_STEP = {"barbell": 5.0, "barbell_light": 5.0, "dumbbell": 5.0, "stack": 5.0, "sled": 10.0}
_UNIT_FLOOR = {"barbell": BAR, "barbell_light": 20.0, "dumbbell": 5.0, "stack": 10.0, "sled": BAR}

# Name hints for movements outside _LIFTS (custom routine slugs): family for the
# layer-2 scale. Longest hint wins.
_FAMILY_HINTS: list[tuple[str, str]] = [
    ("romanian", "deadlift"), ("rdl", "deadlift"), ("hip_thrust", "deadlift"), ("sumo", "deadlift"), ("deadlift", "deadlift"),
    ("leg_press", "squat"), ("hack", "squat"), ("goblet", "squat"), ("bulgarian", "squat"), ("split_squat", "squat"),
    ("lunge", "squat"), ("leg_ext", "squat"), ("leg_curl", "squat"), ("hamstring", "squat"), ("calf", "squat"), ("squat", "squat"),
    ("shoulder_press", "ohp"), ("arnold", "ohp"), ("lateral", "ohp"), ("front_raise", "ohp"), ("rear_delt", "ohp"),
    ("reverse_fly", "ohp"), ("upright", "ohp"), ("shrug", "ohp"), ("overhead_press", "ohp"), ("ohp", "ohp"), ("military", "ohp"),
    ("incline", "bench"), ("decline", "bench"), ("chest", "bench"), ("pec", "bench"), ("fly", "bench"), ("crossover", "bench"),
    ("skull", "bench"), ("pushdown", "bench"), ("tricep", "bench"), ("kickback", "bench"), ("bench", "bench"), ("press", "bench"),
    ("pulldown", "row"), ("pullover", "row"), ("lat", "row"), ("curl", "row"), ("row", "row"), ("face_pull", "row"),
]


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── profile → level / sex / bodyweight ──────────────────────────────────────

def profile_of(user) -> dict:
    """{"sex": male|female|None, "level": one of _LEVELS, "bodyweight": lbs or None,
    "age": int|None, "blind": bool}. blind == no sex AND no bodyweight → the
    caller keeps the template default."""
    g = (getattr(user, "gender", None) or "").strip().lower()
    sex = "male" if g == "male" else "female" if g == "female" else None
    bw = getattr(user, "weight_lbs", None)
    try:
        bw = float(bw) if bw else None
    except (TypeError, ValueError):
        bw = None
    if bw is not None and not (60 <= bw <= 500):
        bw = None
    exp = (getattr(user, "experience", None) or "").strip().lower()
    level = _EXPERIENCE_TO_LEVEL.get(exp, "novice")
    age = getattr(user, "age", None)
    try:
        age = int(age) if age else None
    except (TypeError, ValueError):
        age = None
    return {"sex": sex, "level": level, "bodyweight": bw, "age": age, "blind": sex is None and bw is None}


def _age_factor(age: int | None) -> float:
    if not age:
        return 1.0
    if age >= 60:
        return 0.75
    if age >= 50:
        return 0.85
    if age >= 40:
        return 0.92
    return 1.0


def standard_1rm(family: str, sex: str | None, level: str, bodyweight: float | None, *, age: int | None = None) -> float:
    """Population-standard estimated 1RM for a family, from the profile."""
    li = _LEVELS.index(level if level in _LEVELS else "novice")
    table = _STANDARDS[family]
    if sex in table:
        ratio = table[sex][li]
    else:
        ratio = (table["male"][li] + table["female"][li]) / 2.0
    bw = bodyweight or (DEFAULT_BODYWEIGHT[sex] if sex in DEFAULT_BODYWEIGHT else 155.0)
    return ratio * bw * _age_factor(age)


def working_load_from_1rm(one_rm: float, reps: int) -> float:
    """Epley inverse: the load you can do for `reps`."""
    if reps <= 0:
        return one_rm
    return one_rm / (1.0 + reps / 30.0)


def round_load(x: float, unit: str, *, evidence: bool = False) -> float:
    """Plate-loadable. `evidence` = derived from a weight they stated or lifted: no
    unit floor then (a person who benches 35 on a fixed bar gets 35, not the 45 bar)."""
    step = _UNIT_STEP.get(unit, 5.0)
    floor = step if evidence else _UNIT_FLOOR.get(unit, 5.0)
    return max(floor, step * round(x / step))


def family_for_slug(slug: str) -> str | None:
    if slug in _LIFTS:
        return _LIFTS[slug][0]
    s = (slug or "").lower()
    for hint, fam in sorted(_FAMILY_HINTS, key=lambda h: -len(h[0])):
        if hint in s:
            return fam
    return None


def unit_for_slug(slug: str, default_weight: float, plate_step: float) -> str:
    if slug in _LIFTS:
        return _LIFTS[slug][2]
    s = (slug or "").lower()
    if any(k in s for k in ("db", "dumbbell", "hammer", "lateral", "raise", "arnold", "goblet", "bulgarian", "lunge", "kickback")):
        return "dumbbell"
    if any(k in s for k in ("leg_press", "hack", "sled", "calf")):
        return "sled"
    if any(k in s for k in ("cable", "machine", "pulldown", "pushdown", "extension", "curl_machine", "pec", "fly", "face_pull", "seated", "lat_")):
        return "stack"
    if plate_step >= 10 or default_weight >= 95:
        return "barbell"
    return "barbell_light" if default_weight >= 40 else "dumbbell"


# ─── evidence: anchors + history ─────────────────────────────────────────────

def anchors_of(user) -> dict[str, dict]:
    """users.lift_anchors validated: {slug: {"weight": float, "reps": int, ...}}."""
    raw = getattr(user, "lift_anchors", None)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict] = {}
    for slug, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            w, r = float(v.get("weight") or 0), int(v.get("reps") or 5)
        except (TypeError, ValueError):
            continue
        if w > 0 and r > 0:
            out[str(slug)] = {**v, "weight": w, "reps": r}
    return out


def _history_best(session, user_id: int, slug: str) -> tuple[float, int] | None:
    """Best completed (weight, reps) for a slug across card sessions and legacy rows."""
    from models import SetLog, WorkoutSession, Workout, active
    from workouts.templates import slug_for_name
    best: tuple[float, int] | None = None
    q = (session.query(SetLog.actual_weight, SetLog.actual_reps)
         .join(WorkoutSession, SetLog.session_id == WorkoutSession.id)
         .filter(WorkoutSession.user_id == user_id, SetLog.exercise == slug, SetLog.done.is_(True),
                 SetLog.actual_weight.isnot(None), SetLog.actual_reps.isnot(None)))
    for w, r in q.all():
        if w and r and (best is None or epley_1rm(float(w), int(r)) > epley_1rm(*best)):
            best = (float(w), int(r))
    rows = (active(session, Workout, user_id=user_id).filter(Workout.exercises.isnot(None))
            .order_by(Workout.date.desc(), Workout.id.desc()).limit(30).all())
    for wk in rows:
        for e in (wk.exercises or []):
            if not isinstance(e, dict) or not e.get("weight") or not e.get("reps"):
                continue
            if slug_for_name(e.get("name")) == slug or str(e.get("slug") or "") == slug:
                try:
                    cand = (float(e["weight"]), int(e["reps"]))
                except (TypeError, ValueError):
                    continue
                if cand[0] > 0 and cand[1] > 0 and (best is None or epley_1rm(*cand) > epley_1rm(*best)):
                    best = cand
    return best


def family_1rm_evidence(session, user, family: str) -> tuple[float, str] | None:
    """The family's estimated 1RM from the strongest evidence in it: a completed
    set or a stated anchor on any lift of the family, mapped back to the family
    barbell lift through the accessory fraction. → (1rm, "history"|"anchor") or None."""
    anchors = anchors_of(user)
    cands: list[tuple[float, str]] = []
    for slug, (fam, frac, _unit) in _LIFTS.items():
        if fam != family:
            continue
        ev: list[tuple[tuple[float, int], str]] = []
        if session is not None and getattr(user, "id", None):
            h = _history_best(session, user.id, slug)
            if h:
                ev.append((h, "history"))
        a = anchors.get(slug)
        if a:
            ev.append(((a["weight"], a["reps"]), "anchor"))
        for (w, r), src in ev:
            if frac is None:
                cands.append((epley_1rm(w, r), src))
            elif frac > 0:
                cands.append((w / frac, src))       # accessory working load → family 1RM
    if not cands:
        return None
    # history beats a stated number when both exist; within a source, the strongest lift.
    cands.sort(key=lambda c: (c[1] == "history", c[0]), reverse=True)
    return cands[0]


# ─── the answer ──────────────────────────────────────────────────────────────

def calibrated_load(session, user, slug: str, reps: int, default_weight: float, plate_step: float) -> tuple[float, str]:
    """(planned_weight, source) for a loaded lift with no direct history.
    source ∈ anchor | family | profile | template."""
    if default_weight == 0:
        return 0.0, "template"
    prof = profile_of(user)
    unit = unit_for_slug(slug, default_weight, plate_step)
    fam = family_for_slug(slug)
    known = _LIFTS.get(slug)
    frac = known[1] if known else None

    # 1. a stated anchor for THIS lift
    a = anchors_of(user).get(slug)
    if a:
        return round_load(working_load_from_1rm(epley_1rm(a["weight"], a["reps"]), reps), unit, evidence=True), "anchor"

    # 2. a related lift's evidence
    if fam:
        ev = family_1rm_evidence(session, user, fam)
        if ev:
            one_rm, _src = ev
            if known and frac is not None:
                return round_load(frac * one_rm, unit, evidence=True), "family"
            if known:
                return round_load(working_load_from_1rm(one_rm, reps), unit, evidence=True), "family"
            # custom slug in a known family: scale the generic default by evidence vs. standard
            std = standard_1rm(fam, prof["sex"], prof["level"], prof["bodyweight"], age=prof["age"])
            ref = standard_1rm(fam, _REFERENCE[0], _REFERENCE[1], DEFAULT_BODYWEIGHT["male"])
            evidence = max(0.4 * std, min(2.5 * std, one_rm))     # sanity-clamped around their standard
            return round_load(default_weight * evidence / ref, unit), "family"

    # 3. the profile (no stats at all → template default, the old behaviour)
    if prof["blind"]:
        return float(default_weight), "template"
    if known:
        std = standard_1rm(fam, prof["sex"], prof["level"], prof["bodyweight"], age=prof["age"])
        raw = (frac * std) if frac is not None else working_load_from_1rm(std, reps)
        return round_load(raw * FIRST_SESSION_FACTOR, unit), "profile"
    fam_for_scale = fam or "bench"
    std = standard_1rm(fam_for_scale, prof["sex"], prof["level"], prof["bodyweight"], age=prof["age"])
    ref = standard_1rm(fam_for_scale, _REFERENCE[0], _REFERENCE[1], DEFAULT_BODYWEIGHT["male"])
    return round_load(default_weight * (std / ref) * FIRST_SESSION_FACTOR, unit), "profile"


def has_any_lift_evidence(session, user) -> bool:
    """True once the user has told us or logged ANY loaded lift."""
    if anchors_of(user):
        return True
    if session is None or not getattr(user, "id", None):
        return False
    from models import SetLog, WorkoutSession, Workout, active
    q = (session.query(SetLog.id).join(WorkoutSession, SetLog.session_id == WorkoutSession.id)
         .filter(WorkoutSession.user_id == user.id, SetLog.done.is_(True), SetLog.actual_weight > 0))
    if session.query(q.exists()).scalar():
        return True
    rows = (active(session, Workout, user_id=user.id).filter(Workout.exercises.isnot(None))
            .order_by(Workout.date.desc()).limit(30).all())
    return any(isinstance(e, dict) and e.get("weight") and e.get("reps") for wk in rows for e in (wk.exercises or []))


# ─── stated anchors ("i bench 135") ─────────────────────────────────────────

_ANCHOR_ALIASES: list[tuple[str, str]] = [
    ("bench", "bench_press"), ("squat", "squat"), ("deadlift", "deadlift"), ("dl", "deadlift"),
    ("ohp", "overhead_press"), ("overhead press", "overhead_press"), ("shoulder press", "overhead_press"),
    ("military press", "overhead_press"), ("row", "barbell_row"), ("barbell row", "barbell_row"),
    ("rdl", "romanian_deadlift"), ("romanian", "romanian_deadlift"), ("leg press", "leg_press"),
    ("incline", "incline_db_press"), ("pulldown", "lat_pulldown"), ("curl", "barbell_curl"),
]


def anchor_slug(name: str) -> str | None:
    """Free text → an anchor-able slug (the family barbell lifts + a few common accessories)."""
    from workouts.templates import slug_for_name
    n = (name or "").strip().lower()
    if not n:
        return None
    if n in _LIFTS:
        return n
    s = slug_for_name(n)
    if s in _LIFTS:
        return s
    for hint, slug in sorted(_ANCHOR_ALIASES, key=lambda h: -len(h[0])):
        if hint in n:
            return slug
    return None


def set_anchors(user_id: int, items: list[dict], *, source: str) -> dict:
    """Store stated lifts on users.lift_anchors. items: [{"exercise","weight","reps"?}].
    Reps default to 5 (a "working weight"). Returns {"saved": {slug: "135×5"}, "rejected": [...]}."""
    from models import get_session, User
    from sqlalchemy.orm.attributes import flag_modified
    saved: dict[str, str] = {}
    rejected: list[str] = []
    clean: dict[str, dict] = {}
    for it in items or []:
        if not isinstance(it, dict):
            continue
        slug = anchor_slug(str(it.get("exercise") or ""))
        try:
            w = float(it.get("weight") or 0)
            r = int(it.get("reps") or 5)
        except (TypeError, ValueError):
            w, r = 0.0, 0
        if not slug or w <= 0 or r <= 0 or w > 1000 or r > 30:
            rejected.append(str(it.get("exercise") or "?"))
            continue
        clean[slug] = {"weight": w, "reps": r, "source": source, "at": _utcnow().isoformat()}
        saved[slug] = f"{w:g}×{r}"
    if not clean:
        return {"saved": {}, "rejected": rejected}
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return {"error": "user not found"}
        merged = dict(u.lift_anchors or {})
        merged.update(clean)
        u.lift_anchors = merged
        flag_modified(u, "lift_anchors")
        session.commit()
    finally:
        session.close()
    return {"saved": saved, "rejected": rejected}


# Onboarding has no tools, so a stated lift in a reply is caught in code. Precision
# over recall: a stating form only ("i bench 135", "my squat is 185", "bench 135 for 5",
# "squat: 185x5"), never a goal ("wanna bench 225", "get my squat to 2 plates").
_ANCHOR_LIFT_WORDS = r"(bench|squat|deadlift|dl|ohp|overhead press|shoulder press|military press|barbell row|row|rdl|leg press|incline)"
_STATED_ANCHOR_RE = re.compile(
    r"(?<![a-z])(?:i|my|current|currently)?\s*" + _ANCHOR_LIFT_WORDS +
    r"(?:es|s|'s|s'|ing)?(?:\s*(?::|is|at|around|about|like|currently|usually|rn|=|~))*\s*(\d{2,3})\s*(?:lbs?|pounds?)?"
    r"(?:\s*(?:x|×|for)\s*(\d{1,2})(?:\s*reps?)?)?(?![\d])",
    re.IGNORECASE)
_GOAL_WORDS_RE = re.compile(r"\b(want|wanna|goal|get to|hit|reach|hoping|trying to|one day|eventually|someday|target)\b", re.IGNORECASE)


def parse_stated_anchors(text: str) -> list[dict]:
    """"i bench 135 and squat 185 for 5" → [{"exercise": "bench", "weight": 135}, {..., "reps": 5}].
    A clause carrying a goal word yields nothing (precision-biased)."""
    out: list[dict] = []
    if not text:
        return out
    for clause in re.split(r"[.!?\n;]|,\s*(?:and|but)\s+|\band\b", text):
        if _GOAL_WORDS_RE.search(clause):
            continue
        for m in _STATED_ANCHOR_RE.finditer(clause):
            lift, w, r = m.group(1), int(m.group(2)), m.group(3)
            if 20 <= w <= 900 and anchor_slug(lift):
                item = {"exercise": lift, "weight": w}
                if r:
                    item["reps"] = int(r)
                out.append(item)
    return out


def maybe_capture_stated_anchors(user_id: int, text: str, *, source: str) -> dict | None:
    """Code trigger for tool-less surfaces (onboarding). Returns set_anchors' result or None."""
    items = parse_stated_anchors(text)
    if not items:
        return None
    return set_anchors(user_id, items, source=source)


# The first-card ask, remembered: when start_workout_session refuses a trained user's
# first card and asks for their numbers, the day is parked here so the ANSWER sends
# the card in code. Live 2026-09-23 (3/3): the model saved the anchors and replied
# "got it" without calling start_workout_session again — offering the tool wasn't
# enough; the second half of the ask has to be code's. Lives inside lift_anchors
# under a reserved key (anchors_of() skips it: no weight).
_PENDING_KEY = "_pending_card"
PENDING_CARD_TTL_S = 3 * 3600


def set_pending_card(user_id: int, template_key: str) -> None:
    from models import get_session, User
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return
        merged = dict(u.lift_anchors or {})
        merged[_PENDING_KEY] = {"key": template_key, "at": _utcnow().isoformat()}
        u.lift_anchors = merged
        flag_modified(u, "lift_anchors")
        session.commit()
    finally:
        session.close()


def pop_pending_card(user_id: int) -> str | None:
    """The parked day key if the ask is still fresh (≤ TTL), clearing it either way."""
    from models import get_session, User
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u or not isinstance(u.lift_anchors, dict) or _PENDING_KEY not in u.lift_anchors:
            return None
        entry = u.lift_anchors.get(_PENDING_KEY) or {}
        merged = {k: v for k, v in u.lift_anchors.items() if k != _PENDING_KEY}
        u.lift_anchors = merged
        flag_modified(u, "lift_anchors")
        session.commit()
        try:
            age = (_utcnow() - datetime.fromisoformat(str(entry.get("at")))).total_seconds()
        except (TypeError, ValueError):
            return None
        return str(entry.get("key")) if entry.get("key") and age <= PENDING_CARD_TTL_S else None
    finally:
        session.close()


def peek_pending_card(user_id: int) -> str | None:
    """The parked day key if an ask is pending and fresh — without clearing it."""
    from models import get_session, User
    session = get_session()
    try:
        u = session.get(User, user_id)
        entry = (u.lift_anchors or {}).get(_PENDING_KEY) if u and isinstance(u.lift_anchors, dict) else None
    finally:
        session.close()
    if not isinstance(entry, dict) or not entry.get("key"):
        return None
    try:
        age = (_utcnow() - datetime.fromisoformat(str(entry.get("at")))).total_seconds()
    except (TypeError, ValueError):
        return None
    return str(entry["key"]) if age <= PENDING_CARD_TTL_S else None


# "I don't have a number" answers to the first-card ask. Only consulted while an ask
# is pending (≤ TTL) and the text carries no digits. STRONG forms answer on their own;
# WEAK ones ("idk", "not sure") only as a short bare reply — "lol idk what should i
# eat before" is a food question, not an answer.
_NO_NUMBER_STRONG_RE = re.compile(
    r"\b(no (clue|idea|numbers?)|never (really |actually )?(lifted|touched|benched|squatted|done)"
    r"|haven'?t (really |ever )?(lifted|touched|benched|done)|(just )?(start|go) (me )?light|just start"
    r"|whatever (u|you) think|(u|you) (pick|choose|decide)|surprise me)\b", re.IGNORECASE)
_NO_NUMBER_WEAK_RE = re.compile(r"\b(idk|dunno|not sure|nothing|none|nah|no)\b", re.IGNORECASE)


def is_no_number_answer(text: str) -> bool:
    t = (text or "").strip()
    if not t or re.search(r"\d", t):
        return False
    if _NO_NUMBER_STRONG_RE.search(t):
        return True
    return len(re.findall(r"[a-z']+", t.lower())) <= 4 and bool(_NO_NUMBER_WEAK_RE.search(t))


def handle_pending_card_reply(user_id: int, text: str) -> bool:
    """Code-answer to a pending first-card ask (live 2026-09-23, 3/3: told "no clue,
    never really lifted", the model chatted — "wanna start u today or just planning" —
    instead of sending the light card). A stated number → anchors + the card; a
    "no clue / start light" → the card from their stats. Anything else → False, the
    turn proceeds normally (the model can still set_lift_anchors on "just the bar").
    True = the card (intro + card / per-exercise texts) already went out."""
    key = peek_pending_card(user_id)
    if not key or not (text or "").strip():
        return False
    from workouts.start import start_workout_session
    items = parse_stated_anchors(text)
    if items:
        r = set_anchors(user_id, items, source="reply")
        if r.get("saved"):
            try:
                pop_pending_card(user_id)
                sr = start_workout_session(user_id, key)
                logger.info("PENDING_CARD_ANSWERED user=%s key=%s anchors=%s session=%s", user_id, key, r["saved"], sr["session_id"])
                return True
            except Exception as e:  # noqa: BLE001 — fall through to the model with anchors saved
                logger.warning("PENDING_CARD_ANSWER_FAILED user=%s key=%s err=%s", user_id, key, e)
                return False
    if is_no_number_answer(text):
        try:
            sr = start_workout_session(user_id, key, no_anchors=True)
            logger.info("PENDING_CARD_NO_NUMBER user=%s key=%s session=%s", user_id, key, sr["session_id"])
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning("PENDING_CARD_NO_NUMBER_FAILED user=%s key=%s err=%s", user_id, key, e)
            return False
    return False


def describe_anchors(user) -> str | None:
    """'bench 135×5 (stated), squat 185×5 (stated)' for prompts, or None."""
    from workouts.templates import label_for_slug
    a = anchors_of(user)
    if not a:
        return None
    return ", ".join(f"{label_for_slug(s)} {v['weight']:g}×{v['reps']}" for s, v in a.items())
