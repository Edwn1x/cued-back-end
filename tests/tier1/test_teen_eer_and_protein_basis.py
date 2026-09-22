"""
Calculator changes from the user-32 review (2026-09-22): the IOM Estimated Energy
Requirement replaces the adult equations under 18; protein runs per lb of a
REFERENCE weight (actual capped at BMI 25) and never above 35% of calories; a
maintenance number the user reports from their own tracking centres the override
band and seeds the adaptive cycle. Numbers pinned by hand from the published
coefficients. The founder's live row (139 lb / 5'6" / 2450 / 139) must not move.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from macro_calculator import (calculate_targets, teen_eer, reference_weight_lbs,
                              activity_level_index, apply_target_override, recompute_targets,
                              override_bounds)
from tests.factories import make_user


def _u(**kw):
    base = dict(height_ft=5, height_in=6, weight_lbs=139, age=20, gender="male",
                goal="fat_loss,muscle_building", workout_days="4-5", avg_steps=10000)
    base.update(kw)
    return NS(**base)


AISLINN = dict(height_ft=5, height_in=2, weight_lbs=172.6, age=16, gender="female",
               goal="fat_loss", workout_days="3-4", avg_steps=4000)


# ── teen EER ────────────────────────────────────────────────────────────────

def test_iom_eer_girls_sedentary_pinned():
    # 135.3 − 30.8·16 + 1.00·(10·78.29 + 934·1.5748) + 25 = 1921.3
    kg, cm = 172.6 * 0.453592, 62 * 2.54
    assert round(teen_eer(kg, cm, 16, False, 0)) == 1921
    # low active PA 1.16 → 2282
    assert round(teen_eer(kg, cm, 16, False, 1)) == 2282
    # at her onboarding weight (180) the sedentary EER is 1955
    assert round(teen_eer(180 * 0.453592, cm, 16, False, 0)) == 1955


def test_iom_eer_boys_pinned():
    # 88.5 − 61.9·17 + 1.13·(26.7·70 + 903·1.75) + 25 = 88.5 − 1052.3 + 1.13·3448.25 + 25
    kg, m = 70.0, 1.75
    assert round(teen_eer(kg, m * 100, 17, True, 1)) == round(88.5 - 1052.3 + 1.13 * (26.7 * 70 + 903 * 1.75) + 25)


def test_under_18_uses_the_eer_as_maintenance_no_multiplier():
    t = calculate_targets(_u(**AISLINN))
    assert t["bmr_formula"] == "iom_eer"
    assert t["tdee"] == 1921                    # sedentary (4k steps) PA = 1.0
    assert t["calories"] == 1750                # teen −10% → 1729 → 1750 (was 1400 = −500 → floor)
    assert t["goal_pct"] == -0.10 and t["goal_limits"] == []
    assert t["activity_level"] == 0
    # at 180 lb / 5'2" the same path gives 1955 → 1750 as well
    assert calculate_targets(_u(**dict(AISLINN, weight_lbs=180)))["calories"] == 1750


def test_same_body_at_18_is_adult_obese_rule_with_the_ea_floor():
    """At 18 the adult path applies: Mifflin 1516 × 1.2 = 1819, BMI 31.6 ≥ 30 → −20% =
    364 → 1455; but the energy-availability floor (30 × ~49.6 kg FFM + training ≈ 1596)
    binds → 1600, and the limit is named. (The old flat −500 gave 1319 → the 1400 floor.)"""
    t = calculate_targets(_u(**dict(AISLINN, age=18)))
    assert t["bmr_formula"] == "mifflin" and t["goal_pct"] == -0.20
    assert t["calories"] == 1600 and "EA floor 30kcal/kg FFM" in t["goal_limits"]


def test_eighteen_is_adult_seventeen_is_teen():
    assert calculate_targets(_u(**dict(AISLINN, age=18)))["bmr_formula"] == "mifflin"
    assert calculate_targets(_u(**dict(AISLINN, age=17)))["bmr_formula"] == "iom_eer"


def test_teen_activity_follows_the_same_buckets():
    lo = calculate_targets(_u(**dict(AISLINN, avg_steps=4000)))["tdee"]
    mid = calculate_targets(_u(**dict(AISLINN, avg_steps=6000)))["tdee"]
    hi = calculate_targets(_u(**dict(AISLINN, avg_steps=12000)))["tdee"]
    assert lo == 1921 and mid == 2282 and hi > mid
    assert activity_level_index(None, 3) == 2 and activity_level_index(None, 5) == 3


# ── protein basis ───────────────────────────────────────────────────────────

def test_reference_weight_caps_at_bmi_25():
    cm = 62 * 2.54                              # 5'2" → BMI 25 at 62.0 kg = 136.7 lb
    assert round(reference_weight_lbs(172.6, cm), 1) == 136.7
    assert reference_weight_lbs(120, cm) == 120  # under the cap → actual


def test_protein_share_cap_binds_on_a_small_budget():
    t = calculate_targets(_u(**AISLINN))
    # 136.7 → 137 g; the 35% cap at 1750 is 153 so the reference weight binds → 137 (was 173)
    assert t["protein"] == 137
    assert t["protein"] * 4 <= t["calories"] * 0.35 + 4
    # on a 1400 budget the share cap would bind instead: 122
    from macro_calculator import PROTEIN_MAX_SHARE
    assert int(1400 * PROTEIN_MAX_SHARE / 4) == 122


def test_founder_row_does_not_move():
    t = calculate_targets(_u())
    assert (t["bmr_formula"], t["tdee"], t["calories"], t["protein"]) == ("mifflin", 2731, 2450, 139)
    assert t["reference_weight_lbs"] == 139


def test_protein_floor_still_applies():
    t = calculate_targets(_u(weight_lbs=90, height_ft=4, height_in=11, goal="general_fitness", age=30,
                             gender="female", avg_steps=3000))
    assert t["protein"] >= 80


# ── reported maintenance ────────────────────────────────────────────────────

def test_reported_maintenance_within_range_centres_the_band(db):
    user = make_user(db, onboarding_step=3, calorie_target=1750, protein_target=137,
                     targets_source="computed", **AISLINN)
    # computed tdee 1921; 2200 is +14.5% → accepted; basis = 2200 × 0.9 (teen) = 1980 → 2000 → band 1700–2300
    r = apply_target_override(user.id, calories=1700, maintenance=2200, note="app says 2200")
    assert r["maintenance"]["accepted"] is True and r["maintenance"]["basis_calories"] == 2000
    assert r["accepted"] == {"calories": 1700} and r["rejected"] == {}
    from models import User
    db.expire_all()
    u = db.get(User, user.id)
    assert u.reported_maintenance == 2200 and u.calorie_target == 1700 and u.targets_source == "user"
    assert u.calorie_target_computed == 1750  # the computed pair is still recorded beside it


def test_without_a_reported_maintenance_1400_is_out_of_band(db):
    user = make_user(db, onboarding_step=3, calorie_target=1750, protein_target=137, **AISLINN)
    r = apply_target_override(user.id, calories=1400)
    assert r["accepted"] == {} and r["rejected"]["calories"]["min"] == override_bounds(1750)[0]


def test_reported_maintenance_too_far_is_rejected_and_not_stored(db):
    user = make_user(db, onboarding_step=3, calorie_target=1750, protein_target=137, **AISLINN)
    r = apply_target_override(user.id, calories=2500, maintenance=3000)
    assert r["maintenance"]["accepted"] is False and "min" in r["maintenance"]
    assert r["accepted"] == {}
    from models import User
    db.expire_all()
    assert db.get(User, user.id).reported_maintenance is None


def test_stored_maintenance_keeps_centring_later_calls(db):
    user = make_user(db, onboarding_step=3, calorie_target=1750, protein_target=137, **AISLINN)
    apply_target_override(user.id, maintenance=2200)
    r = apply_target_override(user.id, calories=2250)   # inside the band around 2000, outside the band around 1750
    assert r["accepted"] == {"calories": 2250}


def test_set_targets_tool_result_strings_for_maintenance(db):
    from agent_tools import handle_set_targets
    user = make_user(db, onboarding_step=3, calorie_target=1750, protein_target=137, **AISLINN)
    out = handle_set_targets(user.id, {"maintenance": 2200, "calories": 1800, "reason": "mnd says 2200"})
    assert out.startswith("ok: noted their maintenance 2200") and "set calories 1800" in out
    out = handle_set_targets(user.id, {"maintenance": 4000})
    assert out.startswith("error: maintenance 4000 is too far")


def test_adaptive_cycle_seeds_from_reported_maintenance(db, monkeypatch):
    """The cycle's 'current maintenance' is the reported number when present."""
    import adaptive_targets as at
    from datetime import datetime, timezone, timedelta
    from models import get_session, Meal, WeightLog, User
    user = make_user(db, calorie_target=1700, protein_target=137, targets_source="user",
                     reported_maintenance=2200, **AISLINN)
    seen = {}
    real = at.apply_goal

    def spy(maint, goal, **kw):
        seen["maint"] = maint
        return real(maint, goal, **kw)
    monkeypatch.setattr(at, "apply_goal", spy)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    s = get_session()
    try:
        u = s.get(User, user.id)
        u.created_at = now - timedelta(days=30)
        for d in range(14):
            day = now - timedelta(days=d)
            for i in range(3):
                s.add(Meal(user_id=user.id, description=f"meal {i}", calories=1700 // 3,
                           eaten_at=day.replace(hour=12 + i * 3, minute=0, second=0, microsecond=0)))
        for days_ago, lbs in [(13, 172.6), (10, 172.6), (7, 172.6), (3, 172.6), (0, 172.6)]:
            s.add(WeightLog(user_id=user.id, weighed_at=now - timedelta(days=days_ago, hours=2), weight_lbs=lbs))
        s.commit()
        r = at.evaluate(s.get(User, user.id), s)
    finally:
        s.close()
    assert seen, f"apply_goal never reached — {r}"
    # est ≈ 1700 (flat scale); new_maint = DAMP·1700 + (1−DAMP)·2200, NOT ·1921
    assert abs(seen["maint"] - round(at.DAMP * 1700 + (1 - at.DAMP) * 2200)) <= 2, (seen, r)


# ── recompute for an existing row ───────────────────────────────────────────

def test_recompute_moves_a_computed_row_and_leaves_a_user_pick(db):
    user = make_user(db, onboarding_step=3, calorie_target=1400, protein_target=173,
                     calorie_target_computed=1400, protein_target_computed=180,
                     targets_source="computed", **AISLINN)
    r = recompute_targets(user.id)
    assert r["old"] == (1400, 173) and r["new"] == (1750, 137) and r["changed"]
    picked = make_user(db, onboarding_step=3, calorie_target=1600, protein_target=140,
                       targets_source="user", **dict(AISLINN, phone="+15550000099"))
    r2 = recompute_targets(picked.id)
    assert r2["new"] == (1600, 140) and not r2["changed"]
    from models import User
    db.expire_all()
    assert db.get(User, picked.id).protein_target_computed == 137


# ── goal rule: percentages + ceilings + floors ──────────────────────────────

def test_goal_pct_table():
    from macro_calculator import goal_pct
    assert goal_pct("fat_loss", age=25, bmi=24) == (-0.15, "cutting")
    assert goal_pct("fat_loss", age=25, bmi=31) == (-0.20, "cutting")
    assert goal_pct("fat_loss", age=16, bmi=31) == (-0.10, "cutting")      # teen beats obese
    assert goal_pct("fat_loss", age=70, bmi=31) == (-0.10, "cutting")
    assert goal_pct("fat_loss,muscle_building", age=25, bmi=24) == (-0.10, "recomp")
    assert goal_pct("muscle_building", experience="beginner") == (0.10, "building")
    assert goal_pct("muscle_building", experience="advanced") == (0.05, "building")
    assert goal_pct("endurance") == (0.05, "endurance")
    assert goal_pct("general_fitness") == (0.0, "maintenance")


def test_bare_apply_goal_is_the_adult_percentage_rule():
    from macro_calculator import apply_goal
    assert apply_goal(2000, "fat_loss")["calories"] == 1700
    assert apply_goal(2000, "muscle_building")["calories"] == 2200
    assert apply_goal(2000, "fat_loss,muscle_building")["calories"] == 1800


def test_krla_like_user_lands_on_1550():
    """20 F, 5'0", 137 lb, 6500 steps, fat_loss(+endurance): Mifflin 1313 × 1.375 = 1805
    → −15% = 271 → 1534 → 1550 (flat −500 gave 1305 → floor 1400)."""
    t = calculate_targets(_u(height_ft=5, height_in=0, weight_lbs=137, age=20, gender="female",
                             goal="fat_loss,endurance", workout_days="most days", avg_steps=6500))
    assert t["tdee"] == 1805 and t["calories"] == 1550 and t["goal_pct"] == -0.15
    assert t["protein"] == 128            # BMI-25 reference weight at 5'0" (was 137 = 1 g/lb)


def test_zero_inches_is_a_height_not_a_missing_value():
    """Live bug (user 33, 5'0"): `height_in or 7` computed her as 5'7"."""
    short = calculate_targets(_u(height_ft=5, height_in=0, weight_lbs=137, age=20, gender="female",
                                 goal="fat_loss", workout_days="3", avg_steps=6500))
    tall = calculate_targets(_u(height_ft=5, height_in=7, weight_lbs=137, age=20, gender="female",
                                goal="fat_loss", workout_days="3", avg_steps=6500))
    assert short["bmr"] < tall["bmr"] and short["bmr"] == 1313


def test_training_ceiling_binds_for_a_heavy_lifter():
    """300 lb adult, BMI ≥ 30 → −20% of a ~3400 TDEE would be ~680; capped at 500."""
    t = calculate_targets(_u(height_ft=5, height_in=10, weight_lbs=300, age=30, avg_steps=6000,
                             goal="fat_loss", workout_days="4"))
    assert t["goal_pct"] == -0.20
    assert "deficit≤500 (training)" in t["goal_limits"]
    assert t["tdee"] - t["calories"] in (450, 500, 550)   # 500, then rounded to 50


def test_fat_mass_ceiling_binds_for_a_lean_lifter():
    """Alpert: a very lean person can't fund a 15% deficit from fat alone."""
    t = calculate_targets(_u(height_ft=6, height_in=0, weight_lbs=150, age=25, gender="male",
                             body_fat_pct=6, goal="fat_loss", workout_days="5", avg_steps=12000))
    # fat mass 9 lb → ceiling 279 kcal/day; 15% of his ~3000 TDEE would be ~450
    assert "deficit≤31kcal/lb fat" in t["goal_limits"]
    assert t["tdee"] - t["calories"] <= 300


def test_ea_floor_binds_for_a_small_woman():
    """A 100-lb, 5'0", 25 F with an aggressive-looking BMI-neutral 15% cut: FFM ≈ 34 kg
    → floor ≈ 1020 + training; the universal 1400 floor is higher anyway, but the EA
    floor must be the one named when it binds. Use a body with FFM high enough that
    EA floor > 15%-cut: give body_fat_pct=10 so FFM is ~41 kg → floor ≈ 1230+107."""
    from macro_calculator import apply_goal
    g = apply_goal(1500, "fat_loss", age=25, bmi=22, trains=True, fat_mass_lb=10, ffm_kg=45,
                   training_kcal_per_day=107)
    # 15% → 1275; EA floor = 30·45 + 107 = 1457 → 1450
    assert g["calories"] == 1450 and "EA floor 30kcal/kg FFM" in g["limits"]


def test_goal_profile_estimates_fat_when_not_given():
    from macro_calculator import goal_profile
    p = goal_profile(_u(**AISLINN))
    assert 0.34 <= p["fat_mass_lb"] / 172.6 <= 0.38           # Deurenberg ≈ 36%
    assert 48 <= p["ffm_kg"] <= 52 and p["trains"] and p["age"] == 16
    p2 = goal_profile(_u(**dict(AISLINN, body_fat_pct=40)))
    assert abs(p2["fat_mass_lb"] - 172.6 * 0.40) < 0.01           # a given number wins
