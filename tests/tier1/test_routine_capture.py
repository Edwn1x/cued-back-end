"""
Routine capture (PR C) — a pasted program becomes the user's own card templates.

Live 2026-09-22 (user 42): a six-day PPL with every exercise/set/rep was pasted during
onboarding and survived only as current_split="ppl"; the card would have shown the
generic push day. Also: the summary never showed experience, so "beginner" from the
signup form (a six-day PPL later) had no place to be corrected.
"""

from __future__ import annotations

import json

from tests.factories import make_user

ALEX_PASTE = """Mon-Wed (High Volume)
Thur-Sat (Low Volume)
Sunday (rest day)
Start/End:
Run 1-2 miles

Mon/Push(chest and shoulders)
Dumbbell incline press 3x10 + (warmup)
Shoulder press 3x10
Chest cable fly -mid 3x10
Cable lateral raises 3x10 + (warmup)
Tricep pulldowns bilateral 3x10 + (warm up)

RKC Plank (3 min song) 2x song
1min dead hang

Tue/pull (back and bi/tri, forearms)
Pull up 4x5 (goal x10)
Lat pulldown wide grip 3x10 + (warmup or mayo)
Hammer curls 3x10

Wed/Legs
— NO RUN —
Swim
Squats smith 3x10
Leg press 3x10
Calf raises 3x12"""

PARSED = {"days": {
    "push": [
        {"name": "Dumbbell incline press", "sets": 3, "reps": 10, "bodyweight": False, "timed": False},
        {"name": "Shoulder press", "sets": 3, "reps": 10, "bodyweight": False, "timed": False},
        {"name": "Chest cable fly mid", "sets": 3, "reps": 10, "bodyweight": False, "timed": False},
        {"name": "Cable lateral raises", "sets": 3, "reps": 10, "bodyweight": False, "timed": False},
        {"name": "Tricep pulldowns bilateral", "sets": 3, "reps": 10, "bodyweight": False, "timed": False},
        {"name": "RKC plank", "sets": 2, "reps": 180, "bodyweight": True, "timed": True},
        {"name": "Dead hang", "sets": 1, "reps": 60, "bodyweight": True, "timed": True},
    ],
    "pull": [
        {"name": "Pull up", "sets": 4, "reps": 5, "bodyweight": True, "timed": False},
        {"name": "Lat pulldown wide grip", "sets": 3, "reps": 10, "bodyweight": False, "timed": False},
        {"name": "Hammer curls", "sets": 3, "reps": 10, "bodyweight": False, "timed": False},
    ],
    "legs": [
        {"name": "Squats smith", "sets": 3, "reps": 10, "bodyweight": False, "timed": False},
        {"name": "Leg press", "sets": 3, "reps": 10, "bodyweight": False, "timed": False},
        {"name": "Calf raises", "sets": 3, "reps": 12, "bodyweight": False, "timed": False},
        {"name": "", "sets": 3, "reps": 10},                      # malformed → dropped
        {"name": "Mystery", "sets": "lots", "reps": 10},          # malformed → dropped
    ],
    "cardio": [{"name": "run", "sets": 1, "reps": 1}],           # unknown key → dropped
}}


def _is_parse(kw):
    return "Parse this workout routine" in str(kw["messages"][0]["content"])


def _is_field_extract(kw):
    return "Extract any fitness coaching profile data" in str(kw["messages"][0]["content"])


def test_looks_like_routine_is_shape_not_keywords():
    from workouts.routine import looks_like_routine
    assert looks_like_routine(ALEX_PASTE)
    assert looks_like_routine("push\nbench 3x5\nincline 3x10\npull\nrows 4x8\ncurls 3x12")
    assert not looks_like_routine("hit 185 on bench today for 3x5")          # one line
    assert not looks_like_routine("did 3 sets of pushups\nthen ran 2 miles")  # too few
    assert not looks_like_routine("I do push pull legs, been at it a year")


def test_parse_routine_validates_slugs_weights_and_drops_junk(anthropic_stub):
    from workouts.routine import parse_routine
    anthropic_stub.reply_with(lambda kw: json.dumps(PARSED))
    out = parse_routine(ALEX_PASTE, user_id=1)
    assert set(out) == {"push", "pull", "legs"}
    push = {r["slug"]: r for r in out["push"]}
    # a known movement keeps the global slug + default; unknown ones get a starting load
    assert push["incline_db_press"]["default_weight"] == 40 and push["incline_db_press"]["plate_step"] == 5
    assert push["shoulder_press"]["default_weight"] == 30
    assert push["cable_lateral_raises"]["default_weight"] == 10
    # timed / bodyweight rows: 0 load, rep progression, "(sec)" label
    assert push["rkc_plank"] == {"slug": "rkc_plank", "label": "rkc plank (sec)", "sets": 2, "reps": 180,
                                 "default_weight": 0, "plate_step": 0, "rep_step": 10}
    assert push["dead_hang"]["rep_step"] == 10
    pull = {r["slug"]: r for r in out["pull"]}
    assert pull["pull_up"]["default_weight"] == 0 and pull["pull_up"]["rep_step"] == 1
    assert pull["lat_pulldown"]["default_weight"] == 100          # global template
    assert pull["hammer_curls"]["default_weight"] == 25            # not barbell_curl's 45
    legs = [r["slug"] for r in out["legs"]]
    assert legs == ["squats_smith", "leg_press", "calf_raise"]      # two malformed rows dropped
    assert {r["slug"]: r["default_weight"] for r in out["legs"]}["squats_smith"] == 95   # NOT the barbell squat's 155
    assert {r["slug"]: r["default_weight"] for r in out["legs"]}["leg_press"] == 180     # global template
    assert push["tricep_pushdown"]["default_weight"] == 40                                # "tricep pulldowns bilateral" → alias


def test_parse_routine_survives_bad_model_output(anthropic_stub):
    from workouts.routine import parse_routine
    anthropic_stub.reply_with(lambda kw: "not json at all")
    assert parse_routine(ALEX_PASTE) == {}
    anthropic_stub.reply_with(lambda kw: json.dumps({"days": {"tuesday": [{"name": "x", "sets": 3, "reps": 8}]}}))
    assert parse_routine(ALEX_PASTE) == {}


def test_save_routine_merges_by_day_and_sets_the_split(db, anthropic_stub):
    from models import User
    from workouts.routine import save_routine
    from workouts.templates import templates_for, TEMPLATES
    u = make_user(db, current_split=None, custom_templates={"upper": [{"slug": "x", "label": "x", "sets": 3, "reps": 8, "default_weight": 50}]})
    anthropic_stub.reply_with(lambda kw: json.dumps(PARSED))
    r = save_routine(u.id, ALEX_PASTE, source="model")
    assert r == {"days": {"upper": 1, "push": 7, "pull": 3, "legs": 3}, "split": "ppl"}  # upper kept
    db.expire_all(); u = db.get(User, u.id)
    assert set(u.custom_templates) == {"upper", "push", "pull", "legs"}   # upper kept, days merged
    assert u.current_split == "ppl"
    t = templates_for(u)
    assert [e.slug for e in t["push"]][:2] == ["incline_db_press", "shoulder_press"]
    assert t["lower"] is TEMPLATES["lower"]                                # fallback for days not given
    # an existing split is never overwritten
    v = make_user(db, current_split="upper_lower")
    save_routine(v.id, ALEX_PASTE, source="model")
    db.expire_all()
    assert db.get(User, v.id).current_split == "upper_lower"


def test_onboarding_paste_lands_on_the_cards_and_in_the_prompt(db, anthropic_stub, sms_capture):
    import onboarding_agent
    from models import User
    from tests.tier1.test_berkeley_friend_onboarding import _new_signup
    user = _new_signup(db, onboarding_step=2)
    seen = {}

    def handler(kw):
        if _is_parse(kw):
            return json.dumps(PARSED)
        if _is_field_extract(kw):
            return json.dumps({"current_split": "ppl"})
        seen["system"] = kw["system"]
        return "ok this is way more dialed than u made it sound"
    anthropic_stub.reply_with(handler)
    onboarding_agent.handle_onboarding_reply(user, ALEX_PASTE)
    db.expire_all(); u = db.get(User, user.id)
    assert set(u.custom_templates) == {"push", "pull", "legs"} and u.current_split == "ppl"
    sys_text = "".join(b["text"] for b in seen["system"]) if isinstance(seen["system"], list) else seen["system"]
    assert "Their own routine (already on their workout cards" in sys_text
    assert "push: 7 exercises (dumbbell incline press, shoulder press, chest cable fly mid, …)" in sys_text

    # a non-routine message never triggers the parser
    calls = []
    anthropic_stub.reply_with(lambda kw: (calls.append(1) or "{}") if _is_parse(kw) else ("{}" if _is_field_extract(kw) else "ok"))
    onboarding_agent.handle_onboarding_reply(user, "I try to wake up early and train but I usually have to make food")
    assert calls == []


def test_summary_shows_experience_and_the_routine(db):
    import onboarding_agent
    base = dict(onboarding_step=2, height_ft=5, height_in=8, weight_lbs=160, age=19, gender="male",
                goal="fat_loss", workout_days="mon,tue,wed,thu,fri,sat", workout_time="14:00",
                wake_time="13:00", sleep_time="04:00", activity_level="moderately active", avg_steps=6500)
    u = make_user(db, **base, experience="beginner", custom_templates={"push": [{"slug": "a", "label": "a", "sets": 3, "reps": 10, "default_weight": 40}]})
    s = onboarding_agent._build_confirmation_summary(u)
    assert "Goal is cutting, under 6 months of training." in s
    assert "Your own routine is on your workout cards." in s
    v = make_user(db, **base, experience=None)
    s2 = onboarding_agent._build_confirmation_summary(v)
    assert "Goal is cutting." in s2 and "routine" not in s2


def test_store_accepts_stated_experience_and_goal_only_from_valid_values(db):
    import onboarding_agent
    from models import User
    from tests.tier1.test_berkeley_friend_onboarding import _new_signup
    user = _new_signup(db, onboarding_step=2, experience="beginner", goal="muscle_building")
    onboarding_agent._store_extracted_data(user.id, {"experience": "advanced", "goal": "fat_loss"})
    db.expire_all(); u = db.get(User, user.id)
    assert (u.experience, u.goal) == ("advanced", "fat_loss")
    onboarding_agent._store_extracted_data(user.id, {"experience": "pro", "goal": "get jacked"})
    db.expire_all(); u = db.get(User, user.id)
    assert (u.experience, u.goal) == ("advanced", "fat_loss")   # junk never lands


def test_extractor_prompt_teaches_experience_and_goal_rules(db, anthropic_stub):
    import onboarding_agent
    from tests.tier1.test_berkeley_friend_onboarding import _new_signup
    user = _new_signup(db, onboarding_step=2)
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.update(prompt=kw["messages"][0]["content"]) or "{}")
    onboarding_agent._extract_data_from_message("been lifting like 3 years", user)
    p = seen["prompt"]
    assert '"experience"' in p and "a detailed routine is NOT a statement" in p
    assert '"goal"' in p and '"experience": "advanced", "goal": "fat_loss"' in p


def test_save_routine_tool_and_loop_offering(db, driver, monkeypatch, anthropic_stub):
    import config
    from agent_tools import handle_save_routine
    from models import User
    from tests._fake_anthropic import ToolUse
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    assert handle_save_routine(1, {"routine_text": "short"}).startswith("error")
    calls = []

    def handler(kw):
        if _is_parse(kw):
            return json.dumps(PARSED)
        if not kw.get("tools"):
            return "freeform"
        assert "save_routine" in [t["name"] for t in kw["tools"]]
        calls.append(1)
        if len(calls) == 1:
            return ToolUse("save_routine", {"routine_text": ALEX_PASTE})
        tool_result = str(kw["messages"][-1]["content"])
        assert "routine saved to their cards" in tool_result and "placeholders" in tool_result
        return "got it, ur days are on the cards now. weights are placeholders till u log real ones"
    anthropic_stub.reply_with(handler)
    u = make_user(db, current_split=None)
    replies = driver.send(u, "here's my routine\n" + ALEX_PASTE)
    db.expire_all(); u = db.get(User, u.id)
    assert set(u.custom_templates) == {"push", "pull", "legs"} and u.current_split == "ppl"
    assert any("placeholders" in r for r in replies)


def test_voice_routes_pasted_routines_to_the_tool():
    from agent_loop import _voice_prompt
    assert "save_routine" in _voice_prompt()
