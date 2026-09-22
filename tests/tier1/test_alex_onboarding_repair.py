"""
Alex (user 42, 2026-09-22) onboarding burn-in — PR A.

Live findings, each pinned here:
  1. `diet` stayed unknown after "I don't eat mushrooms tofu and raw fish" (no diet
     label fits) → onboarding parked while the coach said "i got everything".
  2. "staying under 2000 cals … 155 grams of protein" was dropped by both extractors.
  3. A pasted six-day PPL matched the lift keywords at 3:46am → workout_confirmed +
     at_gym during onboarding; and the routine itself survived only as "follows PPL".
  4. Three consecutive big asks with the same stock opener.
  5. wake_time 13:00 was ignored by the quiet window (capped at 11am).
  6. 16.5k-token onboarding system prompt with cache_read=0 on every turn.
"""

from __future__ import annotations

import json

import pytest

from tests.factories import make_user
from tests.tier1.test_berkeley_friend_onboarding import INTAKE, _is_extract, _new_signup, _seed_conversation


ALEX = dict(name="Alex", age=19, gender="male", goal="fat_loss", experience="beginner",
            equipment="full_gym", height_ft=5, height_in=8, weight_lbs=160,
            occupation="student", activity_level="moderately active", avg_steps=6500,
            workout_days="mon,tue,wed,thu,fri,sat", workout_time="14:00", current_split="ppl",
            cooking_situation="cook_myself", injuries="none", wake_time="13:00",
            sleep_time="04:00", existing_tools="scale,recipe book", user_timezone="America/Los_Angeles")

ALEX_PUSH = [
    {"slug": "incline_db_press", "label": "dumbbell incline press", "sets": 3, "reps": 10, "default_weight": 40, "plate_step": 5},
    {"slug": "shoulder_press", "label": "shoulder press", "sets": 3, "reps": 10, "default_weight": 30, "plate_step": 5},
    {"slug": "rkc_plank", "label": "RKC plank (sec)", "sets": 2, "reps": 60, "default_weight": 0, "plate_step": 0, "rep_step": 10},
]


# ─── 1. diet: dislikes ARE an answer ─────────────────────────────────────────

def test_diet_is_known_once_restrictions_are_filled(db):
    from onboarding_agent import _get_missing_fields
    u = make_user(db, **ALEX, onboarding_step=2, diet=None, restrictions="won't eat mushrooms")
    assert "diet" not in [f for f, _ in _get_missing_fields(u)]
    u2 = make_user(db, **ALEX, phone="+15550000002", onboarding_step=2, diet=None, restrictions=None)
    assert "diet" in [f for f, _ in _get_missing_fields(u2)]


def test_store_writes_dislikes_to_restrictions_and_dedupes(db):
    import onboarding_agent
    from models import User
    user = _new_signup(db, onboarding_step=2)
    onboarding_agent._store_extracted_data(user.id, {"diet": "omnivore", "food_dislikes": "mushrooms, tofu, raw fish"})
    onboarding_agent._store_extracted_data(user.id, {"food_dislikes": "tofu"})  # again → no dup
    db.expire_all()
    u = db.get(User, user.id)
    assert u.diet == "omnivore"
    assert u.restrictions == "won't eat mushrooms; won't eat tofu; won't eat raw fish"


def test_extractor_prompt_teaches_dislikes_and_self_stated_targets(db, anthropic_stub):
    import onboarding_agent
    user = _new_signup(db, onboarding_step=2)
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.update(prompt=kw["messages"][0]["content"]) or "{}")
    onboarding_agent._extract_data_from_message("I don't eat mushrooms tofu and raw fish", user)
    p = seen["prompt"]
    assert '"food_dislikes"' in p and '"calorie_target"' in p and '"protein_target"' in p
    assert 'food_dislikes": "mushrooms, tofu, raw fish"' in p     # the live message is the example
    assert '"calorie_target": 2000, "protein_target": 155' in p
    assert "never a number the coach said" in p


# ─── 2. targets they track to ────────────────────────────────────────────────

def test_store_keeps_self_stated_targets_but_never_overwrites_computed(db):
    import onboarding_agent
    from models import User
    user = _new_signup(db, onboarding_step=2)
    onboarding_agent._store_extracted_data(user.id, {"calorie_target": 2000, "protein_target": "155"})
    db.expire_all()
    u = db.get(User, user.id)
    assert (u.calorie_target, u.protein_target, u.targets_source) == (2000, 155, "user")

    computed = make_user(db, phone="+15550000003", onboarding_step=2, calorie_target=2400,
                         protein_target=160, targets_source="computed")
    onboarding_agent._store_extracted_data(computed.id, {"calorie_target": 1500})
    db.expire_all()
    c = db.get(User, computed.id)
    assert (c.calorie_target, c.targets_source) == (2400, "computed")


def test_summary_bounds_self_stated_targets_in_band_stand_out_of_band_clamp(db):
    import onboarding_agent
    from macro_calculator import calculate_targets, override_bounds
    from models import User
    # In band (Alex: 2000 vs computed 2200 on a cut) → stands, summary says "you picked".
    u = make_user(db, **ALEX, onboarding_step=2, diet="omnivore", calorie_target=2000,
                  protein_target=155, targets_source="user")
    computed = calculate_targets(u)
    lo, hi = override_bounds(computed["calories"])
    assert lo <= 2000 <= hi, (lo, hi, computed)
    assert onboarding_agent._reconcile_user_targets(u.id) is None
    db.expire_all(); u = db.get(User, u.id)
    assert (u.calorie_target, u.protein_target, u.targets_source) == (2000, 155, "user")
    assert u.calorie_target_computed == computed["calories"]
    s = onboarding_agent._build_confirmation_summary(u)
    assert "You picked 2000 cal and 155g protein" in s and f"I'd have set {computed['calories']}" in s

    # Way out of band → nearest end of the band is written and the summary says so.
    v = make_user(db, **ALEX, phone="+15550000004", onboarding_step=2, diet="omnivore",
                  calorie_target=1000, protein_target=155, targets_source="user")
    note = onboarding_agent._reconcile_user_targets(v.id)
    db.expire_all(); v = db.get(User, v.id)
    assert v.calorie_target == lo and v.protein_target == 155 and v.targets_source == "user"
    assert note and "you said 1000 cal" in note and f"{lo} cal is as low as I'll go" in note
    s2 = onboarding_agent._build_confirmation_summary(v, clamp_note=note)
    assert "On targets: you said 1000 cal" in s2 and f"So {lo} cal and 155g protein" in s2 and s2.endswith("Sound right?")


def test_last_field_landing_reconciles_targets_before_the_summary(db, anthropic_stub, sms_capture):
    """The live shape: everything known but `diet`, self-stated targets on the row, the
    dislikes text lands → summary goes out with THEIR numbers, not the computed pair."""
    import onboarding_agent
    from models import User
    user = make_user(db, **ALEX, onboarding_step=2, diet=None, restrictions=None,
                     calorie_target=2000, protein_target=155, targets_source="user")
    seen = {}

    def _handler(kw):
        if _is_extract(kw):
            return json.dumps({"diet": "omnivore", "food_dislikes": "mushrooms, tofu, raw fish"})
        seen["instruction"] = kw["messages"][0]["content"]
        return "ok here's what i got ... sound right?"
    anthropic_stub.reply_with(_handler)
    onboarding_agent.handle_onboarding_reply(user, "I don't eat mushrooms tofu and raw fish")
    assert len(sms_capture) == 1
    assert "You picked 2000 cal and 155g protein" in seen["instruction"]
    db.expire_all(); u = db.get(User, user.id)
    assert u.restrictions == "won't eat mushrooms; won't eat tofu; won't eat raw fish"
    assert u.calorie_target_computed and u.targets_source == "user"


# ─── 3a. the classifier keeps its hands off onboarding users ─────────────────

ROUTINE = "Mon/Push\nDumbbell incline press 3x10\nShoulder press 3x10\nSquats smith 3x10\nLeg press 3x10"


def test_pasted_routine_writes_no_training_state_during_onboarding(db, driver, anthropic_stub):
    from app import classify_message
    from models import User, get_session_state, is_workout_confirmed_today
    assert classify_message(ROUTINE) == "workout_log"  # it IS lift text — that's the point
    user = make_user(db, **ALEX, onboarding_step=2, diet=None)
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ok this is dialed")
    driver.send(user, ROUTINE)
    db.expire_all()
    assert get_session_state(user.id) is None
    assert is_workout_confirmed_today(user.id) is False


def test_lift_text_still_writes_training_state_once_onboarded(db, driver, anthropic_stub, monkeypatch):
    import config
    from models import get_session_state
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", False)
    user = make_user(db, **ALEX, onboarding_step=3, diet="omnivore")
    anthropic_stub.reply_with(lambda kw: "nice")
    driver.send(user, "hit 185 on bench today")
    db.expire_all()
    st = get_session_state(user.id)
    assert st and st.get("status") == "at_gym"


# ─── 3b. the routine reaches the card ────────────────────────────────────────

def test_custom_templates_override_the_users_days_and_fall_back_for_the_rest(db):
    from workouts.templates import TEMPLATES, templates_for
    u = make_user(db, **ALEX, onboarding_step=3, diet="omnivore",
                  custom_templates={"push": ALEX_PUSH,
                                    "Pull": [{"slug": "pull up", "label": "pull up", "sets": 4, "reps": 5, "default_weight": 0, "plate_step": 0, "rep_step": 1},
                                             {"slug": "", "sets": 3, "reps": 10},          # malformed → skipped
                                             {"slug": "shrug", "sets": "three", "reps": 12}],  # malformed → skipped
                                    "wed_legs": [{"slug": "x", "sets": 1, "reps": 1}],     # unknown key → ignored
                                    "legs": [{"slug": "nope", "sets": 0, "reps": 10}]})     # all malformed → global day
    t = templates_for(u)
    assert [e.slug for e in t["push"]] == ["incline_db_press", "shoulder_press", "rkc_plank"]
    assert t["push"][2].default_weight == 0 and t["push"][2].rep_step == 10       # reps-only row
    assert [e.slug for e in t["pull"]] == ["pull_up"] and t["pull"][0].label == "pull up"
    assert t["legs"] is TEMPLATES["legs"] and t["upper"] is TEMPLATES["upper"]
    assert "wed_legs" not in t
    plain = make_user(db, **ALEX, phone="+15550000005", onboarding_step=3, diet="omnivore")
    assert templates_for(plain) is TEMPLATES


def test_build_session_plans_the_users_own_push_day(db):
    from models import get_session, SetLog
    from workouts.plan import build_session
    u = make_user(db, **ALEX, onboarding_step=3, diet="omnivore", custom_templates={"push": ALEX_PUSH})
    ws = build_session(u, "push")
    s = get_session()
    try:
        rows = s.query(SetLog).filter_by(session_id=ws.id).order_by(SetLog.id).all()
        by = {}
        for r in rows:
            by.setdefault((r.exercise, r.exercise_label), []).append((r.planned_weight, r.planned_reps))
    finally:
        s.close()
    assert list(by) == [("incline_db_press", "dumbbell incline press"), ("shoulder_press", "shoulder press"), ("rkc_plank", "RKC plank (sec)")]
    assert by[("incline_db_press", "dumbbell incline press")] == [(40.0, 10)] * 3
    assert by[("rkc_plank", "RKC plank (sec)")] == [(0.0, 60)] * 2


def test_custom_templates_column_is_migrated(db):
    from sqlalchemy import inspect
    from models import engine
    assert "custom_templates" in {c["name"] for c in inspect(engine).get_columns("users")}


# ─── 4. the big ask is a one-time move ───────────────────────────────────────

def test_intake_mode_big_ask_once_unless_they_ask_again():
    from onboarding_agent import BIG_ASK_AFTER_TURNS, _intake_mode
    many = [("a", "a — x"), ("b", "b — x"), ("c", "c — x"), ("d", "d — x")]
    assert _intake_mode("yeah", many, turns=BIG_ASK_AFTER_TURNS) == "big_ask"
    assert _intake_mode("yeah", many, turns=BIG_ASK_AFTER_TURNS, big_ask_sent=True) == "friend"
    assert _intake_mode("yeah", many, turns=BIG_ASK_AFTER_TURNS + 3, big_ask_sent=True) == "friend"
    assert _intake_mode("what else do you need", many, turns=2, big_ask_sent=True) == "big_ask"
    assert _intake_mode("yeah", many[:2], turns=BIG_ASK_AFTER_TURNS, big_ask_sent=True) == "bundle"


def test_big_ask_is_marked_counted_and_not_repeated(db, anthropic_stub, sms_capture):
    import onboarding_agent
    from models import Message
    user = _new_signup(db, onboarding_step=2)
    _seed_conversation(user.id, coach_replies=onboarding_agent.BIG_ASK_AFTER_TURNS, inbound_texts=6)
    seen = []

    def _handler(kw):
        if _is_extract(kw):
            return "{}"
        seen.append(kw["messages"][0]["content"])
        return "alr real talk, send me the basics in one go"
    anthropic_stub.reply_with(_handler)

    onboarding_agent.handle_onboarding_reply(user, "haha yeah")
    assert "drop the basics in ONE text" in seen[-1]
    assert "not a stock line" in seen[-1] and "don't re-ask it" in seen[-1]
    assert "alr real talk, just send me the basics" not in seen[-1]  # no hard-coded opener
    db.expire_all()
    types = [m.message_type for m in db.query(Message).filter_by(user_id=user.id, direction="out").order_by(Message.id)]
    assert types[-1] == onboarding_agent.BIG_ASK_MESSAGE_TYPE
    assert onboarding_agent._big_ask_sent(user.id) is True
    assert onboarding_agent._coach_turns(user.id) == onboarding_agent.BIG_ASK_AFTER_TURNS + 1  # it IS a turn

    onboarding_agent.handle_onboarding_reply(user, "ok")   # still 14 unknown, turns ≥ 6
    assert "drop the basics in ONE text" not in seen[-1]   # friend reply, not a second list
    assert "ONE question at most" in seen[-1]


def test_every_intake_builder_forbids_claiming_done(db, anthropic_stub, sms_capture):
    """Live: the bundle reply for the last field ended "i think i got everything i need
    on u now" while `diet` was still unknown."""
    import onboarding_agent
    seen = []
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else (seen.append(kw["messages"][0]["content"]) or "ok"))
    # bundle (two left, late)
    user = _new_signup(db, onboarding_step=2, height_ft=5, height_in=10, weight_lbs=170, occupation="student",
                       activity_level="active", avg_steps=8000, workout_days="4", workout_time="17:00",
                       current_split="none", cooking_situation="dining_hall", injuries="none",
                       wake_time="08:00", sleep_time="00:00")  # diet + existing_tools unknown
    _seed_conversation(user.id, coach_replies=onboarding_agent.BUNDLE_AFTER_TURNS, inbound_texts=4)
    onboarding_agent.handle_onboarding_reply(user, "cook my own food")
    # friend (early)
    user2 = _new_signup(db, phone="+15550000006", onboarding_step=2)
    onboarding_agent.handle_onboarding_reply(user2, "can't sleep")
    # big ask (they asked)
    onboarding_agent.handle_onboarding_reply(user2, "just tell me what you need")
    assert len(seen) == 3
    for ins in seen:
        assert "never say you're done" in ins and "code decides that, not you" in ins


# ─── 5. quiet window honors a 1pm waker ──────────────────────────────────────

def test_quiet_window_extends_to_an_afternoon_waker(monkeypatch):
    import config
    from tests.tier1.test_heartbeat_quiet_and_waitlist import _at_local
    from heartbeat import _in_standing_quiet_hours, _quiet_window
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", True)

    class U:
        user_timezone = "America/Los_Angeles"
        sleep_time = "04:00"
        wake_time = "13:00"
    assert _quiet_window(U()) == (21, 13)
    assert _in_standing_quiet_hours(U(), now=_at_local(9)) is True      # live: was texted-eligible here
    assert _in_standing_quiet_hours(U(), now=_at_local(12, 59)) is True
    assert _in_standing_quiet_hours(U(), now=_at_local(13, 5)) is False
    assert _in_standing_quiet_hours(U(), now=_at_local(20)) is False    # 7:30pm run reminder window is open

    class Late(U):
        wake_time = "15:00"   # beyond the cap → floor only, never a 3pm blackout
    assert _quiet_window(Late()) == (21, 8)


# ─── 6. onboarding prompt caching ────────────────────────────────────────────

def test_onboarding_system_prompt_is_split_for_caching(db, anthropic_stub, sms_capture):
    import onboarding_agent
    user = _new_signup(db, onboarding_step=2)
    sp = onboarding_agent._build_system_prompt(user)
    blocks = onboarding_agent._cacheable_system(sp)
    assert isinstance(blocks, list) and len(blocks) == 2
    assert blocks[0]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in blocks[1]
    assert blocks[0]["text"].lstrip().startswith(__import__("agent_loop").identity_prompt().strip()[:40])
    assert "## RIGHT NOW" in blocks[1]["text"] and user.name in blocks[1]["text"]
    assert "## RIGHT NOW" not in blocks[0]["text"]
    assert blocks[0]["text"] + blocks[1]["text"] == sp
    assert onboarding_agent._cacheable_system("short prompt, no marker") == "short prompt, no marker"

    # and the live call carries it
    anthropic_stub.reply_with(lambda kw: "{}" if _is_extract(kw) else "ok")
    onboarding_agent.handle_onboarding_reply(user, "hey")
    gen = [c for c in anthropic_stub.calls if not _is_extract(c)][-1]
    assert isinstance(gen["system"], list) and gen["system"][0].get("cache_control")
