"""
Model in the loop at interpretive moments, code as the bounds (interpret.py): the first-card
answer is READ (a hedged max from a beginner is a max), and the formula's day-one targets are
reviewed inside a band before they reach the user (4150 / 3400 / 3550 / 3100 were all hand-fixed).
"""

from __future__ import annotations

import json

import pytest

import config
from tests.factories import make_user

JAYDEN = dict(name="Jayden", onboarding_step=2, age=17, gender="male", height_ft=5, height_in=4, weight_lbs=200,
              goal="fat_loss", experience="none", equipment="machines", workout_days="3", workout_time="22:30",
              activity_level="soccer team manager, on his feet 4-8:30", avg_steps=None, occupation="high school",
              cooking_situation="family cooks", reported_intake=None, biggest_obstacle="consistency",
              wake_time="06:30", sleep_time="00:00", injuries="right wrist surgery", diet="omnivore",
              existing_tools="none", preferred_channel="imessage")


def _is_review(kw) -> bool:
    return "WHAT THE FORMULA PRODUCED" in str(kw["messages"][0]["content"])


def _is_lift(kw) -> bool:
    return "THE COACH ASKED" in str(kw["messages"][0]["content"])


@pytest.fixture(autouse=True)
def _review_on(monkeypatch):
    monkeypatch.setattr(config, "TARGETS_MODEL_REVIEW_ENABLED", True)
    monkeypatch.setattr(config, "TARGETS_REVIEW_SAMPLES", 1)


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


# ─── targets ───────────────────────────────────────────────────────────────────────

def test_review_lowers_an_overfed_cut_inside_the_band(db, anthropic_stub, monkeypatch):
    from interpret import review_targets
    from macro_calculator import calculate_targets
    u = make_user(db, **JAYDEN)
    f = calculate_targets(u)
    assert f["calories"] >= 2500, f                       # the formula runs high for this profile
    anthropic_stub.reply_with(lambda kw: json.dumps({"calories": 2500, "protein": 150,
                                                      "reason": "17 and obese on a cut; the multiplier is too generous",
                                                      "confidence": 0.8}) if _is_review(kw) else "ok")
    r = review_targets(u, f)
    assert r and r["calories"] == 2500 and r["protein"] == 150 and "obese" in r["reason"]


def test_review_is_clamped_to_the_band_and_the_floors(db, anthropic_stub, monkeypatch):
    from interpret import review_targets
    from macro_calculator import calculate_targets
    u = make_user(db, **JAYDEN)
    f = calculate_targets(u)
    anthropic_stub.reply_with(lambda kw: json.dumps({"calories": 900, "protein": 40}) if _is_review(kw) else "ok")
    r = review_targets(u, f)
    assert r["calories"] == int(round(f["calories"] * 0.6 / 50) * 50) and r["calories"] >= 1500
    assert r["protein"] == max(80, int(round(f["protein"] * 0.8 / 5) * 5))
    anthropic_stub.reply_with(lambda kw: json.dumps({"calories": 9000, "protein": 400}) if _is_review(kw) else "ok")
    r2 = review_targets(u, f)
    assert r2["calories"] == int(round(f["calories"] * 1.1 / 50) * 50)
    # a woman's floor is 1200 (code-side), the calculator's own floor still applies
    w = make_user(db, **dict(JAYDEN, gender="female", weight_lbs=115, height_in=3, age=19, phone="+15550006001"))
    fw = calculate_targets(w)
    anthropic_stub.reply_with(lambda kw: json.dumps({"calories": 500, "protein": 10}) if _is_review(kw) else "ok")
    rw = review_targets(w, fw)
    assert rw["calories"] >= 1400 and rw["protein"] >= 80


def test_review_keeps_the_formula_on_small_moves_garbage_or_flag_off(db, anthropic_stub, monkeypatch):
    from interpret import review_targets, with_review
    from macro_calculator import calculate_targets
    u = make_user(db, **JAYDEN)
    f = calculate_targets(u)
    anthropic_stub.reply_with(lambda kw: json.dumps({"calories": f["calories"] - 40, "protein": f["protein"]}) if _is_review(kw) else "ok")
    assert review_targets(u, f) is None                    # under the 5% / 10 g threshold
    anthropic_stub.reply_with(lambda kw: "locked in")
    assert review_targets(u, f) is None and with_review(u, f) == f
    monkeypatch.setattr(config, "TARGETS_MODEL_REVIEW_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: json.dumps({"calories": 2000, "protein": 100}))
    assert review_targets(u, f) is None


def test_completion_writes_the_reviewed_pair_and_the_summary_says_it(db, anthropic_stub, monkeypatch, sms_capture):
    import onboarding_agent as oa
    from macro_calculator import calculate_targets
    monkeypatch.setattr(config, "PROGRAM_BRIEF_ENABLED", False)
    monkeypatch.setattr(config, "CARD_SETUP_ENABLED", False)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", False)
    u = make_user(db, **JAYDEN)
    formula = calculate_targets(u)["calories"]
    anthropic_stub.reply_with(lambda kw: json.dumps({"calories": 2500, "protein": 150, "reason": "cutting at BMI 34"})
                              if _is_review(kw) else "locked in")
    oa._complete_onboarding(_u(u.id), "yes")
    row = _u(u.id)
    assert row.calorie_target == 2500 and row.protein_target == 150 and row.targets_source == "computed"
    assert row.calorie_target_computed == 2500
    bodies = [b for _, b in sms_capture]
    assert any("2500 cal" in b and "150g protein" in b for b in bodies), bodies
    assert not any(str(formula) in b for b in bodies)


def test_recompute_carries_the_review_reason(db, anthropic_stub, monkeypatch):
    from macro_calculator import recompute_targets, calculate_targets
    from models import TargetAdjustment
    u = make_user(db, **dict(JAYDEN, onboarding_step=3, calorie_target=4100, protein_target=146, targets_source="computed"))
    anthropic_stub.reply_with(lambda kw: json.dumps({"calories": 2500, "protein": 150, "reason": "obese teen on a cut"})
                              if _is_review(kw) else "ok")
    r = recompute_targets(u.id)
    assert r["new"] == (2500, 150) and r["changed"]
    adj = db.query(TargetAdjustment).filter(TargetAdjustment.user_id == u.id).order_by(TargetAdjustment.id.desc()).first()
    assert adj and "coach review from" in adj.reason and "obese teen" in adj.reason


# ─── the first-card answer ──────────────────────────────────────────────────────

def test_lift_answer_is_read_and_bounded(db, anthropic_stub, monkeypatch):
    from interpret import interpret_lift_answer
    u = make_user(db, **dict(JAYDEN, experience="none"))
    anthropic_stub.reply_with(lambda kw: json.dumps({"lifts": [
        {"exercise": "squat", "weight": 135, "reps": None, "is_max": True},
        {"exercise": "bench", "weight": 135, "reps": 10, "is_max": False},
        {"exercise": "zercher yoke carry", "weight": 300, "reps": 5},          # not an anchor → dropped
        {"exercise": "deadlift", "weight": 5, "reps": 5},                        # under 20 lb → dropped
        {"exercise": "ohp", "weight": 95, "reps": 99},                           # bad reps → reps dropped
    ], "no_number": False, "confidence": 0.7}) if _is_lift(kw) else "ok")
    r = interpret_lift_answer(u, "heaviest u've benched or squatted?", "like no clue i think heaviest squat was 135, bench 135 for 10")
    assert r["items"] == [{"exercise": "squat", "weight": 135.0, "reps": 1},
                          {"exercise": "bench", "weight": 135.0, "reps": 10},
                          {"exercise": "ohp", "weight": 95.0}]
    assert r["no_number"] is False and r["confidence"] == 0.7


def test_lift_answer_skips_the_call_when_there_is_nothing_to_read(db, anthropic_stub):
    from interpret import interpret_lift_answer
    u = make_user(db, **JAYDEN)
    anthropic_stub.reply_with(lambda kw: "SHOULD NOT BE CALLED")
    before = len(anthropic_stub.calls)
    assert interpret_lift_answer(u, "ask", "lol idk what should i eat before") is None
    assert len(anthropic_stub.calls) == before


def test_pending_card_reply_uses_the_reading_and_falls_back_to_the_regex(db, anthropic_stub, monkeypatch):
    from agent_tools import handle_start_workout_session
    from workouts.calibrate import handle_pending_card_reply, anchors_of, peek_pending_card
    from models import User
    import workouts.start as start_mod
    monkeypatch.setattr(config, "START_WORKOUT_TOOL_ENABLED", True)
    monkeypatch.setattr(start_mod, "_resolve_channel", lambda uid: "sms")
    sent = []
    monkeypatch.setattr(start_mod, "send_sms", lambda phone, body, **kw: sent.append(body) or {"sid": f"m{len(sent)}"})
    # the model reads Sarah's answer as a max
    u = make_user(db, **dict(JAYDEN, onboarding_step=3, current_split="ppl", experience="none", gender="female",
                              weight_lbs=115, height_in=3, age=19, lift_anchors=None))
    assert handle_start_workout_session(u.id, {"template_key": "legs"}).startswith("error: first card")
    anthropic_stub.reply_with(lambda kw: json.dumps({"lifts": [{"exercise": "squat", "weight": 135, "reps": None, "is_max": True}],
                                                      "no_number": False, "confidence": 0.6}) if _is_lift(kw) else "ok")
    assert handle_pending_card_reply(u.id, "like no clue i think heaviest squat was 135") is True
    db.expire_all()
    assert anchors_of(db.get(User, u.id))["squat"] == {**anchors_of(db.get(User, u.id))["squat"], "weight": 135.0, "reps": 1}
    assert peek_pending_card(u.id) is None
    # the model says no number → the light card
    v = make_user(db, **dict(JAYDEN, onboarding_step=3, current_split="ppl", lift_anchors=None, phone="+15550006002"))
    assert handle_start_workout_session(v.id, {"template_key": "push"}).startswith("error: first card")
    anthropic_stub.reply_with(lambda kw: json.dumps({"lifts": [], "no_number": True, "confidence": 0.9}) if _is_lift(kw) else "ok")
    sent.clear()
    assert handle_pending_card_reply(v.id, "honestly the bar scares me, start me light") is True
    assert sent and "my best guess" in sent[0]
    # garbage from the model → the regex path as before
    w = make_user(db, **dict(JAYDEN, onboarding_step=3, current_split="ppl", lift_anchors=None, phone="+15550006003"))
    assert handle_start_workout_session(w.id, {"template_key": "push"}).startswith("error: first card")
    anthropic_stub.reply_with(lambda kw: "sure thing!")
    assert handle_pending_card_reply(w.id, "bench 65 for 5") is True
    db.expire_all()
    assert anchors_of(db.get(User, w.id))["bench_press"]["weight"] == 65
