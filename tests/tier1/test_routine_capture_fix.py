"""
Routine-capture fix (2026-09-27, live incident user 31).

He dictated his real pull day — "start off with pull ups to warm up", biceps = "ez bar
curls OR seated leaning-back dumbbell curls OR cable bayesian curls OR two-hand cable
curls" (rotating alternatives, he picks ONE), back = "lat pulldown, seated rows, back
extensions, lat push downs". save_routine SAVED WRONG: the four bicep OPTIONS became
four separate curl slots (four curls every session) and the pull-ups warmup was dropped,
then the coach said "that's your real card now."

Fixes here:
  1. alternatives for one movement → ONE slot whose label carries the options, never N.
  2. a named warmup ("pull ups to warm up") is kept as the FIRST slot; a dictated
     movement with no sets×reps is defaulted (3×10), not dropped.
  3. save_routine reflects back the ACTUAL saved list so a mismatch is visible.
  4. a day with no saved routine surfaces the no-routine/default state (capture offered),
     in the loop context and on the start_workout_session tool result.
"""

from __future__ import annotations

import json

import pytest

from tests.factories import make_user, TEMPLATE_ANCHORS
from tests.tier1.test_workout_phases_3_4_5 import imessage_on, sidecar_ok, card_ok  # noqa: F401 — fixtures


def _is_parse(kw):
    return "Parse this workout routine" in str(kw["messages"][0]["content"])


# a stand-in for the real dictation (the stub ignores it; parse_routine just needs a string).
PULL_TEXT = ("pull day\nstart with pull ups to warm up\n"
             "ez bar curls or seated db curls or cable bayesian curls or two-hand cable curls 3x10\n"
             "lat pulldown\nseated rows 3x10")

# the real dictation, as one movement of alternatives + a stated warmup + a plain list.
PULL_WITH_ALTERNATIVES = {"days": {"pull": [
    {"name": "pull up", "bodyweight": True},                              # warmup, no sets/reps stated
    {"name": "ez bar curl",
     "options": ["ez bar curl", "seated db curl", "cable bayesian curl", "two-hand cable curl"],
     "sets": 3, "reps": 10},                                             # ONE slot, four options
    {"name": "lat pulldown"},                                            # dictated, no sets/reps
    {"name": "seated row", "sets": 3, "reps": 10},
]}}


# ─── 1. alternatives collapse to one slot ─────────────────────────────────────

def test_alternatives_for_one_movement_save_as_one_slot(anthropic_stub):
    from workouts.routine import parse_routine
    anthropic_stub.reply_with(lambda kw: json.dumps(PULL_WITH_ALTERNATIVES))
    out = parse_routine(PULL_TEXT, user_id=1)
    pull = out["pull"]
    curl_rows = [r for r in pull if "curl" in r["slug"]]
    assert len(curl_rows) == 1                                            # NOT four separate curls
    curl = curl_rows[0]
    assert curl["slug"] == "ez_bar_curl"                                  # slug from the primary choice
    # the alternatives ride along in the label (render-safe representation), fits VARCHAR(60)
    assert curl["label"].startswith("ez bar curl (or ")
    assert "seated db curl" in curl["label"] and "cable bayesian curl" in curl["label"]
    assert len(curl["label"]) <= 60


def test_alternatives_flag_off_keeps_them_flat(anthropic_stub, monkeypatch):
    import config
    from workouts.routine import parse_routine
    monkeypatch.setattr(config, "ROUTINE_ALTERNATIVES_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: json.dumps(PULL_WITH_ALTERNATIVES))
    out = parse_routine(PULL_TEXT, user_id=1)
    curl = [r for r in out["pull"] if "curl" in r["slug"]][0]
    assert "(or" not in curl["label"]                                    # no options folded in


# ─── 2. stated warmup is the first slot; dictated moves get defaults ──────────

def test_stated_warmup_is_the_first_slot_and_missing_counts_default(anthropic_stub):
    from workouts.routine import parse_routine
    anthropic_stub.reply_with(lambda kw: json.dumps(PULL_WITH_ALTERNATIVES))
    out = parse_routine(PULL_TEXT, user_id=1)
    pull = out["pull"]
    assert pull[0]["slug"] == "pull_up"                                   # warmup kept, and FIRST
    assert pull[0]["default_weight"] == 0                                 # bodyweight
    assert pull[0]["sets"] == 3 and pull[0]["reps"] == 10                 # missing counts defaulted
    lat = [r for r in pull if r["slug"] == "lat_pulldown"][0]
    assert lat["sets"] == 3 and lat["reps"] == 10                         # dictated, no counts → default


def test_present_but_unparseable_count_is_still_dropped(anthropic_stub):
    from workouts.routine import parse_routine
    anthropic_stub.reply_with(lambda kw: json.dumps({"days": {"pull": [
        {"name": "good", "sets": 3, "reps": 8},
        {"name": "junk", "sets": "lots", "reps": 10},                    # malformed, not "missing"
    ]}}))
    out = parse_routine("x", user_id=1)
    assert [r["slug"] for r in out["pull"]] == ["good"]                   # junk dropped, not defaulted


# ─── 3. save_routine reflects back the actual saved list ─────────────────────

def test_save_routine_reflects_back_the_actual_saved_exercises(db, anthropic_stub):
    from agent_tools import handle_save_routine
    anthropic_stub.reply_with(lambda kw: json.dumps(PULL_WITH_ALTERNATIVES))
    u = make_user(db, current_split=None)
    out = handle_save_routine(u.id, {"routine_text": "here's my real pull day, " + "x" * 30})
    assert "routine saved to their cards" in out and "placeholders" in out
    # the ACTUAL saved list is handed back for the coach to reflect
    assert "read it back" in out.lower()
    assert "pull up" in out                                              # the warmup shows
    assert "ez bar curl (or" in out                                     # one curl slot, options visible
    # and only ONE curl entry made it into the stored template
    from models import User
    db.expire_all(); u = db.get(User, u.id)
    curls = [r for r in u.custom_templates["pull"] if "curl" in r["slug"]]
    assert len(curls) == 1


# ─── 4. no routine on file → the default state is surfaced, capture offered ───

def test_start_workout_flags_a_default_day_and_offers_capture(db, imessage_on, sidecar_ok, card_ok):
    from agent_tools import handle_start_workout_session
    u = make_user(db, preferred_channel="imessage", current_split="ppl",
                  split_days=["push", "pull", "legs"], lift_anchors=TEMPLATE_ANCHORS)
    out = handle_start_workout_session(u.id, {"template_key": "pull"})
    assert out.startswith("ok:")
    assert "STARTING DEFAULT" in out and "save_routine" in out
    assert "[silent]" not in out or "don't reply [silent]" in out        # the offer breaks the silent contract
    assert card_ok, "the default card still goes out"                    # not a hard block


def test_start_workout_stays_silent_when_the_day_is_theirs(db, imessage_on, sidecar_ok, card_ok, anthropic_stub):
    from agent_tools import handle_save_routine, handle_start_workout_session
    anthropic_stub.reply_with(lambda kw: json.dumps(PULL_WITH_ALTERNATIVES))
    u = make_user(db, preferred_channel="imessage", current_split="ppl",
                  split_days=["push", "pull", "legs"], lift_anchors=TEMPLATE_ANCHORS)
    handle_save_routine(u.id, {"routine_text": "my pull day " + "x" * 30})   # pull is now theirs
    out = handle_start_workout_session(u.id, {"template_key": "pull"})
    assert "STARTING DEFAULT" not in out and "Reply with exactly [silent]." in out


def test_loop_context_surfaces_the_no_routine_state(db):
    from agent_loop import build_loop_context
    u = make_user(db, current_split="ppl", split_days=["push", "pull", "legs"])
    ctx = build_loop_context(u, db)
    assert "## THEIR ROUTINE" in ctx and "none on file" in ctx and "save_routine" in ctx


def test_loop_context_shows_their_saved_routine(db, anthropic_stub):
    from agent_loop import build_loop_context
    from models import User
    anthropic_stub.reply_with(lambda kw: json.dumps(PULL_WITH_ALTERNATIVES))
    from workouts.routine import save_routine
    u = make_user(db, current_split="ppl", split_days=["push", "pull", "legs"])
    save_routine(u.id, "my pull day " + "x" * 30, source="model")
    db.expire_all(); u = db.get(User, u.id)
    ctx = build_loop_context(u, db)
    assert "## THEIR ROUTINE (on their workout cards" in ctx
    assert "none on file" not in ctx
    assert "pull:" in ctx                                                # the real day is described


# ─── 5. card renders the alternatives label without breaking plain templates ─

def _state(label):
    return {"session": {"template_key": "pull", "weekday": "mon", "status": "planned"},
            "done_count": 0, "set_count": 3, "volume_lb": 0, "bodyweight": False,
            "exercises": [{"label": label, "sets": [{"planned_weight": 30, "planned_reps": 10}]}]}


def test_card_layout_renders_the_alternatives_label():
    from workouts.card import card_layout
    alt = "ez bar curl (or cable bayesian curl / two-hand cable curl)"
    sub = card_layout(_state(alt))["subcaption"]
    assert alt in sub                                                    # options shown in the bubble
    # a plain (old, no-options) label still renders — graceful degrade
    plain = card_layout(_state("lat pulldown"))["subcaption"]
    assert "lat pulldown" in plain and "(or" not in plain


def test_saved_alternatives_carry_through_to_the_session_card(db, imessage_on, sidecar_ok, card_ok, anthropic_stub):
    """End to end: the collapsed-alternatives label survives save → session → SetLog → card."""
    from agent_tools import handle_save_routine
    from workouts.start import start_workout_session
    from models import get_session, SetLog
    anthropic_stub.reply_with(lambda kw: json.dumps(PULL_WITH_ALTERNATIVES))
    u = make_user(db, preferred_channel="imessage", current_split="ppl",
                  split_days=["push", "pull", "legs"], lift_anchors=TEMPLATE_ANCHORS)
    handle_save_routine(u.id, {"routine_text": "my pull day " + "x" * 30})
    r = start_workout_session(u.id, "pull")
    s = get_session()
    try:
        labels = [x.exercise_label for x in s.query(SetLog).filter_by(session_id=r["session_id"]).order_by(SetLog.id)]
    finally:
        s.close()
    assert any(l.startswith("ez bar curl (or ") for l in labels)         # one curl slot, options in the label
    assert sum(1 for l in labels if "curl" in l) == 3                    # 3 sets of the ONE curl slot, not 12
