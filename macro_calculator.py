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

Ten Haaf also needs a training HISTORY (2026-10-27, live user 77): the equation was fit
on people already training like athletes. A complete beginner who PLANS six days is not
that population yet — "none" / "beginner" (under 6 months) stay on Mifflin whatever
they plan; intermediate+ (or an unknown experience) keep the 5+ rule. Live: 6'2"/165,
22M, never trained, "6 days" → Ten Haaf 2039 × 1.375 → 2804 → 3100 to build; Mifflin
gives 1818 → 2500 → 2750. The 350 kcal gap was the athlete equation on a non-athlete.

Reported current intake (same review): what they EAT NOW is the one input the equations
never see, and it is where feasibility lives — user 77 ate ~2000 and was handed 3100
("I cannot afford that much food"). Setup asks it (onboarding_agent, stored on
users.reported_intake; 0 = asked, doesn't know); the coach can record it mid-coaching
(set_targets current_intake). When the goal number sits more than RAMP_MAX_STEP above
what they eat, the FIRST target is reported × (1 + RAMP_MAX_STEP), rounded to 50, and
protein runs at the low end of the evidence range (0.8 g/lb) instead of 1.0; the full
number rides in the result as `ramp` so the summary can say where this is heading. The
biweekly adaptive cycle steps the target from there off the scale (adaptive_targets
clamps ±150 from the CURRENT target, so a ramped start grows). Downward (a cut whose
target is far below what they eat) is NOT ramped — the deficit ceilings already bound it.

Under 18 (2026-09-22, founder's call after the user-32 review): the adult equations
were validated on adults and run LOW for a growing teenager. Live: a 16-year-old at
5'2"/180 got Mifflin ~1560 × sedentary → 1870 maintenance → the 1400 floor, while
her own app (and the equations built for her age group) said ~2.0–2.2k. For anyone
under 18 the maintenance number is the IOM (2002/2005) Estimated Energy Requirement
for ages 9–18, which is a total-expenditure equation with a growth term — no
separate activity multiplier; activity enters as its PA coefficient, mapped from the
same step / training-day buckets. The goal rule and the floors are unchanged.

Heavy frames (2026-10-29, live users 80 and 86 — 320 lb → 3400 and 220 lb → 3550, both
asking to lose fat): every bias in the calculator pointed the same way for an overweight
user and nothing bounded the high side. Four rules, each gated on BMI ≥ 30 or on the
step scale: (1) 10k steps is "active" (×1.55), not "very active" — the top bucket needs
12.5k+ (activity_level_index); (2) at BMI ≥ 30 the fat-loss rule wins even when muscle is
also a goal — recomp is a lean person's plan (goal_pct); (3) the 500 kcal/day deficit
ceiling is the LEAN-lifter bound and does not apply at BMI ≥ 30 — the fat-mass bound does
(apply_goal); (4) Ten Haaf needs a lean frame as well as a training history (BMI < 30).
User 86 recomputes to 2500 (was 3550), user 80 to 2700 (was 3400). Lean users with
under 10k steps are untouched; 10–12.5k steps moves from ×1.725 to ×1.55.

Protein basis (same review): 1 g/lb of TOTAL bodyweight on a heavy frame produces a
target the calorie budget can't hold (173 g on 1400 cal = 49% of intake). The
per-pound rule now runs on a REFERENCE weight — actual weight, capped at the weight
that puts their height at BMI 25 — and the result is capped at 35% of the calorie
target. Lean users are untouched (the founder's 139 lb / 139 g stays).
"""

import logging
import re

import config

logger = logging.getLogger("cued.macros")

TEN_HAAF_MIN_DAYS = 5
STEPS_LOW_ACTIVE = 5000      # adult step buckets (Tudor-Locke): sedentary / low active / active / very active
STEPS_ACTIVE = 10000
STEPS_VERY_ACTIVE = 12500
TEN_HAAF_UNTRAINED = ("none", "beginner")   # experience levels that stay on Mifflin whatever they plan
RAMP_MAX_STEP = 0.20         # first target ≤ reported intake × 1.20 when the goal number is above it
PROTEIN_RAMP_G_PER_LB = 0.8  # protein while ramped: the low end of the 0.8–1 g/lb range
REPORTED_INTAKE_MIN = 800    # a reported daily intake outside this is a typo / not a number we act on
REPORTED_INTAKE_MAX = 6000
REPORTED_PROTEIN_MIN = 20
REPORTED_PROTEIN_MAX = 400
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

# IOM total energy expenditure for OVERWEIGHT / OBESE children 3–18 (BMI ≥ 85th percentile;
# DRI 2002/2005, Table 5-25). The normal-weight equation above is linear in bodyweight with
# a steep slope (boys: 26.7 kcal/kg × PA) and was fit on normal-weight kids — at 200 lb it
# extrapolates wildly. Live 2026-10-17 (user 40, 17M, 5'4"/200, 10k steps, fat loss): the
# normal-weight equation at "very active" gave a 4585 maintenance and a 4150 target.
#   boys : TEE = 114 − 50.9·age + PA·(19.5·kg + 1161.4·m)
#   girls: TEE = 389 − 41.2·age + PA·(15.0·kg + 701.6·m)
_EER_PA_OVERWEIGHT = {"male": (1.00, 1.12, 1.24, 1.45), "female": (1.00, 1.18, 1.35, 1.60)}
TEEN_OVERWEIGHT_BMI = 25.0      # the adult overweight cutoff ≈ the 85th percentile at 17–18;
                                # younger teens cross it later, which only keeps the gentler equation
TEEN_ADULT_CEILING = 1.15       # a teen maintenance never exceeds the adult path's by more than this


def activity_level_index(avg_steps, days_count: int) -> int:
    """0 sedentary, 1 low active, 2 active, 3 very active — from avg_steps when known
    (objective), else from training days. The SAME buckets feed the adult multiplier
    and the teen PA coefficient so a user doesn't change activity class at 18.

    Step cut-offs (2026-10-29, live user 86): Tudor-Locke's scale — under 5k sedentary,
    5–10k low active ("somewhat active" tops out at 10k), 10–12.5k active, 12.5k+ highly
    active. The old table put 10k in the TOP bucket (×1.725 — the multiplier for someone
    training hard twice a day), which is what the teen function below already refused to
    do. 10k steps is a campus walk, not an athlete's day."""
    if avg_steps:
        if avg_steps < STEPS_LOW_ACTIVE:
            return 0
        if avg_steps < STEPS_ACTIVE:
            return 1
        if avg_steps < STEPS_VERY_ACTIVE:
            return 2
        return 3
    if days_count <= 2:
        return 1
    if days_count <= 4:
        return 2
    return 3


def teen_eer(weight_kg: float, height_cm: float, age: int, is_male: bool, level: int,
             *, overweight: bool = False) -> float:
    """IOM EER for ages 9–18 at activity `level` — a TOTAL daily requirement (growth
    included), so it is the maintenance number itself, not a BMR to multiply.
    `overweight` selects the IOM overweight/obese TEE equation (see _EER_PA_OVERWEIGHT)."""
    m = height_cm / 100.0
    if overweight:
        pa = _EER_PA_OVERWEIGHT["male" if is_male else "female"][level]
        if is_male:
            return 114 - 50.9 * age + pa * (19.5 * weight_kg + 1161.4 * m)
        return 389 - 41.2 * age + pa * (15.0 * weight_kg + 701.6 * m)
    pa = _EER_PA["male" if is_male else "female"][level]
    if is_male:
        return 88.5 - 61.9 * age + pa * (26.7 * weight_kg + 903 * m) + 25
    return 135.3 - 30.8 * age + pa * (10.0 * weight_kg + 934 * m) + 25


def teen_activity_level(avg_steps, days_count: int) -> int:
    """The IOM PA category for a teen. The adult buckets read 10k steps as the top bucket
    (×1.725 — defensible there); IOM "very active" means PAL ≥ 1.9, a competitive athlete,
    and "active" ≥ 60 min of moderate activity on top of daily living. 10k steps a day is
    low active; 12.5k+ or a 5+-day training week earns active; nothing earns very active
    from steps alone."""
    if avg_steps:
        lvl = 0 if avg_steps < 7500 else (1 if avg_steps < 12500 else 2)
    else:
        lvl = 0 if days_count <= 2 else (1 if days_count <= 4 else 2)
    if days_count >= 5:
        lvl = min(lvl + 1, 3)
    return lvl


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


# ─── Goal rule (founder, 2026-09-22 — rewrite/targets-deficit-research/RESEARCH.md) ──
# Percentages of maintenance, not flat kcal. The old flat −500 was the clinical-obesity
# default (ACSM/AHA) applied to everyone: 18% of the founder's TDEE, 26% of a
# 16-year-old's, 31% of a small woman's. Evidence for trained/lean people is 0.5–0.7%
# bodyweight/week ≈ 10–20% of TDEE (ISSN, Helms, Garthe); 500 kcal/day is the CEILING
# at which lifting stops adding lean mass (Murphy & Koehler 2022), not the default.
DEFICIT_ADULT = 0.15          # fat loss, adult
DEFICIT_ADULT_OBESE = 0.20    # fat loss, adult, BMI ≥ 30 (ISSN: higher baseline fat → more aggressive is OK)
DEFICIT_TEEN = 0.10           # fat loss, under 18 (pediatric: maintain / gradual; AAP: don't "diet" them)
DEFICIT_OLDER = 0.10          # fat loss, 65+ (Villareal: lean mass + bone go first)
RECOMP_DEFICIT = 0.10         # fat_loss + muscle (Barakat: maintenance to −200; this is the aggressive end)
SURPLUS_BUILD = 0.10          # muscle/strength, beginner–intermediate (Iraki: +10–20%)
SURPLUS_BUILD_ADVANCED = 0.05 # muscle/strength, advanced (Iraki: slower, less fat)
SURPLUS_ENDURANCE = 0.05
OBESE_BMI = 30.0
OLDER_AGE = 65
# Ceilings on the daily deficit (code-computed, applied after the percentage):
DEFICIT_CEILING_TRAINING = 500        # kcal/day — Murphy & Koehler 2022
KCAL_PER_LB_FAT_PER_DAY = 31          # Alpert 2005: ~290 kJ/kg fat/day the fat store can supply
# Floor on intake: energy availability ≥ 30 kcal/kg fat-free mass after training (IOC RED-S).
EA_FLOOR_KCAL_PER_KG_FFM = 30
TRAINING_KCAL_PER_SESSION = 250       # conservative per-session expenditure for the EA floor


def body_fat_fraction(user, bmi: float | None, age: int, is_male: bool) -> float:
    """body_fat_pct when they gave one; else Deurenberg 1991 (BF% = 1.2·BMI + 0.23·age −
    10.8·male − 5.4), clamped to 5–60%. Only used for the deficit ceiling and the EA
    floor — never shown as a fact."""
    given = getattr(user, "body_fat_pct", None)
    if given and 3 <= float(given) <= 70:
        return float(given) / 100.0
    if not bmi:
        return 0.25
    bf = (1.2 * bmi + 0.23 * age - (10.8 if is_male else 0.0) - 5.4) / 100.0
    return max(0.05, min(0.60, bf))


def goal_profile(user) -> dict:
    """The per-user inputs the goal rule needs, derived ONCE here so calculate_targets
    and adaptive_targets apply the identical rule (a re-application at a new maintenance
    must not silently drop the ceilings/floors)."""
    weight_lbs = float(getattr(user, "weight_lbs", None) or 150)
    height_in = getattr(user, "height_in", None)
    height_cm = ((getattr(user, "height_ft", None) or 5) * 12 + (height_in if height_in is not None else 7)) * 2.54
    age = getattr(user, "age", None) or 25
    gender = getattr(user, "gender", None) or "male"
    is_male = gender in ("male", "prefer_not_to_say")
    weight_kg = weight_lbs * 0.453592
    bmi = weight_kg / (height_cm / 100.0) ** 2 if height_cm else None
    bf = body_fat_fraction(user, bmi, age, is_male)
    days = training_days_per_week(getattr(user, "workout_days", None))
    return {
        "age": age,
        "bmi": bmi,
        "experience": (getattr(user, "experience", None) or "").lower() or None,
        "trains": days > 0,
        "fat_mass_lb": weight_lbs * bf,
        "ffm_kg": weight_kg * (1 - bf),
        "training_kcal_per_day": days * TRAINING_KCAL_PER_SESSION / 7.0,
    }


def goal_pct(goal: str, *, age: int | None = None, bmi: float | None = None,
             experience: str | None = None) -> tuple[float, str]:
    """(signed fraction of maintenance, goal_label)."""
    goal = goal or "general_fitness"
    obese = bmi is not None and bmi >= OBESE_BMI
    adult = age is None or TEEN_MAX_AGE < age < OLDER_AGE
    if "fat_loss" in goal and "muscle" in goal and not (obese and adult):
        # Recomp (hold near maintenance, swap fat for muscle) is a LEAN person's goal. At
        # BMI ≥ 30 the fat-loss rule applies whatever else they ticked: live users 80 and
        # 86 (BMI 45 and 32) both ticked muscle too and got −10% instead of −20%.
        return -RECOMP_DEFICIT, "recomp"
    if "fat_loss" in goal:
        if age is not None and age <= TEEN_MAX_AGE:
            return -DEFICIT_TEEN, "cutting"
        if age is not None and age >= OLDER_AGE:
            return -DEFICIT_OLDER, "cutting"
        if bmi is not None and bmi >= OBESE_BMI:
            return -DEFICIT_ADULT_OBESE, "cutting"
        return -DEFICIT_ADULT, "cutting"
    if "muscle" in goal or "strength" in goal:
        return (SURPLUS_BUILD_ADVANCED if experience == "advanced" else SURPLUS_BUILD), "building"
    if "endurance" in goal:
        return SURPLUS_ENDURANCE, "endurance"
    return 0.0, "maintenance"


def apply_goal(tdee: int, goal: str, *, age=None, bmi=None, experience=None, trains=True,
               fat_mass_lb=None, ffm_kg=None, training_kcal_per_day=0.0) -> dict:
    """The goal rule on a maintenance number → {"calories", "goal_label", "pct", "limits"}.
    Shared by calculate_targets (onboarding) and adaptive_targets (biweekly
    re-application); pass **goal_profile(user) so both see the same ceilings/floors.
    Called bare (tdee, goal) it is the adult percentage rule with no limits.

    Order: percentage → deficit ceilings (500/day if they train; 31 kcal per lb of fat
    mass) → intake floor (30 kcal/kg FFM + training). `limits` names whichever bound
    actually moved the number, so the log line can say why."""
    pct, label = goal_pct(goal, age=age, bmi=bmi, experience=experience)
    calories = tdee * (1 + pct)
    limits = []
    if pct < 0:
        deficit = tdee - calories
        # The 500/day ceiling is the lean-lifter bound (Murphy & Koehler: where lifting stops
        # adding lean mass). The header above calls 500 the clinical-OBESITY default, so at
        # BMI ≥ 30 it is a floor on ambition, not a ceiling — the fat-mass bound below is
        # the one that applies there.
        if trains and deficit > DEFICIT_CEILING_TRAINING and not (bmi is not None and bmi >= OBESE_BMI):
            deficit, _ = DEFICIT_CEILING_TRAINING, limits.append("deficit≤500 (training)")
        if fat_mass_lb is not None and deficit > KCAL_PER_LB_FAT_PER_DAY * fat_mass_lb:
            deficit, _ = KCAL_PER_LB_FAT_PER_DAY * fat_mass_lb, limits.append("deficit≤31kcal/lb fat")
        calories = tdee - deficit
        if ffm_kg:
            ea_floor = EA_FLOOR_KCAL_PER_KG_FFM * ffm_kg + (training_kcal_per_day or 0.0)
            if calories < ea_floor:
                calories, _ = ea_floor, limits.append("EA floor 30kcal/kg FFM")
    return {"calories": int(round(calories / 50.0) * 50), "goal_label": label, "pct": pct,
            "limits": limits}


def _effective_avg_steps(user, session=None):
    """The step count the activity multiplier should use. Normally the static
    onboarding user.avg_steps; but when TDEE_WEARABLE_STEPS_ENABLED is on AND a
    session is available AND the user has recent wearable step data, prefer a
    trailing average of those REAL steps. Fail-open to avg_steps on any miss."""
    static = getattr(user, "avg_steps", None)
    if not config.TDEE_WEARABLE_STEPS_ENABLED or session is None:
        return static
    try:
        from wearable_read import recent_step_avg
        real = recent_step_avg(user, session)
        if real:
            return round(real)
    except Exception:
        pass
    return static


def _untrained(user) -> bool:
    """True for a stated "none" / "beginner" experience (under 6 months): the Ten Haaf
    population is people already training, so these stay on Mifflin however many days
    they plan. An unknown experience keeps today's behaviour."""
    if not getattr(config, "TEN_HAAF_EXPERIENCE_GATE_ENABLED", True):
        return False
    exp = (getattr(user, "experience", None) or "").strip().lower()
    return exp in TEN_HAAF_UNTRAINED


def reported_intake(user) -> int | None:
    """users.reported_intake when it is a usable number (0 = asked, doesn't know → None)."""
    try:
        v = int(getattr(user, "reported_intake", None) or 0)
    except (TypeError, ValueError):
        return None
    return v if REPORTED_INTAKE_MIN <= v <= REPORTED_INTAKE_MAX else None


def ramp_cap(reported: int) -> int:
    """The highest FIRST target for someone eating `reported` a day: +RAMP_MAX_STEP, to 50."""
    return int(round(reported * (1 + RAMP_MAX_STEP) / 50.0) * 50)


def calculate_targets(user, session=None) -> dict:
    """
    Calculate calorie and protein targets based on user profile.
    Uses Mifflin-St Jeor for BMR, activity multiplier for TDEE,
    then adjusts based on goal.

    `session` is optional and read-only: when supplied AND
    TDEE_WEARABLE_STEPS_ENABLED is on, the activity multiplier prefers a trailing
    average of REAL wearable steps over the static onboarding avg_steps. Every
    existing caller passing no session gets exactly today's behaviour.
    """
    # Defaults if data is somehow missing
    weight_kg = (user.weight_lbs or 150) * 0.453592
    # height_in may legitimately be 0 (5'0") — `or 7` treated it as missing and computed
    # a 5'0" user as 5'7" (live: user 33). Only None is "missing".
    height_in = user.height_in if getattr(user, "height_in", None) is not None else 7
    height_cm = ((user.height_ft or 5) * 12 + height_in) * 2.54
    age = user.age or 25
    gender = user.gender or "male"

    days_count = training_days_per_week(user.workout_days)
    is_male = gender in ("male", "prefer_not_to_say")
    eff_steps = _effective_avg_steps(user, session)
    level = activity_level_index(eff_steps, days_count)
    adult_bmi = weight_kg / (height_cm / 100.0) ** 2
    if age <= TEEN_MAX_AGE:
        # Teen: the EER IS the maintenance number. `bmr` reports the sedentary
        # (PA=1.0) requirement — the closest analog for the log line and the
        # profile page; there is no separate resting figure in this equation.
        # Overweight teens get the IOM overweight equation, the PA category is the
        # teen one (see teen_activity_level), and the result is capped at the adult
        # path × TEEN_ADULT_CEILING — the adult equations run low for a growing
        # teenager, not 60% low (live 2026-10-17, user 40: 4585 vs 3179).
        bmi = weight_kg / (height_cm / 100.0) ** 2
        overweight = bool(getattr(config, "TEEN_EER_OVERWEIGHT_ENABLED", True) and bmi >= TEEN_OVERWEIGHT_BMI)
        bmr_formula = "iom_eer_overweight" if overweight else "iom_eer"
        bmr = teen_eer(weight_kg, height_cm, age, is_male, 0, overweight=overweight)
        if getattr(config, "TEEN_EER_OVERWEIGHT_ENABLED", True):
            level = teen_activity_level(eff_steps, days_count)
        tdee_iom = round(teen_eer(weight_kg, height_cm, age, is_male, level, overweight=overweight))
        if is_male:
            adult_bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + 5
        else:
            adult_bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age - 161
        adult_tdee = round(adult_bmr * (1.2, 1.375, 1.55, 1.725)[activity_level_index(eff_steps, days_count)])
        ceiling = round(adult_tdee * TEEN_ADULT_CEILING)
        if getattr(config, "TEEN_EER_OVERWEIGHT_ENABLED", True) and tdee_iom > ceiling:
            logger.info("TEEN_TDEE_CEILING user=%s iom=%s adult=%s ceiling=%s", getattr(user, "id", None),
                        tdee_iom, adult_tdee, ceiling)
            tdee = ceiling
        else:
            tdee = tdee_iom
        multiplier = None
    elif days_count >= TEN_HAAF_MIN_DAYS and not _untrained(user) and adult_bmi < OBESE_BMI:
        # Ten Haaf was fit on lean athletes and charges 11.9 kcal per kg of TOTAL weight
        # (Mifflin: 10). At BMI ≥ 30 that is fat tissue billed at muscle rates — live
        # user 86 (5'10"/220, intermediate, 5 days): +283 at rest, +490 at maintenance.
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
        # Adult activity multiplier — steps when known (objective), else training days.
        if eff_steps:
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
    g = apply_goal(tdee, goal, **goal_profile(user))
    calories, goal_label = g["calories"], g["goal_label"]

    # Floor first: the protein share cap reads the FINAL calorie number.
    calories = max(calories, CALORIE_FLOOR)

    # Protein: 1g/lb for muscle/fat-loss/strength, 0.7g for endurance, 0.8g otherwise —
    # per lb of REFERENCE weight (actual, capped at BMI 25), then capped at 35% of
    # calories. See the module header.
    ref_lbs = reference_weight_lbs(user.weight_lbs or 150, height_cm)
    if "muscle" in goal or "strength" in goal or "fat_loss" in goal:
        g_per_lb = 1.0
    elif "endurance" in goal:
        g_per_lb = 0.7
    else:
        g_per_lb = 0.8

    def _protein_for(cal: int, per_lb: float) -> int:
        p = round(ref_lbs * per_lb)
        p = min(p, int(cal * PROTEIN_MAX_SHARE / 4))
        return max(p, PROTEIN_FLOOR)

    protein = _protein_for(calories, g_per_lb)

    # Ramp (module header): the first target never sits more than RAMP_MAX_STEP above
    # what they say they eat now. Upward only — a goal number far above their intake is
    # the infeasible case (live user 77: ate ~2000, handed 3100).
    ramp = None
    eats = reported_intake(user) if getattr(config, "TARGET_RAMP_ENABLED", True) else None
    if eats and calories > ramp_cap(eats):
        full_cal, full_pro = calories, protein
        calories = max(ramp_cap(eats), CALORIE_FLOOR)
        protein = _protein_for(calories, min(g_per_lb, PROTEIN_RAMP_G_PER_LB))
        ramp = {"from_intake": eats, "full_calories": full_cal, "full_protein": full_pro}

    return {
        "calories": calories,
        "protein": protein,
        "ramp": ramp,
        "tdee": tdee,
        "bmr": round(bmr),
        "bmr_formula": bmr_formula,
        "goal_label": goal_label,
        "goal_pct": g["pct"],
        "goal_limits": g["limits"],
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
        # Pass the session so the wearable-steps path (flag-gated, default off) can
        # read recent real steps; no-op when the flag is off.
        t = calculate_targets(user, session)
        old = (user.calorie_target, user.protein_target)
        user.calorie_target_computed = t["calories"]
        user.protein_target_computed = t["protein"]
        if getattr(user, "targets_source", None) != "user":
            user.calorie_target = t["calories"]
            user.protein_target = t["protein"]
            user.targets_source = "computed"
        new = (user.calorie_target, user.protein_target)
        changed = old != new
        if changed:
            # Leave the same record the biweekly cycle leaves, so the coach explains it
            # through the existing TARGET CHANGED TODAY path (adaptive_targets.
            # todays_adjustment_context) in its own voice — once, plainly. The reason
            # is prefixed so that block can say "a fix on our side", not "the scale".
            from models import TargetAdjustment
            limits = f", limit: {', '.join(t['goal_limits'])}" if t["goal_limits"] else ""
            ramp = (f", stepped from their ~{t['ramp']['from_intake']} intake (full {t['ramp']['full_calories']})"
                    if t.get("ramp") else "")
            session.add(TargetAdjustment(
                user_id=user_id, old_target=old[0], new_target=new[0], changed=True,
                reason=(f"calculator update ({t['bmr_formula']} maintenance {t['tdee']}, "
                        f"{t['goal_pct']:+.0%} goal rule{limits}{ramp}); protein {old[1]} → {new[1]}g")[:300],
            ))
        session.commit()
        logger.info("TARGETS_RECOMPUTED user=%s %s -> %s formula=%s tdee=%s pct=%+.2f limits=%s ramp=%s",
                    user_id, old, new, t["bmr_formula"], t["tdee"], t["goal_pct"], t["goal_limits"], t.get("ramp"))
        return {"old": old, "new": new, "changed": changed, "tdee": t["tdee"],
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
                          maintenance=None, intake=None, protein_intake=None) -> dict:
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
    adaptive cycle seeds from it (adaptive_targets).

    `intake` / `protein_intake`: what they say they EAT NOW (not a number they aim for).
    Stored on users.reported_intake / reported_protein_intake when plausible; the
    calculator then ramps (module header), and — unless the targets are their own pick —
    the code-owned target is rewritten to the ramped number right here, so "i eat like
    2000" lands as a real change, not advice. The result carries
    "intake": {"accepted": True, "reported": 2000, "calories": 2400, "protein": 132,
               "full_calories": 3100, "rewritten": True}."""
    from models import get_session, User
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return {"error": "user not found"}
        accepted, rejected = {}, {}
        maint_result = None
        intake_result = None
        if intake is not None or protein_intake is not None:
            intake_result = {"accepted": False}
            try:
                if intake is not None:
                    v = int(round(float(intake)))
                    if REPORTED_INTAKE_MIN <= v <= REPORTED_INTAKE_MAX:
                        user.reported_intake = v
                        intake_result.update(accepted=True, reported=v)
                    else:
                        intake_result.update(reported=v, min=REPORTED_INTAKE_MIN, max=REPORTED_INTAKE_MAX)
                if protein_intake is not None:
                    pv = int(round(float(protein_intake)))
                    if REPORTED_PROTEIN_MIN <= pv <= REPORTED_PROTEIN_MAX:
                        user.reported_protein_intake = pv
                        intake_result["reported_protein"] = pv
                        if intake is None:
                            intake_result["accepted"] = True
            except (TypeError, ValueError):
                intake_result = {"accepted": False, "reason": "not a number"}
        computed = calculate_targets(user)
        if intake_result and intake_result.get("accepted"):
            intake_result.update(calories=computed["calories"], protein=computed["protein"],
                                 full_calories=(computed["ramp"] or {}).get("full_calories", computed["calories"]),
                                 full_protein=(computed["ramp"] or {}).get("full_protein", computed["protein"]),
                                 rewritten=False)
            user.calorie_target_computed = computed["calories"]
            user.protein_target_computed = computed["protein"]
            if getattr(user, "targets_source", None) != "user" and (
                    user.calorie_target != computed["calories"] or user.protein_target != computed["protein"]):
                intake_result.update(previous=(user.calorie_target, user.protein_target), rewritten=True)
                user.calorie_target = computed["calories"]
                user.protein_target = computed["protein"]
                user.targets_source = "computed"
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
            basis_cal = max(apply_goal(int(stored_m), user.goal or "", **goal_profile(user))["calories"],
                            CALORIE_FLOOR)
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
        if accepted or (maint_result and maint_result.get("accepted")) or (intake_result and intake_result.get("accepted")):
            session.commit()
        if maint_result:
            logger.info("TARGETS_REPORTED_MAINTENANCE user=%s %s", user_id, maint_result)
        if intake_result:
            logger.info("TARGETS_REPORTED_INTAKE user=%s %s", user_id, intake_result)
        if rejected:
            logger.info("TARGETS_OVERRIDE_REJECTED user=%s rejected=%s", user_id, rejected)
        out = {"accepted": accepted, "rejected": rejected,
               "computed": {"calories": computed["calories"], "protein": computed["protein"]},
               "current": {"calories": user.calorie_target, "protein": user.protein_target}}
        if maint_result is not None:
            out["maintenance"] = maint_result
        if intake_result is not None:
            out["intake"] = intake_result
        return out
    finally:
        session.close()
