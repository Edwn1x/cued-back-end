"""
Macro Calculator — Cued
========================
The ONE calorie/protein calculator (2026-09-14: a second, uncalled one that
looked activity up from a free-text column and fell to 1.375 was deleted).
Onboarding computes targets here at completion and stores them on the User
row so every surface reads the same numbers.

Base rate (2026-09-14, founder's call after the literature check):
  - Ten Haaf et al. 2014 for anyone who trains 5+ days a week. In the 2023
    meta-analysis of 1,058 athletes (mean age 23) it put 80% of people within
    10% of measured RMR; Mifflin-St Jeor put 52% and significantly
    underestimates that population.
  - Mifflin-St Jeor (1990) for everyone else — the best general-population
    equation.
A workout_days RANGE counts as its lower bound ("4-5" → 4 → Mifflin): Ten Haaf
runs higher, so only a clear 5+ earns it.

Under 18 (2026-09-22, founder's call after the user-32 review): the adult equations
were validated on adults and run LOW for a growing teenager. Live: a 16-year-old at
5'2"/180 got Mifflin ~1560 × sedentary → 1870 maintenance → the 1400 floor, while
her own app (and the equations built for her age group) said ~2.0–2.2k. For anyone
under 18 the maintenance number is the IOM (2002/2005) Estimated Energy Requirement
for ages 9–18, which is a total-expenditure equation with a growth term — no
separate activity multiplier; activity enters as its PA coefficient, mapped from the
same step / training-day buckets. The goal rule and the floors are unchanged.

Protein basis (same review): 1 g/lb of TOTAL bodyweight on a heavy frame produces a
target the calorie budget can't hold (173 g on 1400 cal = 49% of intake). The
per-pound rule now runs on a REFERENCE weight — actual weight, capped at the weight
that puts their height at BMI 25 — and the result is capped at 35% of the calorie
target. Lean users are untouched (the founder's 139 lb / 139 g stays).
"""

import logging
import re

logger = logging.getLogger("cued.macros")

TEN_HAAF_MIN_DAYS = 5
TEEN_MAX_AGE = 17            # < 18 → IOM EER (ages 9–18)
PROTEIN_MAX_SHARE = 0.35     # protein kcal ≤ 35% of the calorie target
REFERENCE_BMI = 25.0         # protein per-lb rule runs on min(actual, weight at BMI 25)
CALORIE_FLOOR = 1400
PROTEIN_FLOOR = 80

# IOM Estimated Energy Requirement, ages 9–18 (Dietary Reference Intakes, 2002/2005).
#   boys : EER = 88.5  − 61.9·age + PA·(26.7·kg + 903·m) + 25
#   girls: EER = 135.3 − 30.8·age + PA·(10.0·kg + 934·m) + 25
# PA by activity level: sedentary / low active / active / very active.
_EER_PA = {"male": (1.00, 1.13, 1.26, 1.42), "female": (1.00, 1.16, 1.31, 1.56)}


def activity_level_index(avg_steps, days_count: int) -> int:
    """0 sedentary, 1 low active, 2 active, 3 very active — from avg_steps when known
    (objective), else from training days. The SAME buckets feed the adult multiplier
    and the teen PA coefficient so a user doesn't change activity class at 18."""
    if avg_steps:
        if avg_steps < 5000:
            return 0
        if avg_steps < 7500:
            return 1
        if avg_steps < 10000:
            return 2
        return 3
    if days_count <= 2:
        return 1
    if days_count <= 4:
        return 2
    return 3


def teen_eer(weight_kg: float, height_cm: float, age: int, is_male: bool, level: int) -> float:
    """IOM EER for ages 9–18 at activity `level` — a TOTAL daily requirement (growth
    included), so it is the maintenance number itself, not a BMR to multiply."""
    pa = _EER_PA["male" if is_male else "female"][level]
    m = height_cm / 100.0
    if is_male:
        return 88.5 - 61.9 * age + pa * (26.7 * weight_kg + 903 * m) + 25
    return 135.3 - 30.8 * age + pa * (10.0 * weight_kg + 934 * m) + 25


def reference_weight_lbs(weight_lbs: float, height_cm: float) -> float:
    """The weight the per-pound protein rule runs on: actual, capped at BMI 25 for
    their height. A 5'2" frame caps at ~137 lb whatever the scale says."""
    cap_kg = REFERENCE_BMI * (height_cm / 100.0) ** 2
    return min(float(weight_lbs), cap_kg / 0.453592)

_RANGE_RE = re.compile(r"^\s*(\d+)\s*(?:-|–|to)\s*(\d+)\s*$")
_PLUS_RE = re.compile(r"^\s*(\d+)\s*\+\s*$")


def training_days_per_week(workout_days, default: int = 3) -> int:
    """Count from the free-form workout_days column: "5", "4-5" (→ 4, the lower
    bound), "5+" (→ 5), "mon,wed,fri" (→ 3), "3 days" (→ 3). Unparseable → default."""
    if workout_days is None:
        return default
    text = str(workout_days).strip().lower()
    if not text:
        return default
    if "," in text:
        return len([p for p in text.split(",") if p.strip()])
    m = _RANGE_RE.match(text)
    if m:
        return int(m.group(1))
    m = _PLUS_RE.match(text)
    if m:
        return int(m.group(1))
    m = re.search(r"\d+", text)
    if m:
        return int(m.group(0))
    return default


def apply_goal(tdee: int, goal: str) -> dict:
    """The goal rule on a maintenance number → {"calories", "goal_label"}. Shared by
    calculate_targets (onboarding) and adaptive_targets (biweekly re-application)."""
    goal = goal or "general_fitness"
    if "fat_loss" in goal and "muscle" in goal:
        return {"calories": round(tdee * 0.9 / 50) * 50, "goal_label": "recomp"}
    if "fat_loss" in goal:
        return {"calories": round((tdee - 500) / 50) * 50, "goal_label": "cutting"}
    if "muscle" in goal or "strength" in goal:
        return {"calories": round((tdee + 250) / 50) * 50, "goal_label": "building"}
    if "endurance" in goal:
        return {"calories": round((tdee + 150) / 50) * 50, "goal_label": "endurance"}
    return {"calories": round(tdee / 50) * 50, "goal_label": "maintenance"}


def calculate_targets(user) -> dict:
    """
    Calculate calorie and protein targets based on user profile.
    Uses Mifflin-St Jeor for BMR, activity multiplier for TDEE,
    then adjusts based on goal.
    """
    # Defaults if data is somehow missing
    weight_kg = (user.weight_lbs or 150) * 0.453592
    height_cm = ((user.height_ft or 5) * 12 + (user.height_in or 7)) * 2.54
    age = user.age or 25
    gender = user.gender or "male"

    days_count = training_days_per_week(user.workout_days)
    is_male = gender in ("male", "prefer_not_to_say")
    level = activity_level_index(getattr(user, "avg_steps", None), days_count)
    if age <= TEEN_MAX_AGE:
        # Teen: the EER IS the maintenance number. `bmr` reports the sedentary
        # (PA=1.0) requirement — the closest analog for the log line and the
        # profile page; there is no separate resting figure in this equation.
        bmr_formula = "iom_eer"
        bmr = teen_eer(weight_kg, height_cm, age, is_male, 0)
        tdee = round(teen_eer(weight_kg, height_cm, age, is_male, level))
        multiplier = None
    elif days_count >= TEN_HAAF_MIN_DAYS:
        bmr_formula = "ten_haaf"
        bmr = (11.936 * weight_kg + 587.728 * (height_cm / 100.0) - 8.129 * age
               + 191.027 * (1 if is_male else 0) + 29.279)
    else:
        bmr_formula = "mifflin"
        if is_male:
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
        else:
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

    if age > TEEN_MAX_AGE:
        # Adult activity multiplier — avg_steps when known (objective), else training days.
        avg_steps = getattr(user, "avg_steps", None)
        if avg_steps:
            multiplier = (1.2, 1.375, 1.55, 1.725)[level]
        else:
            if days_count <= 2:
                multiplier = 1.375
            elif days_count <= 4:
                multiplier = 1.55
            elif days_count <= 5:
                multiplier = 1.65
            else:
                multiplier = 1.75
        tdee = round(bmr * multiplier)

    goal = user.goal or "general_fitness"
    g = apply_goal(tdee, goal)
    calories, goal_label = g["calories"], g["goal_label"]

    # Floor first: the protein share cap reads the FINAL calorie number.
    calories = max(calories, CALORIE_FLOOR)

    # Protein: 1g/lb for muscle/fat-loss/strength, 0.7g for endurance, 0.8g otherwise —
    # per lb of REFERENCE weight (actual, capped at BMI 25), then capped at 35% of
    # calories. See the module header.
    ref_lbs = reference_weight_lbs(user.weight_lbs or 150, height_cm)
    if "muscle" in goal or "strength" in goal or "fat_loss" in goal:
        protein = round(ref_lbs * 1.0)
    elif "endurance" in goal:
        protein = round(ref_lbs * 0.7)
    else:
        protein = round(ref_lbs * 0.8)
    protein = min(protein, int(calories * PROTEIN_MAX_SHARE / 4))
    protein = max(protein, PROTEIN_FLOOR)

    return {
        "calories": calories,
        "protein": protein,
        "tdee": tdee,
        "bmr": round(bmr),
        "bmr_formula": bmr_formula,
        "goal_label": goal_label,
        "activity_level": level,
        "reference_weight_lbs": round(ref_lbs, 1),
    }


def recompute_targets(user_id: int) -> dict:
    """Re-run the calculator for one user whose targets are code-owned
    (targets_source != 'user') and write the result — the path for a FORMULA
    change reaching an existing row (onboarding computes once; log_weight only
    follows protein). Returns {"old": (cal, pro), "new": (cal, pro), "changed": bool}.
    A user-picked pair is left alone (only the computed pair beside it is refreshed)."""
    from models import get_session, User
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return {"error": "user not found"}
        t = calculate_targets(user)
        old = (user.calorie_target, user.protein_target)
        user.calorie_target_computed = t["calories"]
        user.protein_target_computed = t["protein"]
        if getattr(user, "targets_source", None) != "user":
            user.calorie_target = t["calories"]
            user.protein_target = t["protein"]
            user.targets_source = "computed"
        new = (user.calorie_target, user.protein_target)
        session.commit()
        logger.info("TARGETS_RECOMPUTED user=%s %s -> %s formula=%s tdee=%s",
                    user_id, old, new, t["bmr_formula"], t["tdee"])
        return {"old": old, "new": new, "changed": old != new, "tdee": t["tdee"],
                "formula": t["bmr_formula"]}
    finally:
        session.close()


# ─── Bounded user override (founder, 2026-09-14) ─────────────────────────────
# "I just don't think I could eat that much" is a real constraint, and the
# no-invented-numbers rule left no path for it. Middle ground: the user can set
# either target within ±15% of the COMPUTED value; the row records that it's
# their pick (targets_source='user') and keeps the computed number beside it.
# Outside the band the caller offers the nearest allowed value and explains.
TARGET_OVERRIDE_PCT = 0.15
_NEAREST_STEP = 10


def override_bounds(computed: int) -> tuple[int, int]:
    lo = int(round(computed * (1 - TARGET_OVERRIDE_PCT) / _NEAREST_STEP) * _NEAREST_STEP)
    hi = int(round(computed * (1 + TARGET_OVERRIDE_PCT) / _NEAREST_STEP) * _NEAREST_STEP)
    return lo, hi


# A maintenance number the user brings from months of their own tracking (an app's
# TDEE, a coach's number) is better evidence than an equation — but only within
# reason. It's accepted when it sits within this share of OUR maintenance estimate;
# the calorie band is then built around the goal rule applied to THEIR number.
REPORTED_MAINTENANCE_PCT = 0.25


def apply_target_override(user_id: int, *, calories=None, protein=None, note: str = None,
                          maintenance=None) -> dict:
    """Code-owned. Bounds each requested value against calculate_targets(user);
    writes accepted ones to users.calorie_target / protein_target, stamps
    targets_source='user' and the computed pair beside them. Returns
      {"accepted": {"calories": 2200}, "rejected": {"protein": {"asked": 200, "min": 120, "max": 160}},
       "computed": {"calories": 2450, "protein": 139}, "current": {"calories": 2200, "protein": 139}}
    Never raises on bad numbers — they land in "rejected".

    `maintenance`: a maintenance/TDEE number THEY report (their app, prior tracking).
    Within ±25% of the computed TDEE it is stored on users.reported_maintenance, and
    the calorie band is centred on apply_goal(reported) instead of the computed
    calories — the result carries "maintenance": {"accepted": True, "basis_calories": N}.
    Outside that range it is rejected with the computed TDEE and the band is unchanged.
    A stored reported_maintenance keeps centring the band on later calls, and the
    adaptive cycle seeds from it (adaptive_targets)."""
    from models import get_session, User
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return {"error": "user not found"}
        computed = calculate_targets(user)
        accepted, rejected = {}, {}
        maint_result = None
        if maintenance is not None:
            try:
                m = int(round(float(maintenance)))
                lo_m = int(round(computed["tdee"] * (1 - REPORTED_MAINTENANCE_PCT)))
                hi_m = int(round(computed["tdee"] * (1 + REPORTED_MAINTENANCE_PCT)))
                if lo_m <= m <= hi_m:
                    user.reported_maintenance = m
                    maint_result = {"accepted": True, "reported": m, "computed_tdee": computed["tdee"]}
                else:
                    maint_result = {"accepted": False, "reported": m, "computed_tdee": computed["tdee"],
                                    "min": lo_m, "max": hi_m}
            except (TypeError, ValueError):
                maint_result = {"accepted": False, "reported": maintenance, "reason": "not a number"}
        # The calorie band's centre: their (accepted, stored) maintenance through the
        # goal rule, else the computed calories.
        basis_cal = computed["calories"]
        stored_m = getattr(user, "reported_maintenance", None)
        if stored_m:
            basis_cal = max(apply_goal(int(stored_m), user.goal or "")["calories"], CALORIE_FLOOR)
        if maint_result is not None:
            maint_result["basis_calories"] = basis_cal
        for field, asked in (("calories", calories), ("protein", protein)):
            if asked is None:
                continue
            try:
                asked = int(round(float(asked)))
            except (TypeError, ValueError):
                rejected[field] = {"asked": asked, "reason": "not a number"}
                continue
            centre = basis_cal if field == "calories" else computed[field]
            lo, hi = override_bounds(centre)
            if lo <= asked <= hi:
                accepted[field] = asked
            else:
                rejected[field] = {"asked": asked, "min": lo, "max": hi, "computed": computed[field]}
                if field == "calories" and centre != computed[field]:
                    rejected[field]["basis"] = centre
        if accepted:
            if "calories" in accepted:
                user.calorie_target = accepted["calories"]
            if "protein" in accepted:
                user.protein_target = accepted["protein"]
            user.calorie_target_computed = computed["calories"]
            user.protein_target_computed = computed["protein"]
            user.targets_source = "user"
            logger.info("TARGETS_USER_CHOSEN user=%s accepted=%s computed=%s/%s basis=%s note=%s",
                        user_id, accepted, computed["calories"], computed["protein"], basis_cal,
                        (note or "")[:80])
        if accepted or (maint_result and maint_result.get("accepted")):
            session.commit()
        if maint_result:
            logger.info("TARGETS_REPORTED_MAINTENANCE user=%s %s", user_id, maint_result)
        if rejected:
            logger.info("TARGETS_OVERRIDE_REJECTED user=%s rejected=%s", user_id, rejected)
        out = {"accepted": accepted, "rejected": rejected,
               "computed": {"calories": computed["calories"], "protein": computed["protein"]},
               "current": {"calories": user.calorie_target, "protein": user.protein_target}}
        if maint_result is not None:
            out["maintenance"] = maint_result
        return out
    finally:
        session.close()
