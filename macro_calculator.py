"""
Macro Calculator — Cued
========================
Computes calorie and protein targets from user profile data.
Stores them on the User record so numbers are consistent across all messages.

Returns targets plus a plain-English explanation and an ambiguity flag
so the coach can be transparent about where the numbers come from.
"""

from dataclasses import dataclass


ACTIVITY_MULTIPLIERS = {
    "sedentary":      1.2,
    "lightly_active": 1.375,
    "active":         1.55,
    "very_active":    1.725,
}

GOAL_ADJUSTMENTS = {
    "fat_loss":        -400,
    "muscle_building": +250,
    "strength":        +150,
    "endurance":       +150,   # training-day average
    "general_fitness": 0,
    "flexibility":     0,
}


@dataclass
class MacroResult:
    calorie_target: int
    protein_target: int
    explanation: str       # plain-English reason to send to user
    is_ambiguous: bool     # True if goal is unclear or key data is missing
    ambiguity_note: str    # what's missing / what assumption was made


def compute_targets(user) -> MacroResult:
    """
    Compute daily calorie and protein targets from the user's profile.
    Returns a MacroResult with the targets and an explanation string.
    """
    missing = []

    # ── BMR (Mifflin-St Jeor) ──────────────────────
    weight_kg = (user.weight_lbs * 0.453592) if user.weight_lbs else None
    height_cm = None
    if user.height_ft and user.height_in is not None:
        height_cm = (user.height_ft * 30.48) + (user.height_in * 2.54)

    if not weight_kg:
        missing.append("weight")
    if not height_cm:
        missing.append("height")
    if not user.age:
        missing.append("age")

    if weight_kg and height_cm and user.age:
        gender = user.gender or "prefer_not_to_say"
        if gender == "female":
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * user.age - 161
        else:
            # Use male formula as default for non-binary / prefer_not_to_say
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * user.age + 5
    else:
        # Fallback estimate when key stats are missing
        bmr = 1800

    # ── TDEE ──────────────────────────────────────
    activity = user.activity_level or "lightly_active"
    multiplier = ACTIVITY_MULTIPLIERS.get(activity, 1.375)
    tdee = int(bmr * multiplier)

    # ── Goal adjustment ───────────────────────────
    goals_str = user.goal or ""
    goals = [g.strip() for g in goals_str.split(",") if g.strip()]

    is_ambiguous = False
    ambiguity_note = ""

    # Check for conflicting goals (cut vs bulk)
    wants_fat_loss = "fat_loss" in goals
    wants_muscle = "muscle_building" in goals or "strength" in goals

    if wants_fat_loss and wants_muscle:
        # Recomp — genuinely ambiguous, explain it
        adjustment = 0
        is_ambiguous = True
        ambiguity_note = (
            "Your goals include both fat loss and muscle building — that's body recomp. "
            "I'm starting you at maintenance calories. Tell me if you want to prioritize "
            "cutting fat or gaining muscle and I'll adjust the target."
        )
        goal_label = "body recomp (maintenance)"
        goal_reason = "build muscle while staying lean"
    elif wants_fat_loss:
        adjustment = GOAL_ADJUSTMENTS["fat_loss"]
        goal_label = "fat loss"
        goal_reason = f"a {abs(adjustment)} cal deficit below your maintenance to lose fat steadily without losing muscle"
    elif wants_muscle or "strength" in goals:
        primary = "muscle_building" if wants_muscle else "strength"
        adjustment = GOAL_ADJUSTMENTS[primary]
        goal_label = "muscle building" if wants_muscle else "strength"
        goal_reason = f"a {adjustment} cal surplus above maintenance to fuel growth without excess fat gain"
    elif goals:
        adjustment = GOAL_ADJUSTMENTS.get(goals[0], 0)
        goal_label = goals[0].replace("_", " ")
        goal_reason = "maintenance calories to support your goal"
    else:
        adjustment = 0
        goal_label = "general fitness"
        goal_reason = "maintenance calories — no specific goal set"
        is_ambiguous = True
        ambiguity_note = "No specific goal was set. I'll use maintenance for now. Tell me if you want to cut or bulk."

    calorie_target = tdee + adjustment

    # ── Protein target ────────────────────────────
    if user.weight_lbs:
        if wants_fat_loss:
            protein_target = int(user.weight_lbs * 1.0)   # 1g/lb on a cut
        else:
            protein_target = int(user.weight_lbs * 0.85)  # ~0.85g/lb for maintenance/bulk
    else:
        protein_target = 140  # safe fallback
        missing.append("protein estimate (no weight on file)")

    # ── Build explanation ──────────────────────────
    if missing:
        stats_note = f"Missing: {', '.join(missing)} — using estimates."
    else:
        height_str = f"{user.height_ft}'{user.height_in or 0}\""
        stats_note = f"Based on: {height_str}, {user.weight_lbs:.0f}lbs, {user.age}yo, {activity.replace('_', ' ')} lifestyle."

    explanation = (
        f"{stats_note} "
        f"Your TDEE is ~{tdee} cal. "
        f"For {goal_label}, we're targeting {calorie_target} cal and {protein_target}g protein — {goal_reason}."
    )

    return MacroResult(
        calorie_target=calorie_target,
        protein_target=protein_target,
        explanation=explanation,
        is_ambiguous=is_ambiguous,
        ambiguity_note=ambiguity_note,
    )


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

    # Mifflin-St Jeor BMR
    if gender in ("male", "prefer_not_to_say"):
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
    else:
        bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

    # Activity multiplier based on training days
    workout_days = user.workout_days or "3"

    try:
        if "," in str(workout_days):
            days_count = len(workout_days.split(","))
        else:
            days_count = int(workout_days)
    except (ValueError, TypeError):
        days_count = 3

    if days_count <= 2:
        multiplier = 1.375
    elif days_count <= 4:
        multiplier = 1.55
    elif days_count <= 5:
        multiplier = 1.65
    else:
        multiplier = 1.75

    tdee = round(bmr * multiplier)

    # Adjust for goal
    goal = user.goal or "general_fitness"
    goal_label = "maintenance"

    if "fat_loss" in goal and "muscle" in goal:
        calories = round(tdee * 0.9 / 50) * 50
        goal_label = "recomp"
    elif "fat_loss" in goal:
        calories = round((tdee - 500) / 50) * 50
        goal_label = "cutting"
    elif "muscle" in goal or "strength" in goal:
        calories = round((tdee + 250) / 50) * 50
        goal_label = "building"
    else:
        calories = round(tdee / 50) * 50
        goal_label = "maintenance"

    # Protein: 1g/lb for muscle/fat-loss goals, 0.8g otherwise
    weight_lbs = user.weight_lbs or 150
    if "muscle" in goal or "strength" in goal or "fat_loss" in goal:
        protein = round(weight_lbs * 1.0)
    else:
        protein = round(weight_lbs * 0.8)

    # Floor values
    calories = max(calories, 1400)
    protein = max(protein, 80)

    return {
        "calories": calories,
        "protein": protein,
        "tdee": tdee,
        "bmr": round(bmr),
        "goal_label": goal_label,
    }


def get_or_compute_targets(user, session) -> MacroResult:
    """
    Return stored targets if they exist, otherwise compute and store them.
    Always returns a MacroResult.
    """
    result = compute_targets(user)

    # Store if not already set
    if not user.calorie_target or not user.protein_target:
        user.calorie_target = result.calorie_target
        user.protein_target = result.protein_target
        session.commit()

    return result
