"""
Bounded user override of calorie/protein targets (founder, 2026-09-14): the user
can set either target within ±15% of the COMPUTED value; the row records it as
their pick (targets_source='user') with the computed pair beside it. Runs in code
during the onboarding adjust turn (the extractor reads the number they asked for)
and as the set_targets tool on the coach loop. Out-of-band asks are rejected with
the nearest allowed values; the model never invents a number.
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from tests.factories import make_user

FOUNDER = dict(height_ft=5, height_in=6, weight_lbs=139, age=20, gender="male",
               goal="fat_loss,muscle_building", workout_days="4-5", avg_steps=10000,
               occupation="student", activity_level="active", workout_time="14:00",
               current_split="ppl", cooking_situation="cooks", diet="omnivore", injuries="none",
               wake_time="11:30", sleep_time="02:00", existing_tools="strava,fitbit", name="Nau")


def _targets(db, user_id):
    from models import User
    db.expire_all()
    u = db.get(User, user_id)
    return (u.calorie_target, u.protein_target, u.calorie_target_computed, u.protein_target_computed, u.targets_source)


# ─── bounds + apply ─────────────────────────────────────────────────────────

def test_bounds_are_15_percent_rounded_to_10():
    from macro_calculator import override_bounds
    assert override_bounds(2450) == (2080, 2820)
    assert override_bounds(139) == (120, 160)


def test_apply_accepts_in_band_and_stamps_source(db):
    from macro_calculator import apply_target_override
    user = make_user(db, onboarding_step=3, **FOUNDER)
    r = apply_target_override(user.id, calories=2200, protein=150, note="cant eat that much")
    assert r["accepted"] == {"calories": 2200, "protein": 150} and r["rejected"] == {}
    assert r["computed"] == {"calories": 2450, "protein": 139}
    assert _targets(db, user.id) == (2200, 150, 2450, 139, "user")


def test_apply_rejects_out_of_band_with_nearest_and_writes_nothing(db):
    from macro_calculator import apply_target_override
    user = make_user(db, onboarding_step=3, calorie_target=2450, protein_target=139, **FOUNDER)
    r = apply_target_override(user.id, calories=1800)
    assert r["accepted"] == {}
    assert r["rejected"]["calories"] == {"asked": 1800, "min": 2080, "max": 2820, "computed": 2450}
    assert _targets(db, user.id)[:2] == (2450, 139)
    assert _targets(db, user.id)[4] is None  # untouched — no pick was made


def test_apply_partial_one_accepted_one_rejected(db):
    from macro_calculator import apply_target_override
    user = make_user(db, onboarding_step=3, **FOUNDER)
    r = apply_target_override(user.id, calories=2300, protein=250)
    assert r["accepted"] == {"calories": 2300} and "protein" in r["rejected"]
    assert _targets(db, user.id) == (2300, None, 2450, 139, "user")


def test_apply_garbage_is_rejected_not_raised(db):
    from macro_calculator import apply_target_override
    user = make_user(db, onboarding_step=3, **FOUNDER)
    r = apply_target_override(user.id, calories="two thousand")
    assert r["rejected"]["calories"]["reason"] == "not a number" and r["accepted"] == {}


# ─── coach-loop tool ────────────────────────────────────────────────────────

def test_set_targets_tool_result_strings(db):
    from agent_tools import handle_set_targets, dispatch_tool
    user = make_user(db, onboarding_step=3, calorie_target=2450, protein_target=139, **FOUNDER)
    out = handle_set_targets(user.id, {"calories": 2200, "protein_g": 150, "reason": "too much food"})
    assert out.startswith("ok: set calories 2200, protein 150 (their pick; computed was 2450 cal / 139g)"), out
    assert out.endswith("current: 2200 cal / 150g")
    out2 = dispatch_tool("set_targets", {"calories": 1500}, user.id)
    assert out2.startswith("error: calories 1500 is outside the 15% band — nearest allowed 2080–2820"), out2
    assert "current: 2200 cal / 150g" in out2
    assert handle_set_targets(user.id, {}) == "error: give calories and/or protein_g"


def test_set_targets_tool_is_offered_to_the_loop(db, monkeypatch):
    import config
    monkeypatch.setattr(config, "SET_TARGETS_TOOL_ENABLED", True)
    import agent_loop
    src = open(agent_loop.__file__).read()
    assert "SET_TARGETS_TOOL" in src
    from agent_tools import SET_TARGETS_TOOL
    assert SET_TARGETS_TOOL["name"] == "set_targets"
    assert "15%" in SET_TARGETS_TOOL["description"]


# ─── onboarding adjust turn ─────────────────────────────────────────────────

def _is_field_extract(kwargs):
    return "Extract any fitness coaching profile data" in str(kwargs["messages"][0]["content"])


def _is_target_extract(kwargs):
    return "reacting to proposed daily targets" in str(kwargs["messages"][0]["content"])


def _step2_user(db):
    from models import get_session, Message
    user = make_user(db, onboarding_step=2, **FOUNDER)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body="here's what i'm working with ... sound right?",
                      message_type="onboarding"))
        s.commit()
    finally:
        s.close()
    return user


def test_pushback_with_numbers_applies_the_override_in_code(db, anthropic_stub, sms_capture):
    import onboarding_agent
    user = _step2_user(db)
    seen = {}

    def _handler(kw):
        if _is_field_extract(kw):
            return "{}"
        if _is_target_extract(kw):
            return '{"calories": 2200, "protein": 150}'
        seen["instruction"] = kw["messages"][0]["content"]
        return "alr 2200 and 150 it is, your pick — i'd have gone 2450. lock it in?"
    anthropic_stub.reply_with(_handler)

    done = onboarding_agent.handle_onboarding_reply(
        user, "Hmm, 2450 lwk sounds too high, how about 2200 and we up the protein to like 150g?")
    assert done is False
    assert _targets(db, user.id) == (2200, 150, 2450, 139, "user")
    ins = seen["instruction"]
    assert "TARGET REQUEST HANDLED IN CODE" in ins
    assert "calories: they asked for 2200 → ACCEPTED" in ins and "protein: they asked for 150 → ACCEPTED" in ins
    assert "You picked 2200 cal and 150g protein" in ins      # the summary now shows their pick
    assert "Do NOT repeat the whole summary" in ins
    assert sms_capture[-1][1].startswith("alr 2200 and 150")


def test_pushback_out_of_band_tells_the_model_the_nearest_allowed(db, anthropic_stub, sms_capture):
    import onboarding_agent
    user = _step2_user(db)
    seen = {}

    def _handler(kw):
        if _is_field_extract(kw):
            return "{}"
        if _is_target_extract(kw):
            return '{"calories": 1800, "protein": null}'
        seen["instruction"] = kw["messages"][0]["content"]
        return "1800's too low for you — lowest i can do is 2080. want that?"
    anthropic_stub.reply_with(_handler)

    onboarding_agent.handle_onboarding_reply(user, "can we do like 1800")
    assert _targets(db, user.id)[4] is None and _targets(db, user.id)[0] is None
    ins = seen["instruction"]
    assert "calories: they asked for 1800 → NOT allowed (band is 2080–2820" in ins
    assert "do not state any other number" in ins


def test_pushback_without_a_number_skips_the_extractor_and_invites_one(db, anthropic_stub, sms_capture):
    import onboarding_agent
    user = _step2_user(db)
    calls = {"target_extract": 0}
    seen = {}

    def _handler(kw):
        if _is_field_extract(kw):
            return "{}"
        if _is_target_extract(kw):
            calls["target_extract"] += 1
            return "{}"
        seen["instruction"] = kw["messages"][0]["content"]
        return "fair — what feels doable?"
    anthropic_stub.reply_with(_handler)

    onboarding_agent.handle_onboarding_reply(user, "Idk, I just don't think I could eat that much food ngl")
    assert calls["target_extract"] == 0, "no digit in the message → no extractor call"
    assert "TARGET REQUEST HANDLED" not in seen["instruction"]
    assert "invite one" in seen["instruction"]
    assert _targets(db, user.id)[4] is None


def test_completion_keeps_their_pick_and_records_the_computed_pair(db, anthropic_stub, sms_capture):
    import onboarding_agent
    from macro_calculator import apply_target_override
    user = _step2_user(db)
    apply_target_override(user.id, calories=2200, protein=150)
    anthropic_stub.reply_with(lambda kw: "{}" if _is_field_extract(kw) else "locked in. link: x")

    assert onboarding_agent.handle_onboarding_reply(user, "okay sure ig, sounds good for now") is True
    assert _targets(db, user.id) == (2200, 150, 2450, 139, "user")


def test_completion_without_a_pick_stores_computed_and_says_so(db, anthropic_stub, sms_capture):
    import onboarding_agent
    user = _step2_user(db)
    anthropic_stub.reply_with(lambda kw: "{}" if _is_field_extract(kw) else "locked in. link: x")
    assert onboarding_agent.handle_onboarding_reply(user, "sounds good") is True
    assert _targets(db, user.id) == (2450, 139, 2450, 139, "computed")


def test_profile_page_exposes_source_and_computed(db):
    from profile_page import build_profile_payload
    from macro_calculator import apply_target_override
    from models import get_session, User
    user = make_user(db, onboarding_step=3, **FOUNDER)
    apply_target_override(user.id, calories=2200)
    s = get_session()
    try:
        payload = build_profile_payload(s, s.get(User, user.id))
    finally:
        s.close()
    assert payload["targets"]["calories"] == 2200 and payload["targets"]["source"] == "user"
    assert payload["targets"]["computed_calories"] == 2450
