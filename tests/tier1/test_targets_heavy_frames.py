"""
Heavy frames (2026-10-29): every bias in the calculator pointed the same way for an
overweight user and nothing bounded the high side. Live: user 80 (26M, 5'11"/320, desk
job, 7.5k steps, all five goals ticked) was handed 3400; user 86 (19M, 5'10"/220,
intermediate, 5 days, 10.8k steps, fat loss + muscle, "no idea" intake, one meal a day)
was handed 3550. Four rules, each pinned here: the step scale, the obese cut over recomp,
no 500 ceiling at BMI ≥ 30, and Ten Haaf needing a lean frame.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from macro_calculator import (calculate_targets, activity_level_index, goal_pct, apply_goal,
                              OBESE_BMI, STEPS_LOW_ACTIVE, STEPS_ACTIVE, STEPS_VERY_ACTIVE)

JAMES = dict(height_ft=5, height_in=10, weight_lbs=220, age=19, gender="male",
             goal="fat_loss,muscle_building", experience="intermediate",
             workout_days="tue,thu,fri,sat,sun", avg_steps=10848, reported_intake=0)
ANDREW = dict(height_ft=5, height_in=11, weight_lbs=320, age=26, gender="male",
              goal="fat_loss,muscle_building,strength,general_fitness,endurance", experience="none",
              workout_days="3", avg_steps=7500, reported_intake=0)


def _u(**kw):
    base = dict(body_fat_pct=None, reported_maintenance=None)
    base.update(kw)
    return NS(**base)


# ─── the two live rows ───────────────────────────────────────────────────────

def test_live_2026_10_29_user_86_is_not_told_3550():
    """Was: Ten Haaf 2302 × 1.725 (10.8k steps = top bucket) = 3971, −10% recomp → 3550.
    Now: BMI 31.6 → Mifflin 2019; 10.8k steps = active ×1.55 → 3130; fat-loss rule −20%
    (recomp is a lean plan) = 626 off, no 500 ceiling at BMI ≥ 30 → 2504 → 2500."""
    t = calculate_targets(_u(**JAMES))
    assert t["bmr_formula"] == "mifflin" and t["bmr"] == 2019
    assert t["activity_level"] == 2 and t["tdee"] == 3130
    assert t["goal_label"] == "cutting" and t["goal_pct"] == -0.20
    assert t["goal_limits"] == []
    assert t["calories"] == 2500 and t["protein"] == 174


def test_live_2026_10_28_user_80_is_not_told_3400():
    """Was: Mifflin 2454 × 1.55 (7.5k steps sat exactly on the old "active" line) = 3803,
    −10% recomp → 3400. Now: 7.5k = low active ×1.375 → 3374; BMI 44.6 → −20% → 2699 → 2700."""
    t = calculate_targets(_u(**ANDREW))
    assert t["bmr_formula"] == "mifflin"
    assert t["activity_level"] == 1 and t["tdee"] == 3374
    assert t["goal_pct"] == -0.20 and t["goal_limits"] == []
    assert t["calories"] == 2700 and t["protein"] == 179


# ─── 1. the step scale ───────────────────────────────────────────────────────

@pytest.mark.parametrize("steps,level", [
    (3000, 0), (4999, 0), (5000, 1), (7500, 1), (9999, 1), (10000, 2), (10848, 2), (12499, 2),
    (12500, 3), (15000, 3),
])
def test_adult_step_buckets_follow_tudor_locke(steps, level):
    assert activity_level_index(steps, 3) == level


def test_step_cutoffs():
    assert (STEPS_LOW_ACTIVE, STEPS_ACTIVE, STEPS_VERY_ACTIVE) == (5000, 10000, 12500)


def test_days_fallback_is_unchanged():
    assert activity_level_index(None, 2) == 1 and activity_level_index(None, 4) == 2
    assert activity_level_index(None, 5) == 3


def test_ten_k_steps_is_active_not_very_active():
    lean = dict(JAMES, weight_lbs=150, experience="none", workout_days="4")
    assert calculate_targets(_u(**dict(lean, avg_steps=10000)))["tdee"] == \
        calculate_targets(_u(**dict(lean, avg_steps=12000)))["tdee"]
    assert calculate_targets(_u(**dict(lean, avg_steps=12500)))["tdee"] > \
        calculate_targets(_u(**dict(lean, avg_steps=12000)))["tdee"]


# ─── 2. the obese cut beats recomp ───────────────────────────────────────────

def test_recomp_is_a_lean_plan():
    assert goal_pct("fat_loss,muscle_building", age=20, bmi=22.9) == (-0.10, "recomp")
    assert goal_pct("fat_loss,muscle_building", age=20, bmi=29.9) == (-0.10, "recomp")
    assert goal_pct("fat_loss,muscle_building", age=20, bmi=30.0) == (-0.20, "cutting")
    assert goal_pct("fat_loss,muscle_building,strength,endurance", age=26, bmi=44.6) == (-0.20, "cutting")


def test_unknown_bmi_keeps_recomp():
    assert goal_pct("fat_loss,muscle_building", age=20, bmi=None) == (-0.10, "recomp")


def test_teen_and_older_obese_keep_their_gentler_rules():
    # a minor is never put on a 20% cut (pediatric guidance); 65+ loses lean mass first
    assert goal_pct("fat_loss,muscle_building", age=17, bmi=35.4) == (-0.10, "recomp")
    assert goal_pct("fat_loss", age=17, bmi=35.4) == (-0.10, "cutting")
    assert goal_pct("fat_loss,muscle_building", age=70, bmi=33.0) == (-0.10, "recomp")
    assert goal_pct("fat_loss", age=70, bmi=33.0) == (-0.10, "cutting")


# ─── 3. the 500 ceiling is the lean-lifter bound ─────────────────────────────

def test_no_500_ceiling_at_bmi_30():
    g = apply_goal(3400, "fat_loss", age=30, bmi=36.0, trains=True, fat_mass_lb=120, ffm_kg=70,
                   training_kcal_per_day=143)
    assert g["pct"] == -0.20 and g["calories"] == 2700 and g["limits"] == []


def test_500_ceiling_still_binds_below_bmi_30():
    g = apply_goal(3450, "fat_loss", age=25, bmi=29.2, trains=True, fat_mass_lb=50, ffm_kg=75,
                   training_kcal_per_day=143)
    assert g["limits"] == ["deficit≤500 (training)"] and g["calories"] == 2950


def test_fat_mass_and_ea_bounds_still_apply_at_bmi_30():
    # a heavy frame reported as very lean: the fat-mass bound is the one that governs
    g = apply_goal(3400, "fat_loss", age=30, bmi=31.0, trains=True, fat_mass_lb=12, ffm_kg=90,
                   training_kcal_per_day=143)
    assert "deficit≤31kcal/lb fat" in g["limits"]


# ─── 4. Ten Haaf needs a lean frame ──────────────────────────────────────────

def test_ten_haaf_is_off_at_bmi_30_even_for_an_intermediate_five_day_lifter():
    assert calculate_targets(_u(**JAMES))["bmr_formula"] == "mifflin"
    assert calculate_targets(_u(**dict(JAMES, weight_lbs=210)))["bmr_formula"] == "mifflin"   # BMI 30.1


def test_ten_haaf_returns_once_the_frame_is_lean():
    for lbs in (200, 180):                                     # BMI 28.7, 25.8
        assert calculate_targets(_u(**dict(JAMES, weight_lbs=lbs)))["bmr_formula"] == "ten_haaf"


def test_obese_bmi_constant_is_the_who_cutoff():
    assert OBESE_BMI == 30.0
