"""
Live 2026-10-27, user 77 (Alexey): 22M, 6'2"/165, never trained, "6 days", 7000 steps,
muscle+strength → 3100 cal / 165g. He eats ~2000 / 100g and "cannot afford that much food".
The coach could offer only 2650 — the floor of the ±15% band around 3100.

Three fixes, one module:
  1. Ten Haaf (the athlete RMR equation) needs a training HISTORY, not a plan — "none" /
     "beginner" stay on Mifflin however many days they plan (2039 → 1818 RMR here).
  2. Setup asks what they eat now (users.reported_intake; 0 = asked, no idea).
  3. The first target never sits more than 20% above what they eat (ramp), protein runs at
     0.8 g/lb while ramped, the band re-centres on the ramped number, and the coach can
     record an intake mid-coaching via set_targets current_intake. The adaptive cycle
     grows a ramped start off the scale.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS

import pytest

from macro_calculator import calculate_targets, ramp_cap, RAMP_MAX_STEP, TEN_HAAF_UNTRAINED
from tests.factories import make_user

ALEXEY = dict(height_ft=6, height_in=2, weight_lbs=165, age=22, gender="male",
              goal="muscle_building,strength", workout_days="6", avg_steps=7000, experience="none")


def _u(**kw):
    base = dict(ALEXEY)
    base.update(kw)
    return NS(**base)


# ─── 1. the Ten Haaf gate ────────────────────────────────────────────────────

def test_alexey_never_trained_six_days_is_mifflin_not_ten_haaf():
    t = calculate_targets(_u())
    assert t["bmr_formula"] == "mifflin"
    assert t["bmr"] == 1818 and t["tdee"] == 2500          # 10·74.84 + 6.25·187.96 − 5·22 + 5; ×1.375 (7000 steps)
    assert t["calories"] == 2750 and t["protein"] == 165   # +10% build → 2750; 1 g/lb, no ramp (intake unknown)
    assert t["ramp"] is None


@pytest.mark.parametrize("exp", list(TEN_HAAF_UNTRAINED))
def test_untrained_levels_stay_on_mifflin(exp):
    assert calculate_targets(_u(experience=exp))["bmr_formula"] == "mifflin"


@pytest.mark.parametrize("exp", ["intermediate", "advanced", None, ""])
def test_a_training_history_or_an_unknown_one_keeps_the_five_day_rule(exp):
    """Intermediate+ earned the athlete equation; an UNKNOWN experience keeps today's behaviour
    (no existing row moves on a null)."""
    t = calculate_targets(_u(experience=exp))
    assert t["bmr_formula"] == "ten_haaf" and t["tdee"] == 2804
    assert t["calories"] == (2950 if exp == "advanced" else 3100)   # advanced builds at +5%, not +10%


def test_gate_only_bites_at_five_plus_days():
    assert calculate_targets(_u(experience="intermediate", workout_days="4"))["bmr_formula"] == "mifflin"


def test_gate_flag_off_restores_ten_haaf_for_a_beginner(monkeypatch):
    import config
    monkeypatch.setattr(config, "TEN_HAAF_EXPERIENCE_GATE_ENABLED", False)
    assert calculate_targets(_u())["bmr_formula"] == "ten_haaf"


# ─── 3. the ramp ─────────────────────────────────────────────────────────────

def test_ramp_cap_is_twenty_percent_to_the_nearest_fifty():
    assert RAMP_MAX_STEP == 0.20
    assert ramp_cap(2000) == 2400 and ramp_cap(1830) == 2200 and ramp_cap(2480) == 3000


def test_alexey_eating_2000_starts_at_2400_and_protein_at_point_eight():
    t = calculate_targets(_u(reported_intake=2000, reported_protein_intake=100))
    assert t["calories"] == 2400 and t["protein"] == 132          # 0.8 × 165 lb
    assert t["ramp"] == {"from_intake": 2000, "full_calories": 2750, "full_protein": 165}
    assert t["tdee"] == 2500 and t["bmr_formula"] == "mifflin"   # the maintenance estimate is untouched


def test_no_ramp_when_the_target_is_already_within_twenty_percent():
    t = calculate_targets(_u(reported_intake=2400))               # cap 2900 ≥ 2750
    assert t["calories"] == 2750 and t["protein"] == 165 and t["ramp"] is None


@pytest.mark.parametrize("eats", [None, 0, 300, 9000, "nope"])
def test_unknown_or_implausible_intake_is_not_a_ramp(eats):
    t = calculate_targets(_u(reported_intake=eats))
    assert t["calories"] == 2750 and t["ramp"] is None


def test_ramp_applies_on_a_cut_too_when_the_target_is_far_above_what_they_eat():
    """Direction is about the TARGET vs their intake, not the goal: a cut target 50% above a
    tiny reported intake is the same infeasible jump."""
    t = calculate_targets(_u(goal="fat_loss", weight_lbs=230, reported_intake=1200))
    assert t["ramp"] is not None and t["calories"] == 1450
    assert t["calories"] == max(ramp_cap(1200), 1400)


def test_ramp_never_breaks_the_calorie_floor_or_the_protein_share_cap():
    t = calculate_targets(_u(reported_intake=1000))               # cap 1200 < floor 1400
    assert t["calories"] == 1400
    assert t["protein"] <= int(1400 * 0.35 / 4) and t["protein"] >= 80


def test_ramp_flag_off_leaves_the_full_number(monkeypatch):
    import config
    monkeypatch.setattr(config, "TARGET_RAMP_ENABLED", False)
    t = calculate_targets(_u(reported_intake=2000))
    assert t["calories"] == 2750 and t["ramp"] is None


def test_a_user_who_already_eats_plenty_is_untouched():
    t = calculate_targets(_u(experience="intermediate", reported_intake=3000))   # 3100 ≤ 3600
    assert t["calories"] == 3100 and t["ramp"] is None


# ─── the override path: set_targets current_intake ───────────────────────────

def _row(db, uid):
    from models import User
    db.expire_all()
    u = db.get(User, uid)
    return (u.calorie_target, u.protein_target, u.calorie_target_computed, u.protein_target_computed,
            u.targets_source, u.reported_intake, u.reported_protein_intake)


def test_reported_intake_rewrites_a_code_owned_target_right_there(db):
    from macro_calculator import apply_target_override
    user = make_user(db, calorie_target=2750, protein_target=165, calorie_target_computed=2750,
                     protein_target_computed=165, targets_source="computed", reported_intake=None, **ALEXEY)
    r = apply_target_override(user.id, intake=2000, protein_intake=100)
    it = r["intake"]
    assert it["accepted"] and it["rewritten"] and it["previous"] == (2750, 165)
    assert (it["calories"], it["protein"], it["full_calories"], it["full_protein"]) == (2400, 132, 2750, 165)
    assert _row(db, user.id) == (2400, 132, 2400, 132, "computed", 2000, 100)
    assert r["current"] == {"calories": 2400, "protein": 132}


def test_reported_intake_recentres_the_band_so_their_number_is_now_reachable(db):
    """Before: band 2340–2690 around 2750 (2640–3570 around the live 3100). After the intake is
    known the band sits around 2400, so 2200 — a number near what they eat — is a legal pick."""
    from macro_calculator import apply_target_override, override_bounds
    user = make_user(db, calorie_target=2750, protein_target=165, targets_source="computed",
                     reported_intake=None, **ALEXEY)
    assert apply_target_override(user.id, calories=2200)["rejected"]["calories"]["min"] == override_bounds(2750)[0]
    r = apply_target_override(user.id, intake=2000, calories=2200)
    assert r["accepted"] == {"calories": 2200} and r["rejected"] == {}
    assert _row(db, user.id)[0] == 2200 and _row(db, user.id)[4] == "user"


def test_their_own_pick_is_not_overwritten_by_an_intake_report(db):
    from macro_calculator import apply_target_override
    user = make_user(db, calorie_target=2600, protein_target=150, targets_source="user",
                     reported_intake=None, **ALEXEY)
    r = apply_target_override(user.id, intake=2000)
    assert r["intake"]["accepted"] and r["intake"]["rewritten"] is False
    row = _row(db, user.id)
    assert row[:2] == (2600, 150) and row[4] == "user" and row[5] == 2000
    assert row[2:4] == (2400, 132)   # the computed pair beside it follows the ramp


def test_implausible_intake_is_rejected_and_writes_nothing(db):
    from macro_calculator import apply_target_override
    user = make_user(db, calorie_target=2750, protein_target=165, targets_source="computed",
                     reported_intake=None, **ALEXEY)
    r = apply_target_override(user.id, intake=200)
    assert r["intake"]["accepted"] is False and r["intake"]["min"] == 800
    assert _row(db, user.id)[:2] == (2750, 165) and _row(db, user.id)[5] is None
    r2 = apply_target_override(user.id, intake="idk")
    assert r2["intake"] == {"accepted": False, "reason": "not a number"}


def test_set_targets_tool_names_the_step_and_where_it_is_heading(db):
    from agent_tools import handle_set_targets, SET_TARGETS_TOOL
    assert "current_intake" in SET_TARGETS_TOOL["input_schema"]["properties"]
    assert "current_protein_intake" in SET_TARGETS_TOOL["input_schema"]["properties"]
    assert "EAT NOW" in SET_TARGETS_TOOL["description"]
    user = make_user(db, calorie_target=2750, protein_target=165, targets_source="computed",
                     reported_intake=None, **ALEXEY)
    out = handle_set_targets(user.id, {"current_intake": 2000, "current_protein_intake": 100,
                                       "reason": "can't afford that much food"})
    assert out.startswith("ok: noted they eat about 2000 cal / 100g a day; target stepped 2750/165g → 2400 cal / 132g to start"), out
    assert "full number for their stats is 2750 / 165g" in out and out.endswith("current: 2400 cal / 132g")
    again = handle_set_targets(user.id, {"current_intake": 2000})
    assert "no step needed" in again and "target stays 2400 cal / 132g" in again, again
    bad = handle_set_targets(user.id, {"current_intake": 20})
    assert bad.startswith("error: current_intake 20 isn't a daily number we can act on"), bad


def test_voice_routes_what_they_eat_to_current_intake():
    src = open("prompts/voice.md").read()
    assert "`current_intake`" in src and "3100 on someone eating 2000 → 2400" in src


def test_loop_context_tells_the_coach_the_target_is_the_feasible_first_step(db):
    from agent_loop import build_loop_context
    from models import get_session, User
    user = make_user(db, calorie_target=2400, protein_target=132, reported_intake=2000,
                     reported_protein_intake=100, **ALEXEY)
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "they said they eat about 2000 cal a day / 100g protein" in ctx
    assert "feasible first step" in ctx


# ─── 2. the setup ask + the extractor ────────────────────────────────────────

def test_setup_asks_what_they_eat_once_and_no_idea_is_a_full_answer(db):
    import onboarding_agent as oa
    u = make_user(db, onboarding_step=2, reported_intake=None, **ALEXEY)
    fields = [f for f, _ in oa._get_missing_fields(u)]
    assert "current_intake" in fields
    assert fields.index("current_intake") > fields.index("cooking_situation")   # it follows the food question
    assert "current_intake" in oa.PLAIN_ASKS and "no idea" in oa.PLAIN_ASKS["current_intake"]
    assert "current_intake" in [f for f, _ in oa.REQUIRED_FIELDS]
    u.reported_intake = 0
    assert "current_intake" not in [f for f, _ in oa._get_missing_fields(u)]
    u.reported_intake = 2000
    assert "current_intake" not in [f for f, _ in oa._get_missing_fields(u)]


def test_setup_ask_flag_off_removes_the_field(db, monkeypatch):
    import config
    import onboarding_agent as oa
    monkeypatch.setattr(config, "INTAKE_ASK_ENABLED", False)
    u = make_user(db, onboarding_step=2, reported_intake=None, **ALEXEY)
    assert "current_intake" not in [f for f, _ in oa._get_missing_fields(u)]


def test_extractor_fields_are_stored_and_no_idea_never_overwrites_a_number(db):
    import onboarding_agent as oa
    user = make_user(db, onboarding_step=2, reported_intake=None, **ALEXEY)
    oa._store_extracted_data(user.id, {"current_intake_unknown": True})
    assert _row(db, user.id)[5] == 0
    oa._store_extracted_data(user.id, {"current_intake_calories": 2000, "current_intake_protein": "100"})
    assert _row(db, user.id)[5:] == (2000, 100)
    oa._store_extracted_data(user.id, {"current_intake_unknown": True})      # a later shrug doesn't erase it
    assert _row(db, user.id)[5] == 2000
    oa._store_extracted_data(user.id, {"current_intake_calories": 50})       # implausible → ignored
    assert _row(db, user.id)[5] == 2000
    # an AIM is still a target, not an intake
    assert _row(db, user.id)[0] is None


def test_extractor_prompt_separates_what_they_eat_from_what_they_aim_for():
    import onboarding_agent as oa
    src = open(oa.__file__).read()
    assert '"current_intake_calories": integer or null' in src
    assert '"current_intake_unknown": true or null' in src
    assert 'I eat about 100g of protein a day and roughly 2000 calories' in src


def test_summary_says_where_the_number_came_from_and_where_it_is_heading(db):
    import onboarding_agent as oa
    user = make_user(db, onboarding_step=2, reported_intake=2000, reported_protein_intake=100,
                     occupation="physics student", activity_level="sedentary", workout_time="08:00",
                     current_split="none", cooking_situation="dining_hall", diet="omnivore", injuries="thumb",
                     existing_tools="none", name="Alexey", **ALEXEY)
    text = oa._build_confirmation_summary(user)
    assert "ur at about 2000 cal now, so 2400 cal and 132g protein a day to start" in text, text
    assert "more like 2750 and 165g eventually, we step up to it as the scale moves" in text
    assert "we adjust in a few weeks" not in text
    assert "ur 6'2 165" in text and "training 6 days a week in the mornings" in text


def test_summary_without_an_intake_is_unchanged(db):
    import onboarding_agent as oa
    user = make_user(db, onboarding_step=2, reported_intake=0, workout_time="08:00", name="Alexey", **ALEXEY)
    text = oa._build_confirmation_summary(user)
    assert "so 2750 cal and 165g protein a day to start, we adjust in a few weeks" in text, text


def test_onboarding_profile_line_shows_what_they_eat(db):
    import onboarding_agent as oa
    user = make_user(db, onboarding_step=2, reported_intake=2000, reported_protein_intake=100, **ALEXEY)
    assert "Eats now (their estimate): ~2000 cal / 100g protein a day" in oa._build_system_prompt(user)
    user.reported_intake, user.reported_protein_intake = 0, None
    assert "Eats now: no idea (asked — never ask again)" in oa._build_system_prompt(user)


def test_completion_writes_the_ramped_pair_as_the_computed_target(db, monkeypatch):
    """_complete_onboarding reads calculate_targets → the ramped number IS the computed target."""
    import onboarding_agent as oa
    from models import User
    monkeypatch.setattr(oa, "send_sms", lambda *a, **k: None)
    monkeypatch.setattr(oa, "_send_capability_rundown", lambda *a, **k: False)
    import config
    monkeypatch.setattr(config, "SETUP_SEQUENCE_ENABLED", False)
    monkeypatch.setattr(config, "PROGRAM_BUILDER_ENABLED", False)
    user = make_user(db, onboarding_step=2, reported_intake=2000, reported_protein_intake=100,
                     occupation="physics student", activity_level="sedentary", workout_time="08:00",
                     current_split="none", cooking_situation="dining_hall", diet="omnivore", injuries="none",
                     existing_tools="none", name="Alexey", **ALEXEY)
    try:
        oa._complete_onboarding(user, "ok")
    except Exception as e:  # noqa: BLE001 — the post-summary sequence may need live plumbing; the row is what we assert
        print("post-summary step raised:", e)
    db.expire_all()
    u = db.get(User, user.id)
    assert (u.calorie_target, u.protein_target, u.calorie_target_computed, u.protein_target_computed) == (2400, 132, 2400, 132)
    assert u.targets_source == "computed" and u.onboarding_step == 3


# ─── the adaptive cycle grows a ramped start ─────────────────────────────────

def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_adaptive_cycle_steps_a_ramped_bulk_up_when_the_scale_is_flat(db):
    """Ramped to 2400, eating 2400, scale flat → expenditure ≈ 2400 → new maintenance
    0.7·2400 + 0.3·2500 = 2470 → build target 2700 → +150 step → 2550. It climbs."""
    from adaptive_targets import evaluate
    from models import get_session, Meal, WeightLog, User
    user = make_user(db, calorie_target=2400, protein_target=132, targets_source="computed",
                     reported_intake=2000, reported_protein_intake=100, **ALEXEY)
    s = get_session()
    try:
        u = s.get(User, user.id)
        u.created_at = _now() - timedelta(days=30)
        for d in range(14):
            day = _now() - timedelta(days=d)
            for i in range(3):
                s.add(Meal(user_id=user.id, description=f"meal {i}", calories=800,
                           eaten_at=day.replace(hour=12 + i * 3, minute=0, second=0, microsecond=0)))
        for days_ago, lbs in [(13, 165.0), (10, 165.2), (7, 164.9), (3, 165.1), (0, 165.0)]:
            s.add(WeightLog(user_id=user.id, weighed_at=_now() - timedelta(days=days_ago, hours=2), weight_lbs=lbs))
        s.commit()
        r = evaluate(s.get(User, user.id), s)
    finally:
        s.close()
    assert r["change"] is True and r["new"] == 2550, r
    assert "up 150" in r["reason"]


def test_profile_payload_carries_the_reported_intake(db):
    from profile_page import build_profile_payload
    from models import get_session, User
    user = make_user(db, calorie_target=2400, protein_target=132, reported_intake=2000,
                     reported_protein_intake=100, **ALEXEY)
    s = get_session()
    try:
        p = build_profile_payload(s, s.get(User, user.id))
    finally:
        s.close()
    assert p["targets"]["reported_intake"] == 2000 and p["targets"]["reported_protein_intake"] == 100
