"""
Tier-2 (live) anchors for interpret.py — the four day-one targets a human corrected within the
hour, and the first-card answers a regex misread. Binary bands, x2 each.
Run: pytest tests/tier2/test_interpret_live.py --run-tier2 -s
"""

from __future__ import annotations

import pytest

from tests.factories import make_user

pytestmark = pytest.mark.tier2

# (profile, formula seen live, what a human set)
CASES = {
    "jayden_4150": (dict(name="Jayden", age=17, gender="male", height_ft=5, height_in=4, weight_lbs=200, goal="fat_loss",
                         experience="none", equipment="machines", workout_days="3", activity_level="soccer team manager, on feet 4-8:30pm",
                         occupation="high school", cooking_situation="family cooks", reported_intake=None, biggest_obstacle="consistency"), 2500),
    "andrew_3400": (dict(name="Andrew", age=26, gender="male", height_ft=5, height_in=11, weight_lbs=320, goal="fat_loss",
                         experience="none", equipment="home_gym", workout_days="3", activity_level="desk job, early hours; some days <5k steps",
                         avg_steps=7500, occupation="early morning job", cooking_situation="cook", reported_intake=None,
                         biggest_obstacle="consistency", injuries="lower back / foot / calf pain when walking a lot"), 2550),
    "james_3550": (dict(name="James", age=19, gender="male", height_ft=5, height_in=10, weight_lbs=220, goal="fat_loss",
                        experience="intermediate", equipment="full_gym", workout_days="5", activity_level="volleyball 3x/week, dining hall job 12h/wk",
                        occupation="civil eng student", cooking_situation="dining_hall", reported_intake=None,
                        food_context="eats one big meal a day", biggest_obstacle="nutrition"), 2400),
    "alexey_3100": (dict(name="Alexey", age=22, gender="male", height_ft=6, height_in=2, weight_lbs=165, goal="muscle_building,strength",
                         experience="intermediate", equipment="full_gym", workout_days="6", activity_level="mostly sitting, physics major",
                         occupation="physics student", cooking_situation="frat house meals, fixed portions", reported_intake=2000,
                         food_context="tight food budget, house meals", biggest_obstacle="consistency"), 2400),
}


@pytest.mark.parametrize("i", range(2))
@pytest.mark.parametrize("case", list(CASES))
def test_day_one_targets_land_near_the_human_number(db, case, i):
    from interpret import with_review
    from macro_calculator import calculate_targets
    profile, human = CASES[case]
    u = make_user(db, onboarding_step=2, **profile)
    f = calculate_targets(u)
    t = with_review(u, f)
    print(f"\n[{case} {i}] formula={f['calories']}/{f['protein']} → {t['calories']}/{t['protein']} human={human} "
          f"reason={t.get('review', {}).get('reason')!r}")
    assert human * 0.85 <= t["calories"] <= human * 1.15, (f["calories"], t["calories"], human)


ANSWERS = [
    # (level, ask, text, expected (exercise prefix, reps or None, is-no-number))
    ("none", "heaviest u've benched or squatted, even just the bar", "like no clue i think heaviest squat was 135", ("squat", 1, False)),
    ("intermediate", "what do u bench and squat for like 5", "All time PRs: Bench 185lb for 1. But I can do like 135 for 10 lwk", ("bench", 10, False)),
    ("none", "heaviest u've benched or squatted, even just the bar", "no clue, never really lifted", (None, None, True)),
    ("none", "heaviest u've benched or squatted, even just the bar", "just the bar on bench lol", ("bench", None, False)),
]


@pytest.mark.parametrize("i", range(2))
@pytest.mark.parametrize("level, ask, text, expect", ANSWERS)
def test_first_card_answers_are_read_like_a_coach_would(db, level, ask, text, expect, i):
    from interpret import interpret_lift_answer
    u = make_user(db, onboarding_step=3, experience=level, gender="female" if level == "none" else "male", weight_lbs=115 if level == "none" else 180)
    r = interpret_lift_answer(u, ask, text)
    print(f"\n[{i}] {text!r} → {r}")
    ex, reps, no_number = expect
    if r is None:
        # nothing to read (no digit, no lift word) → the regex no-number path decides, as designed
        from workouts.calibrate import is_no_number_answer
        assert no_number and is_no_number_answer(text), text
        return
    assert r["no_number"] is no_number
    if ex:
        items = [it for it in r["items"] if it["exercise"].lower().startswith(ex)]
        assert items, r
        if ex == "bench" and reps == 10:
            assert any(it.get("reps") == 10 and it["weight"] == 135 for it in items), items
        elif reps is not None:
            assert items[-1].get("reps") == reps, items
        if ex == "bench" and reps is None:
            assert items[0]["weight"] == 45
