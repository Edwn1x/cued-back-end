"""
Live 2026-10-28, user 77, home pull session. Three wrong exercises, one cause: the name → template
matcher (templates.slug_for_name) took any name holding a hint word as that template lift.
- 23:03 the coach promised "dumbbell rows" → the card said barbell row 100 lb (he has dumbbells).
- 23:51 "Can u swap it for me" → the coach asked for a chest-supported incline row + incline curls →
  BOTH came out incline db press ("incline" → incline_db_press): a chest press on pull day, 6 sets of
  it, while the coach texted "chest-supported rows (30s) and incline curls (15s)". Hand-fixed.
- 23:03 the first build crashed: a free-text slug cut at 60 chars into a String(40) column
  ("pull_ups_thumbless_grip_or_slow_negatives", 41).
And the coach had no way to swap ONE exercise on the open card — it closed the card (a summary of
3 pull-up sets went out) and built new ones.

Now: a hint only counts when the name's movement and its variant words agree with the hint's
lift; otherwise the name is its own exercise. Free-text slugs/labels fit their columns. A
swap_card_exercise tool swaps one lift on the open card (the card's own swap logic), done sets kept.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

import config
from tests.factories import make_user, TEMPLATE_ANCHORS
from tests._fake_anthropic import ToolUse

FOUNDER = dict(name="Nau", onboarding_step=3, current_split="ppl", preferred_channel="sms",
               height_ft=5, height_in=6, weight_lbs=139, age=20, gender="male", goal="fat_loss,muscle_building",
               lift_anchors=TEMPLATE_ANCHORS)


# ─── 1. the matcher keeps variants apart ─────────────────────────────────────

@pytest.mark.parametrize("name", [
    "chest-supported incline row", "chest supported row", "chest-supported dumbbell row",
    "chest supported incline dumbbell row", "incline db row", "seal row", "dumbbell row", "db row",
    "machine row", "t-bar row", "incline curl", "incline dumbbell curl", "decline bench",
])
def test_a_variant_of_another_lift_is_its_own_exercise(name):
    from workouts.templates import slug_for_name
    assert slug_for_name(name) is None, name


@pytest.mark.parametrize("name,slug", [
    ("bench", "bench_press"), ("bench press", "bench_press"), ("dumbbell bench", "bench_press"),
    ("incline", "incline_db_press"), ("incline bench", "incline_db_press"), ("incline press", "incline_db_press"),
    ("incline db press", "incline_db_press"), ("incline dumbbell press", "incline_db_press"),
    ("row", "barbell_row"), ("rows", "barbell_row"), ("barbell row", "barbell_row"),
    ("seated cable row", "seated_cable_row"), ("cable row", "seated_cable_row"), ("inverted row", "inverted_row"),
    ("curls", "barbell_curl"), ("barbell curl", "barbell_curl"), ("db curl", "dumbbell_curl"),
    ("dumbbell curl", "dumbbell_curl"), ("hammer curls", "hammer_curl"), ("preacher curl", "preacher_curl"),
    ("leg curl", "leg_curl"), ("cable curl", "cable_curl"),
    ("squat", "squat"), ("back squat", "squat"), ("ohp", "overhead_press"), ("lat pulldown", "lat_pulldown"),
    ("rdl", "romanian_deadlift"), ("dumbbell lateral raise", "lateral_raise"), ("pull-ups", None),
    ("chest press", "chest_press_machine"), ("rear delt fly", "rear_delt_fly"),
])
def test_the_lifts_it_matched_before_still_match(name, slug):
    from workouts.templates import slug_for_name
    assert slug_for_name(name) == slug, name


def test_the_card_swap_resolver_gets_it_right_too():
    from card_page import resolve_exercise_name
    assert resolve_exercise_name("chest supported row", set()) == ("chest_supported_row", "chest supported row")
    assert resolve_exercise_name("incline curl", set()) == ("incline_curl", "incline curl")
    assert resolve_exercise_name("rows", set())[0] == "barbell_row"


# ─── 2. a long free-text name fits the columns ───────────────────────────────

def test_a_long_free_text_name_fits_slug_and_label_columns():
    from workouts.start import one_off_templates
    from models import SetLog
    slug_max = SetLog.__table__.c.exercise.type.length
    label_max = SetLog.__table__.c.exercise_label.type.length
    long = "pull ups (thumbless grip or slow negatives if the thumb hurts, 3-5 sec down)"
    (t,) = one_off_templates([{"name": long, "sets": 3, "reps": 8}])
    assert len(t.slug) <= slug_max and len(t.label) <= label_max


def test_the_live_long_name_builds_a_session_instead_of_crashing(db):
    from workouts.plan import build_session
    from workouts.start import one_off_templates
    u = make_user(db, **FOUNDER)
    ex = one_off_templates([{"name": "pull_ups_thumbless_grip_or_slow_negatives", "sets": 3, "reps": 8},
                            {"name": "chest-supported incline row", "sets": 3, "reps": 12, "weight": 30},
                            {"name": "incline curl", "sets": 3, "reps": 12, "weight": 15}])
    ws = build_session(u, "pull", now=datetime(2026, 10, 28, 23, 0), exercises=ex)
    from models import SetLog
    db.expire_all()
    slugs = [r.exercise for r in db.query(SetLog).filter_by(session_id=ws.id).order_by(SetLog.id)]
    assert "incline_db_press" not in slugs and "chest_supported_incline_row" in slugs and "incline_curl" in slugs


# ─── 3. the coach swaps one lift on the open card ────────────────────────────

@pytest.fixture
def open_card(db, monkeypatch):
    import workouts.card as card_mod
    refreshed = []
    monkeypatch.setattr(card_mod, "refresh_card_async", lambda sid: refreshed.append(sid))
    monkeypatch.setattr(config, "SWAP_CARD_EXERCISE_TOOL_ENABLED", True, raising=False)
    from workouts.plan import build_session
    from workouts.start import one_off_templates
    u = make_user(db, **FOUNDER)
    ex = one_off_templates([{"name": "pull ups", "sets": 3, "reps": 8, "weight": 0},
                            {"name": "incline db press", "sets": 3, "reps": 12, "weight": 50},
                            {"name": "dumbbell curl", "sets": 3, "reps": 12, "weight": 20}])
    ws = build_session(u, "pull", now=datetime(2026, 10, 28, 23, 0), exercises=ex)
    return u, ws.id, refreshed


def _rows(db, sid):
    from models import SetLog
    db.expire_all()
    return [(r.exercise, r.exercise_label, r.planned_weight, r.planned_reps, r.done)
            for r in db.query(SetLog).filter_by(session_id=sid).order_by(SetLog.id)]


def test_swap_replaces_the_lift_on_the_same_card(db, open_card):
    from agent_tools import dispatch_tool, SWAP_CARD_EXERCISE_TOOL
    assert SWAP_CARD_EXERCISE_TOOL["name"] == "swap_card_exercise"
    u, sid, refreshed = open_card
    out = dispatch_tool("swap_card_exercise", {"exercise": "incline db press", "to": "chest-supported db row",
                                               "weight": 30, "reps": 12}, u.id)
    assert out.startswith("ok:") and "chest-supported db row" in out and "30" in out, out
    rows = _rows(db, sid)
    assert not any(r[0] == "incline_db_press" for r in rows)
    assert [r[2:4] for r in rows if r[0] == "chest_supported_db_row"] == [(30, 12)] * 3
    from models import WorkoutSession
    assert db.get(WorkoutSession, sid).status in ("planned", "active"), "the card stays open — no summary"
    assert refreshed == [sid]


def test_swap_keeps_done_sets_and_finds_the_lift_by_label_or_slug(db, open_card):
    from agent_tools import dispatch_tool
    from models import SetLog
    u, sid, _ = open_card
    first = db.query(SetLog).filter_by(session_id=sid, exercise="dumbbell_curl").order_by(SetLog.id).first()
    first.done, first.actual_weight, first.actual_reps = True, 20, 12
    db.commit()
    out = dispatch_tool("swap_card_exercise", {"exercise": "dumbbell_curl", "to": "incline curl", "weight": 15,
                                               "reps": 12}, u.id)
    assert out.startswith("ok:"), out
    rows = _rows(db, sid)
    assert [r for r in rows if r[0] == "dumbbell_curl"] == [("dumbbell_curl", "dumbbell curl", 20, 12, True)]
    assert len([r for r in rows if r[0] == "incline_curl"]) == 2, "as many sets as were left"


@pytest.mark.parametrize("args,needle", [
    ({"exercise": "deadlift", "to": "rows"}, "on the card: pull ups, incline db press, dumbbell curl"),
    ({"exercise": "incline db press", "to": "dumbbell curl"}, "already on the card"),
    ({"exercise": "incline db press", "to": "landmine row"}, "weight and reps"),
    ({"exercise": "", "to": "rows"}, "exercise and to are required"),
])
def test_swap_errors_say_what_to_do(db, open_card, args, needle):
    from agent_tools import dispatch_tool
    u, sid, _ = open_card
    out = dispatch_tool("swap_card_exercise", args, u.id)
    assert out.startswith("error:") and needle in out, out


def test_swap_without_an_open_card_says_so(db):
    from agent_tools import dispatch_tool
    u = make_user(db, **FOUNDER)
    assert "no open card" in dispatch_tool("swap_card_exercise", {"exercise": "row", "to": "seal row",
                                                                 "weight": 30, "reps": 10}, u.id)


def test_the_live_swap_ask_keeps_the_card_open(db, open_card, anthropic_stub, sms_capture):
    """23:50 "Can u swap it for me", replayed with the model using the tool: same session, no summary."""
    import agent_loop
    from models import WorkoutSession, Message
    u, sid, _ = open_card
    anthropic_stub.push(ToolUse("swap_card_exercise", {"exercise": "incline db press", "to": "chest-supported db row",
                                                       "weight": 30, "reps": 12}, id="toolu_s"),
                        "swapped it on ur card: chest-supported db row, 30s for 12")
    reply = agent_loop.run_agent_loop(u, "Can u swap it for me", "freeform")
    assert "chest-supported db row" in reply
    db.expire_all()
    assert db.get(WorkoutSession, sid).status in ("planned", "active")
    assert not db.query(Message).filter_by(user_id=u.id, message_type="workout_summary").count()
    assert any(r[0] == "chest_supported_db_row" for r in _rows(db, sid)), "the swap actually landed"


def test_the_loop_offers_it_behind_the_flag(db, anthropic_stub, monkeypatch):
    import agent_loop
    monkeypatch.setattr(config, "SWAP_CARD_EXERCISE_TOOL_ENABLED", True, raising=False)
    anthropic_stub.reply_with(lambda kw: "ok")
    u = make_user(db, **FOUNDER)
    agent_loop.run_agent_loop(u, "hey", "freeform")
    assert "swap_card_exercise" in {t.get("name") for t in anthropic_stub.calls[-1].get("tools") or []}
    monkeypatch.setattr(config, "SWAP_CARD_EXERCISE_TOOL_ENABLED", False)
    agent_loop.run_agent_loop(u, "hey", "freeform")
    assert "swap_card_exercise" not in {t.get("name") for t in anthropic_stub.calls[-1].get("tools") or []}


def test_voice_says_swap_on_the_card_never_close_and_rebuild():
    import re
    from agent_loop import _voice_prompt
    v = re.sub(r"\s+", " ", _voice_prompt())
    assert "swap_card_exercise" in v and "never close the card and send a new one to swap a lift" in v


def test_the_open_card_block_lists_the_lifts_and_points_at_swap(db, open_card):
    """23:33 the coach said "dumbbell rows → chest-supported row" while the card said barbell row: the
    block never named the card's lifts, and its only instruction was reset_workout_session."""
    from workouts.session_ops import active_session_brief
    u, sid, _ = open_card
    block = active_session_brief(u)
    import re
    assert re.search(r"on the card: pull ups 3×8, incline db press 3×12 @ \d+, dumbbell curl 3×12 @ \d+\.", block), block
    assert "swap_card_exercise" in block and "not reset_workout_session" in block
