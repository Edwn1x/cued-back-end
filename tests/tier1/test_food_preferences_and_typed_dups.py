"""
Memory follow-ups from Jayden's first day (user 40, 2026-10-17 19:40 PT).

"I really don't like things with added protein like protein shakes or bars" → the coach
saved it under training_preferences (there was no food bucket). That category was already
370/400 chars of facts duplicating his TYPED columns (10k steps, cooking mix, Apple Watch,
canned, spicy), so the write evicted "prefers machine exercises over barbell — wrist
anxiety": the one training fact his cards depend on. Two fixes:
  1. a food_preferences category, with food facts misfiled under training_preferences
     moved there on the next write;
  2. a fact that restates a typed column is skipped at write time.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace as NS

import pytest

import config
from tests.factories import make_user

JAYDEN_COLS = dict(avg_steps=10000, cooking_situation="mix", existing_tools="apple_fitness,apple_watch",
                   restrictions="won't eat canned food; won't eat canned fish; won't eat spicy foods",
                   workout_days="4", workout_time="22:30")
DUPS = ["averages about 10,000 steps a day", "eats an equal mix of eating out and home-cooked meals",
        "uses the Apple Fitness app and an Apple Watch to track activity",
        "dislikes canned foods, especially canned fish", "dislikes spicy foods"]
REAL = ["likes incorporating stairmaster cardio into workouts",
        "prefers machine exercises over barbell/free-weight work because of the wrist",
        "loves deadlifts", "uses a Garmin", "prefers to train 3 days a week because of soccer"]


def _facts(texts, category="training_preferences"):
    return [{"action": "add", "category": category, "text": t, "replaces_text": None, "safety_critical": False} for t in texts]


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


# ── 1. the category ──────────────────────────────────────────────────────────

def test_food_preferences_is_a_category_the_tool_and_the_extractor_know():
    import app
    from memory import CATEGORIES, CATEGORY_INJECTION_MAP
    from agent_tools import REMEMBER_TOOL
    assert "food_preferences" in CATEGORIES
    assert "food_preferences" in CATEGORY_INJECTION_MAP["nutrition"]["categories"]
    assert "food_preferences" in REMEMBER_TOOL["input_schema"]["properties"]["category"]["enum"]
    assert "'food_preferences'" in REMEMBER_TOOL["description"]
    src = open(app.__file__, encoding="utf-8").read()
    assert "- food_preferences" in src and "daily step count" in src and "fitness apps and wearables" in src


def test_food_facts_misfiled_under_training_preferences_move_on_the_next_write(caplog):
    from memory import apply_facts, reclassify_food_preferences
    prof = {"training_preferences": [
        {"id": "a", "text": "likes incorporating stairmaster cardio into workouts", "ts": "t", "uses": 1},
        {"id": "b", "text": "Dislikes protein-added products like protein shakes and bars — finds the flavor and texture off-putting.", "ts": "t", "uses": 1},
        {"id": "c", "text": "pre-workout meal timing matters", "ts": "t", "uses": 0},
        {"id": "d", "text": "hates morning cardio", "ts": "t", "uses": 0},
    ]}
    caplog.set_level(logging.INFO)
    new, stats = apply_facts(prof, [], user_id=40)
    assert [e["id"] for e in new["training_preferences"]] == ["a", "c", "d"]
    assert [e["id"] for e in new["food_preferences"]] == ["b"] and new["food_preferences"][0]["uses"] == 1
    assert "MEMORY_RECLASSIFIED" in caplog.text
    assert reclassify_food_preferences({"training_preferences": [{"id": "x", "text": "hates morning cardio", "safety": False}]}) == 0


def test_reclassification_flag_off_leaves_entries_where_they_are(monkeypatch):
    from memory import apply_facts
    monkeypatch.setattr(config, "MEMORY_FOOD_PREFERENCES_ENABLED", False)
    prof = {"training_preferences": [{"id": "b", "text": "dislikes canned fish", "ts": "t", "uses": 0}]}
    new, _ = apply_facts(prof, [], user_id=1)
    assert [e["id"] for e in new["training_preferences"]] == ["b"] and new["food_preferences"] == []


def test_the_loop_renders_food_preferences_and_the_remember_tool_stores_there(db):
    from agent_tools import handle_remember
    from memory import render_categories, CATEGORIES
    u = make_user(db, **JAYDEN_COLS)
    r = handle_remember(u.id, {"action": "add", "category": "food_preferences",
                               "text": "won't touch protein shakes or bars — texture"})
    assert r.startswith("ok: add") and "'added': 1" in r
    prof = _u(u.id).user_profile_memory
    assert [e["text"] for e in prof["food_preferences"]] == ["won't touch protein shakes or bars — texture"]
    txt, _ = render_categories(prof, tuple(c for c in CATEGORIES if c != "coaching_lessons"), include_safety_universal=True, show_ids=False)
    assert "protein shakes" in txt


# ── 2. typed-column duplicates are skipped at write time ─────────────────────

@pytest.mark.parametrize("text,column", [
    (DUPS[0], "avg_steps"), (DUPS[1], "cooking_situation"), (DUPS[2], "existing_tools"),
    (DUPS[3], "restrictions"), (DUPS[4], "restrictions"),
    ("prefers late-night gym sessions around 10:30pm", "workout_time"), ("trains 4 days a week", "workout_days"),
] + [(t, None) for t in REAL] + [("walks the dog every morning", None)])
def test_typed_column_duplicate_detection(text, column):
    from memory import typed_column_duplicate
    assert typed_column_duplicate(NS(**JAYDEN_COLS), text) == column


def test_empty_typed_columns_never_match():
    from memory import typed_column_duplicate
    blank = NS(avg_steps=None, cooking_situation=None, existing_tools=None, restrictions=None, workout_days=None, workout_time=None)
    assert all(typed_column_duplicate(blank, t) is None for t in DUPS)
    assert typed_column_duplicate(None, DUPS[0]) is None


def test_live_user_40_duplicates_are_skipped_and_real_facts_kept(caplog):
    from memory import apply_facts, _category_chars
    caplog.set_level(logging.INFO)
    new, stats = apply_facts({}, _facts(DUPS + REAL), user_id=40, user=NS(**JAYDEN_COLS))
    kept = [e["text"] for e in new["training_preferences"]]
    assert kept == REAL and stats["skipped"] == 5 and stats["added"] == 5
    assert caplog.text.count("MEMORY_TYPED_DUP_SKIPPED") == 5
    assert _category_chars(new["training_preferences"]) < config.USER_PROFILE_MEMORY_CATEGORY_SOFT_CAP


def test_without_the_row_or_with_the_flag_off_nothing_is_skipped(monkeypatch):
    from memory import apply_facts
    new, stats = apply_facts({}, _facts(DUPS), user_id=40)                      # no user row (admin / lessons paths)
    assert stats["added"] == 5
    monkeypatch.setattr(config, "MEMORY_TYPED_DUP_SKIP_ENABLED", False)
    new, stats = apply_facts({}, _facts(DUPS), user_id=40, user=NS(**JAYDEN_COLS))
    assert stats["added"] == 5


def test_safety_facts_are_never_skipped_as_duplicates():
    from memory import apply_facts
    u = NS(**dict(JAYDEN_COLS, restrictions="allergic to peanuts"))
    facts = [{"action": "add", "category": "constraints", "text": "allergic to peanuts — carries an epipen",
              "replaces_text": None, "safety_critical": True}]
    new, stats = apply_facts({}, facts, user_id=1, user=u)
    assert stats["added"] == 1 and new["constraints"][0].get("safety") is True


def test_remember_tool_reports_the_skip_and_the_extractor_path_passes_the_row(db, caplog):
    from agent_tools import handle_remember
    u = make_user(db, **JAYDEN_COLS)
    caplog.set_level(logging.INFO)
    r = handle_remember(u.id, {"action": "add", "category": "training_preferences", "text": DUPS[0]})
    assert "'skipped': 1" in r and "'added': 0" in r and "MEMORY_TYPED_DUP_SKIPPED" in caplog.text
    assert (_u(u.id).user_profile_memory or {}).get("training_preferences", []) == []
    # app.extract_and_store_memory passes the locked row through
    import inspect, app
    assert "apply_facts(current_profile, facts, user_id=user_id, user=user)" in inspect.getsource(app.extract_and_store_memory)
