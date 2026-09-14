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
"""

import re

TEN_HAAF_MIN_DAYS = 5

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
    if days_count >= TEN_HAAF_MIN_DAYS:
        bmr_formula = "ten_haaf"
        bmr = (11.936 * weight_kg + 587.728 * (height_cm / 100.0) - 8.129 * age
               + 191.027 * (1 if is_male else 0) + 29.279)
    else:
        bmr_formula = "mifflin"
        if is_male:
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
        else:
            bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161

    # Activity multiplier — use avg_steps if available (objective), else fall back to workout_days
    avg_steps = getattr(user, "avg_steps", None)
    if avg_steps:
        if avg_steps < 5000:
            multiplier = 1.2    # sedentary
        elif avg_steps < 7500:
            multiplier = 1.375  # lightly active
        elif avg_steps < 10000:
            multiplier = 1.55   # active
        else:
            multiplier = 1.725  # very active
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
    elif "endurance" in goal:
        calories = round((tdee + 150) / 50) * 50  # slight surplus to fuel training volume
        goal_label = "endurance"
    else:
        calories = round(tdee / 50) * 50
        goal_label = "maintenance"

    # Protein: 1g/lb for muscle/fat-loss/strength, 0.7g for endurance, 0.8g otherwise
    weight_lbs = user.weight_lbs or 150
    if "muscle" in goal or "strength" in goal or "fat_loss" in goal:
        protein = round(weight_lbs * 1.0)
    elif "endurance" in goal:
        protein = round(weight_lbs * 0.7)
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
        "bmr_formula": bmr_formula,
        "goal_label": goal_label,
    }
