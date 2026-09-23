"""
Bro splits get their own cards (2026-09-22, user 43 "Angel").

Live: "chest and biceps , back and triceps  and legs and shoulders" was captured as
current_split="bro_split" — a label the onboarding extractor may write — and 40 minutes
later "show me a workout card" produced a FULL BODY card, because the split cycle table
and the template file both stopped at push/pull/legs/upper/lower/full_body. Two gaps:
the label had no card behind it, and the grouping he stated was never stored.

Now: body-part days compose from part templates (chest_biceps = chest block + biceps
block), the user's own day order lives in users.split_days (extractor field + code
trigger in onboarding, save_routine(split_days=…) in the loop), the card and the pointer
walk that cycle, and a split with no mapping REFUSES instead of defaulting.
"""

from __future__ import annotations

import json

import pytest

from tests.factories import make_user, TEMPLATE_ANCHORS
from tests.tier1.test_workout_phases_3_4_5 import imessage_on, sidecar_ok, card_ok  # noqa: F401 — fixtures

ANGEL_MSG = "chest and biceps , back and triceps  and legs and shoulders"
ANGEL_DAYS = ["chest_biceps", "back_triceps", "legs_shoulders"]


# ─── parsing: code owns the mapping ─────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    (ANGEL_MSG, ANGEL_DAYS),                                           # the live message, "and" both inside and between days
    ("chest/bis, back/tris, legs/shoulders", ANGEL_DAYS),               # slash joins parts when commas separate days
    ("push/pull/legs", ["push", "pull", "legs"]),                       # slash separates days when nothing stronger does
    ("Chest + tris\nBack + bis\nLegs", ["chest_triceps", "back_biceps", "legs"]),
    ("chest, back, shoulders, arms, legs", ["chest", "back", "shoulders", "arms", "legs"]),
    ("chest and shoulders and triceps, back and biceps, legs", ["chest_shoulders_triceps", "back_biceps", "legs"]),
    ("push, pull, legs, then arms", ["push", "pull", "legs", "arms"]),
    ("chest and back and legs", None),                                  # odd "and" chain — ambiguous, not guessed
    ("i lift monday and tuesday", None),                                # not body parts
    ("chest", None),                                                    # one day is not a split
    ("", None),
])
def test_parse_day_list_is_precision_biased(text, expected):
    from workouts.templates import parse_day_list
    assert parse_day_list(text) == expected


@pytest.mark.parametrize("key,expected", [
    ("chest_biceps", "chest_biceps"), ("chest+bis", "chest_biceps"), ("Chest and Bis", "chest_biceps"),
    ("arms", "arms"), ("legs", "legs"), ("push", "push"), ("fullbody", "full_body"),
    ("back_tris", "back_triceps"), ("tuesday", None), ("chest_x", None), ("", None),
])
def test_normalize_template_key_accepts_body_part_days(key, expected):
    from workouts.templates import normalize_template_key
    assert normalize_template_key(key) == expected


def test_composed_days_build_from_parts_and_label_readably():
    from workouts.templates import compose_day, day_label, day_template, is_composed_key
    gym = [t.slug for t in compose_day("chest_biceps")]
    assert gym == ["bench_press", "incline_db_press", "cable_fly", "pec_deck", "barbell_curl", "hammer_curl", "preacher_curl"]
    assert [t.slug for t in compose_day("legs_shoulders")][:4] == ["squat", "romanian_deadlift", "leg_press", "leg_curl"]
    assert len(compose_day("legs_shoulders")) == 7                      # capped: 4 from the focus part, 3 from the second
    bw = compose_day("chest_triceps", bodyweight=True)
    assert [t.slug for t in bw] == ["pushup", "diamond_pushup", "chair_dip"]   # duplicates across parts dropped
    assert all(t.default_weight == 0 for t in bw)
    assert is_composed_key("chest_biceps") and not is_composed_key("push") and not is_composed_key("chest_x")
    assert day_label("chest_biceps") == "chest + biceps" and day_label("full_body") == "full body" and day_label(None) == "workout"

    class U:  # a gym user vs a bodyweight user get different chest days
        equipment = "full_gym"; custom_templates = None
    assert day_template(U(), "chest")[0].slug == "bench_press"
    U.equipment = "bodyweight"
    assert day_template(U(), "chest")[0].slug == "pushup"
    U.custom_templates = {"chest_biceps": [{"slug": "my_press", "label": "my press", "sets": 3, "reps": 8, "default_weight": 50}]}
    assert [t.slug for t in day_template(U(), "chest_biceps")] == ["my_press"]   # their own day wins
    with pytest.raises(KeyError):
        day_template(U(), "tuesday")


# ─── the cycle: their days, then the label ──────────────────────────────────

def test_cycle_for_prefers_stated_days_then_routine_then_label(db):
    from split_pointer import cycle_for
    assert cycle_for(make_user(db, current_split="bro_split", split_days=ANGEL_DAYS)) == ANGEL_DAYS
    assert cycle_for(make_user(db, current_split="bro_split")) == ["chest", "back", "shoulders", "arms", "legs"]
    assert cycle_for(make_user(db, current_split="ppl")) == ["push", "pull", "legs"]
    # a pasted routine defines the cycle for a loose label, in the order pasted
    rows = [{"slug": "x", "label": "x", "sets": 3, "reps": 8, "default_weight": 50}]
    u = make_user(db, current_split="custom", custom_templates={"back_triceps": rows, "chest_biceps": rows})
    assert cycle_for(u) == ["back_triceps", "chest_biceps"]
    # …but a named system keeps its canonical order even with a partial paste
    v = make_user(db, current_split="ppl", custom_templates={"legs": rows, "push": rows})
    assert cycle_for(v) == ["push", "pull", "legs"]
    assert cycle_for(make_user(db, current_split="custom")) == []
    assert cycle_for(make_user(db, current_split=None)) == []


def test_infer_template_walks_their_days_and_refuses_an_unmapped_split(db):
    from workouts.start import infer_template
    angel = make_user(db, current_split="bro_split", split_days=ANGEL_DAYS)
    assert infer_template(angel) == "chest_biceps"                                   # no pointer → day one
    angel2 = make_user(db, current_split="bro_split", split_days=ANGEL_DAYS, split_pointer_day="back_triceps")
    assert infer_template(angel2) == "legs_shoulders"
    angel3 = make_user(db, current_split="bro_split", split_days=ANGEL_DAYS, split_pointer_day="legs_shoulders")
    assert infer_template(angel3) == "chest_biceps"                                  # wraps
    assert infer_template(make_user(db, current_split="bro_split")) == "chest"        # label alone → classic bro cycle, never full body
    assert infer_template(make_user(db, current_split=None)) == "full_body"           # no split at all → the starting program
    assert infer_template(make_user(db, current_split="none")) == "full_body"
    assert infer_template(make_user(db, current_split="custom")) is None              # "I have a routine" with no days → don't guess
    assert infer_template(make_user(db, current_split="something_else")) is None


def test_split_pointer_advances_through_their_days(db):
    from split_pointer import advance_split_pointer
    from models import User
    u = make_user(db, current_split="bro_split", split_days=ANGEL_DAYS, split_pointer_day="chest_biceps")
    p = advance_split_pointer(u.id, named_day=None)
    assert (p["day"], p["source"]) == ("back_triceps", "inferred")
    # the label-only bro split walks the classic cycle
    v = make_user(db, current_split="bro_split", split_pointer_day="arms")
    assert advance_split_pointer(v.id, named_day=None)["day"] == "legs"
    # a named body-part day is accepted as confirmed
    p = advance_split_pointer(u.id, named_day="legs_shoulders")
    db.expire_all()
    assert db.get(User, u.id).split_pointer_day == "legs_shoulders" and p["source"] == "confirmed"


# ─── the card ───────────────────────────────────────────────────────────────

def test_angel_gets_a_chest_and_biceps_card_not_full_body(db, imessage_on, sidecar_ok, card_ok):
    from workouts.start import start_workout_session
    from models import get_session, SetLog
    angel = make_user(db, name="Angel", preferred_channel="imessage", current_split="bro_split",
                      confirmed_training_split="bro_split", split_days=ANGEL_DAYS, equipment="full_gym",
                      lift_anchors=TEMPLATE_ANCHORS)   # lifts on file → template loads, no first-card ask
    r = start_workout_session(angel.id)
    assert r["template_key"] == "chest_biceps"
    assert sidecar_ok == ["chest + biceps day. starting u at 135 on bench press — first card, weights are off what u told me. tap a set and change the number if it's off, i'll remember."]
    assert card_ok[0]["caption"].startswith("chest + biceps · ")
    s = get_session()
    try:
        slugs = []
        for row in s.query(SetLog).filter_by(session_id=r["session_id"]).order_by(SetLog.id):
            if row.exercise not in slugs:
                slugs.append(row.exercise)
    finally:
        s.close()
    assert slugs == ["bench_press", "incline_db_press", "cable_fly", "pec_deck", "barbell_curl", "hammer_curl", "preacher_curl"]


def test_unmapped_split_refuses_with_an_ask_instead_of_a_full_body_card(db, imessage_on, sidecar_ok, card_ok):
    from workouts.start import start_workout_session
    from agent_tools import dispatch_tool
    u = make_user(db, preferred_channel="imessage", current_split="custom", lift_anchors=TEMPLATE_ANCHORS)
    with pytest.raises(ValueError, match="isn't mapped to days yet"):
        start_workout_session(u.id)
    out = dispatch_tool("start_workout_session", {}, u.id)
    assert out.startswith("error: their split isn't mapped") and "save_routine" in out
    assert sidecar_ok == [] and card_ok == []                                         # nothing went out
    # naming a body-part day still works without a saved cycle
    assert dispatch_tool("start_workout_session", {"template_key": "chest_biceps"}, u.id) == "ok" or "chest" in sidecar_ok[0]


def test_session_summary_and_caption_use_the_readable_day_label(db, imessage_on, sidecar_ok, card_ok):
    from workouts.start import start_workout_session
    from workouts.summary import format_summary
    u = make_user(db, preferred_channel="imessage", current_split="bro_split", split_days=ANGEL_DAYS, lift_anchors=TEMPLATE_ANCHORS)
    r = start_workout_session(u.id, "legs_shoulders")
    assert r["template_key"] == "legs_shoulders" and card_ok[0]["caption"].startswith("legs + shoulders · ")
    head = format_summary({"template_key": "legs_shoulders", "weekday": "tue", "minutes": 0, "pr_count": 0,
                           "volume_lb": 0, "sets_done": 3, "lines": []}).splitlines()[0]
    assert head == "legs + shoulders · tue"


# ─── saving the split from the loop (save_routine tool, form 2) ─────────────

def test_save_routine_tool_accepts_split_days_in_their_words(db):
    from agent_tools import dispatch_tool
    from models import User
    u = make_user(db, current_split=None, split_pointer_day="push", split_pointer_source="confirmed")
    out = dispatch_tool("save_routine", {"split_days": ["chest and biceps", "back and triceps", "legs and shoulders"]}, u.id)
    assert out.startswith("ok: split saved — chest + biceps → back + triceps → legs + shoulders")
    db.expire_all(); u = db.get(User, u.id)
    assert u.split_days == ANGEL_DAYS and u.current_split == "bro_split"
    assert u.split_pointer_day is None                                                 # a pointer outside the new cycle is cleared
    # ppl in words labels itself ppl; an existing label is never overwritten
    v = make_user(db, current_split="upper_lower")
    dispatch_tool("save_routine", {"split_days": ["push", "pull", "legs"]}, v.id)
    db.expire_all(); v = db.get(User, v.id)
    assert v.split_days == ["push", "pull", "legs"] and v.current_split == "upper_lower"
    # junk never lands
    assert dispatch_tool("save_routine", {"split_days": ["monday", "tuesday"]}, u.id).startswith("error:")
    assert dispatch_tool("save_routine", {"split_days": ["chest"]}, u.id).startswith("error:")
    assert dispatch_tool("save_routine", {}, u.id).startswith("error: pass routine_text")
    db.expire_all(); assert db.get(User, u.id).split_days == ANGEL_DAYS


def test_pasted_bro_split_routine_keeps_body_part_days_and_their_order(db, anthropic_stub):
    from workouts.routine import save_routine, describe_routine
    from workouts.templates import day_template
    from models import User
    parsed = {"days": {
        "back_triceps": [{"name": "Lat pulldown", "sets": 4, "reps": 10}, {"name": "Skullcrushers", "sets": 3, "reps": 12}],
        "chest_biceps": [{"name": "Bench press", "sets": 4, "reps": 6}, {"name": "Hammer curls", "sets": 3, "reps": 10}],
        "legs_shoulders": [{"name": "Squat", "sets": 4, "reps": 6}],
        "cardio": [{"name": "run", "sets": 1, "reps": 1}],
    }}
    anthropic_stub.reply_with(lambda kw: json.dumps(parsed))
    u = make_user(db, current_split="bro_split")
    r = save_routine(u.id, "back/tris\nlat pulldown 4x10\nskullcrushers 3x12\nchest/bis\nbench 4x6\nhammer curls 3x10\nlegs+shoulders\nsquat 4x6", source="model")
    assert r == {"days": {"back_triceps": 2, "chest_biceps": 2, "legs_shoulders": 1}, "split": "bro_split"}
    db.expire_all(); u = db.get(User, u.id)
    assert u.split_days == ["back_triceps", "chest_biceps", "legs_shoulders"]          # the order they pasted
    assert [t.slug for t in day_template(u, "chest_biceps")] == ["bench_press", "hammer_curls"]
    assert "back_triceps: 2 exercises (lat pulldown, skullcrushers)" in describe_routine(u.custom_templates)
    # a split they already stated is not re-ordered by a later paste
    v = make_user(db, current_split="bro_split", split_days=ANGEL_DAYS)
    save_routine(v.id, "whatever\n3x10\n3x10\n3x10\n3x10", source="model")
    db.expire_all(); assert db.get(User, v.id).split_days == ANGEL_DAYS


# ─── onboarding: the extractor field and the code trigger ───────────────────

def test_onboarding_captures_the_stated_days_even_when_the_extractor_misses(db, anthropic_stub, sms_capture):
    import onboarding_agent
    from models import User
    from tests.tier1.test_berkeley_friend_onboarding import _new_signup
    user = _new_signup(db, onboarding_step=2, experience="intermediate")
    seen = {}

    def handler(kw):
        if "Extract any fitness coaching profile data" in str(kw["messages"][0]["content"]):
            return json.dumps({"current_split": "bro_split"})                          # exactly what happened live
        seen["system"] = kw["system"]
        return "classic 3 day split. how many days u actually get in a week"
    anthropic_stub.reply_with(handler)
    onboarding_agent.handle_onboarding_reply(user, ANGEL_MSG)
    db.expire_all(); u = db.get(User, user.id)
    assert u.current_split == "bro_split" and u.split_days == ANGEL_DAYS
    sys_text = "".join(b["text"] for b in seen["system"]) if isinstance(seen["system"], list) else seen["system"]
    assert "Their days, in order: chest + biceps → back + triceps → legs + shoulders" in sys_text
    assert "which days they group together" not in sys_text                            # nothing left to ask about the split


def test_extractor_split_days_field_lands_and_labels_the_split(db):
    import onboarding_agent
    from models import User
    from tests.tier1.test_berkeley_friend_onboarding import _new_signup
    user = _new_signup(db, onboarding_step=2)
    onboarding_agent._store_extracted_data(user.id, {"split_days": ["chest and biceps", "back and triceps", "legs and shoulders"]})
    db.expire_all(); u = db.get(User, user.id)
    assert u.split_days == ANGEL_DAYS and u.current_split == "bro_split"
    onboarding_agent._store_extracted_data(user.id, {"split_days": ["monday", "tuesday"]})     # junk never lands
    onboarding_agent._store_extracted_data(user.id, {"split_days": "push pull legs"})           # wrong shape ignored
    db.expire_all(); u = db.get(User, user.id)
    assert u.split_days == ANGEL_DAYS
    onboarding_agent._store_extracted_data(user.id, {"split_days": ["push", "pull", "legs"]})   # latest clear statement wins
    db.expire_all(); assert db.get(User, user.id).split_days == ["push", "pull", "legs"]


def test_extractor_prompt_teaches_split_days(db, anthropic_stub):
    import onboarding_agent
    from tests.tier1.test_berkeley_friend_onboarding import _new_signup
    user = _new_signup(db, onboarding_step=2)
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.setdefault("p", str(kw["messages"][0]["content"])) and "{}")
    onboarding_agent._extract_data_from_message(ANGEL_MSG, user)
    assert '"split_days"' in seen["p"] and "split_days rules" in seen["p"]
    assert '["chest and biceps", "back and triceps", "legs and shoulders"] (and current_split="bro_split")' in seen["p"]


def test_a_bro_split_label_without_days_is_still_unknown_to_onboarding(db):
    import onboarding_agent
    from models import User
    u = make_user(db, onboarding_step=2, goal="fat_loss", current_split="bro_split", experience="intermediate")
    fields = [f[0] for f in onboarding_agent._get_missing_fields(u)]
    assert "split_days" in fields and "current_split" not in fields
    u.split_days = ANGEL_DAYS; db.commit(); db.expire_all()
    assert "split_days" not in [f[0] for f in onboarding_agent._get_missing_fields(db.get(User, u.id))]
    assert "split_days" not in [f[0] for f in onboarding_agent._get_missing_fields(make_user(db, onboarding_step=2, goal="fat_loss", current_split="ppl"))]
    # a pasted routine with its days answers the question too
    rows = [{"slug": "x", "label": "x", "sets": 3, "reps": 8, "default_weight": 50}]
    w = make_user(db, onboarding_step=2, goal="fat_loss", current_split="custom", custom_templates={"chest_biceps": rows, "back_triceps": rows})
    assert "split_days" not in [f[0] for f in onboarding_agent._get_missing_fields(w)]


def test_summary_states_their_split(db):
    import onboarding_agent
    base = dict(onboarding_step=2, height_ft=5, height_in=11, weight_lbs=180, age=20, gender="male",
                goal="fat_loss", workout_days="5,6", workout_time="17:00-18:00",
                wake_time="08:00", sleep_time="00:00", activity_level="moderately active", avg_steps=9000)
    s = onboarding_agent._build_confirmation_summary(make_user(db, **base, current_split="bro_split", split_days=ANGEL_DAYS))
    assert "Split is chest + biceps / back + triceps / legs + shoulders." in s


# ─── the loop sees the split ────────────────────────────────────────────────

def test_loop_context_shows_the_cycle_or_flags_the_gap(db):
    from agent_loop import build_loop_context
    angel = make_user(db, current_split="bro_split", split_days=ANGEL_DAYS, split_pointer_day="chest_biceps", split_pointer_source="confirmed")
    ctx = build_loop_context(angel, db)
    assert "## SPLIT\nbro_split: chest + biceps → back + triceps → legs + shoulders." in ctx
    assert "template_key accepts these day keys: chest_biceps, back_triceps, legs_shoulders" in ctx
    assert "last completed: chest + biceps (confirmed)" in ctx
    gap = build_loop_context(make_user(db, current_split="custom"), db)
    assert "WHICH days they run isn't saved" in gap and "save_routine with split_days" in gap
    assert "## SPLIT" not in build_loop_context(make_user(db, current_split=None), db)


def test_tool_descriptions_teach_body_part_days():
    from agent_tools import START_WORKOUT_SESSION_TOOL, SAVE_ROUTINE_TOOL, LOG_WORKOUT_TOOL
    assert "chest_biceps" in START_WORKOUT_SESSION_TOOL["description"]
    assert "split_days" in SAVE_ROUTINE_TOOL["input_schema"]["properties"] and SAVE_ROUTINE_TOOL["input_schema"]["required"] == []
    assert "chest_biceps" in LOG_WORKOUT_TOOL["input_schema"]["properties"]["split_day"]["description"]
