"""
One calculator, two base-rate formulas (founder, 2026-09-14): Ten Haaf for a clear
5+ training days a week (the athlete-validated equation), Mifflin-St Jeor otherwise.
Numbers below are pinned by hand from the published coefficients.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from macro_calculator import calculate_targets, training_days_per_week, TEN_HAAF_MIN_DAYS


def _u(**kw):
    base = dict(height_ft=5, height_in=6, weight_lbs=139, age=20, gender="male",
                goal="fat_loss,muscle_building", workout_days="4-5", avg_steps=10000)
    base.update(kw)
    return NS(**base)


@pytest.mark.parametrize("raw,expected", [
    ("5", 5), ("4-5", 4), ("4–5", 4), ("4 to 5", 4), ("5+", 5), ("6+", 6),
    ("mon,wed,fri", 3), ("mon, wed, fri, sat", 4), ("3 days", 3), ("like 4", 4),
    (None, 3), ("", 3), ("whenever", 3), (5, 5),
])
def test_training_days_parser(raw, expected):
    assert training_days_per_week(raw) == expected


def test_founder_row_stays_on_mifflin_with_a_4_to_5_range():
    """'4-5' is its lower bound → 4 → Mifflin. His live 2450 must not move."""
    t = calculate_targets(_u(workout_days="4-5"))
    assert t["bmr_formula"] == "mifflin"
    assert t["bmr"] == 1583 and t["tdee"] == 2731 and t["calories"] == 2450 and t["protein"] == 139


def test_five_days_switches_to_ten_haaf():
    t = calculate_targets(_u(workout_days="5"))
    assert t["bmr_formula"] == "ten_haaf"
    # 11.936*63.05 + 587.728*1.6764 - 8.129*20 + 191.027 + 29.279 = 1795.6
    assert t["bmr"] == 1796
    assert t["tdee"] == round(1795.6 * 1.725)  # 10k+ steps → 1.725
    assert t["calories"] == round(t["tdee"] * 0.9 / 50) * 50


def test_ten_haaf_sex_term_is_191_for_men_only():
    m = calculate_targets(_u(workout_days="6", gender="male"))
    f = calculate_targets(_u(workout_days="6", gender="female"))
    assert m["bmr_formula"] == f["bmr_formula"] == "ten_haaf"
    assert m["bmr"] - f["bmr"] == 191


def test_threshold_is_exactly_five():
    assert TEN_HAAF_MIN_DAYS == 5
    assert calculate_targets(_u(workout_days="4"))["bmr_formula"] == "mifflin"
    assert calculate_targets(_u(workout_days="5"))["bmr_formula"] == "ten_haaf"
    assert calculate_targets(_u(workout_days="mon,tue,wed,thu,fri"))["bmr_formula"] == "ten_haaf"


def test_no_steps_falls_back_to_days_multiplier_with_ten_haaf_base():
    t = calculate_targets(_u(workout_days="5", avg_steps=None))
    assert t["bmr_formula"] == "ten_haaf" and t["tdee"] == round(1795.6 * 1.65)


def test_dead_calculator_is_gone():
    import macro_calculator as mc
    for name in ("compute_targets", "get_or_compute_targets", "MacroResult", "ACTIVITY_MULTIPLIERS"):
        assert not hasattr(mc, name), name
