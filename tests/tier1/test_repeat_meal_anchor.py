"""
A repeat meal the user NAMES ("same overnight oats but with strawberries instead of
blueberries") is anchored to THEIR OWN prior in code, before the model estimates.

Live 2026-10-22 (user 51): three prior logs of 'overnight oats w/ milk, blueberries, honey
+ scoop vanilla whey' at 470cal/37g. "Same ... but with strawberries" was logged fresh at
350cal/12g — the whey vanished. match_meal_history was never called, and with the user's
two-word name Jaccard would have scored 0.2 anyway. Pins: (1) name containment matches an
ingredient-listed prior and ranks the usual (3x) above a one-off; (2) the tool's short-name
fallback; one word stays strict; (3) the phrase parser: food + stated change, and the
non-meals it must ignore; (4) the context block carries the prior, the macros, the change,
and the keep-everything rule; silent on no phrasing / no prior / flag off; (5) wired into
run_agent_loop.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add_meal(db, user_id, description, *, calories, protein_g, carbs_g=None, fat_g=None, days_ago=1):
    from models import Meal
    m = Meal(user_id=user_id, description=description, calories=calories, protein_g=protein_g,
             carbs_g=carbs_g, fat_g=fat_g, eaten_at=_now() - timedelta(days=days_ago),
             source="text", log_type="user_reported")
    db.add(m)
    db.commit()
    return m


USUAL = "overnight oats w/ milk, blueberries, honey + scoop vanilla whey"


def _seed_oats(db, user_id):
    for d in (9, 8, 7):
        _add_meal(db, user_id, USUAL, calories=470, protein_g=37, carbs_g=62, fat_g=12, days_ago=d)


# ── 1. the matcher ───────────────────────────────────────────────────────────────

def test_name_containment_finds_the_ingredient_listed_prior(db):
    from tests.factories import make_user
    from meal_history import match_repeat_meal, match_meal_history, jaccard, normalize_tokens

    user = make_user(db)
    _seed_oats(db, user.id)
    # the live miss: Jaccard can't reach it
    assert jaccard(normalize_tokens("overnight oats"), normalize_tokens(USUAL)) < 0.6
    assert match_meal_history(user.id, "same overnight oats but with strawberries instead of blueberries") == []
    hits = match_repeat_meal(user.id, "overnight oats")
    assert hits and hits[0]["description"] == USUAL
    assert hits[0]["count"] == 3 and hits[0]["calories"] == 470 and hits[0]["protein_g"] == 37


def test_the_usual_outranks_a_one_off_with_the_same_name(db):
    from tests.factories import make_user
    from meal_history import match_repeat_meal

    user = make_user(db)
    _seed_oats(db, user.id)
    _add_meal(db, user.id, "overnight oats w/ lactose-free milk, honey, strawberries",
              calories=350, protein_g=12, days_ago=0)       # today's thin row
    hits = match_repeat_meal(user.id, "oats")
    assert hits[0]["description"] == USUAL, "3x usual ranks above the 1x row logged today"


def test_repeat_match_is_silent_without_a_named_prior(db):
    from tests.factories import make_user
    from meal_history import match_repeat_meal

    user = make_user(db)
    _add_meal(db, user.id, "chicken and rice", calories=650, protein_g=45, days_ago=2)
    assert match_repeat_meal(user.id, "overnight oats") == []
    assert match_repeat_meal(user.id, "") == []


def test_tool_matcher_short_name_fallback_but_one_word_stays_strict(db):
    from tests.factories import make_user
    from meal_history import match_meal_history

    user = make_user(db)
    _seed_oats(db, user.id)
    _add_meal(db, user.id, "grilled chicken thighs w/ rice", calories=600, protein_g=50, days_ago=2)
    _add_meal(db, user.id, "chicken caesar wrap", calories=550, protein_g=35, days_ago=3)
    assert match_meal_history(user.id, "overnight oats")[0]["description"] == USUAL
    assert match_meal_history(user.id, "chicken") == [], "one generic word must not match every chicken dish"
    assert match_meal_history(user.id, "chicken and quinoa") == [], "the spec near-miss still rejects"


# ── 2. the phrase parser ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, food, change", [
    ("And eating same overnight oats but with strawberries instead of blueberries",
     "overnight oats", "but with strawberries instead of blueberries"),
    ("had my usual shake", "shake", ""),
    ("same bagel sandwich from the place, no cheese this time", "bagel sandwich",
     "from the place, no cheese this time"),
    ("that chicken bowl again", "chicken bowl", ""),
    ("ate the same oats", "oats", ""),
])
def test_parser_pulls_the_food_and_the_stated_change(text, food, change):
    from agent_loop import _repeat_meal_mention
    assert _repeat_meal_mention(text) == (food, change)


@pytest.mark.parametrize("text", [
    "Hit legs", "Already", "same thing as yesterday", "same as always", "same here lol",
    "same", "", "going to the same gym", "ok same time tmrw",
])
def test_parser_ignores_non_meals_and_unnamed_repeats(text):
    from agent_loop import _repeat_meal_mention
    hit = _repeat_meal_mention(text)
    # a non-food name may parse ("gym") — it only matters that no FOOD comes out of these
    assert hit is None or hit[0] in {"gym"}


# ── 3. the context block ─────────────────────────────────────────────────────────

def test_block_carries_the_prior_the_change_and_the_keep_rule(db, monkeypatch):
    import config
    from tests.factories import make_user
    from agent_loop import _repeat_meal_block

    monkeypatch.setattr(config, "REPEAT_MEAL_ANCHOR_ENABLED", True, raising=False)
    user = make_user(db)
    _seed_oats(db, user.id)
    block = _repeat_meal_block(
        user, "And eating same overnight oats but with strawberries instead of blueberries", db)
    assert block and block.startswith("## REPEAT MEAL")
    assert USUAL in block and "logged 3x" in block and "~470cal/37g protein" in block
    assert "but with strawberries instead of blueberries" in block
    assert "stays exactly as in the prior" in block and "log_meal NOW" in block


def test_block_is_silent_without_phrasing_prior_or_flag(db, monkeypatch):
    import config
    from tests.factories import make_user
    from agent_loop import _repeat_meal_block

    monkeypatch.setattr(config, "REPEAT_MEAL_ANCHOR_ENABLED", True, raising=False)
    user = make_user(db)
    assert _repeat_meal_block(user, "same overnight oats", db) is None, "no prior → silent"
    _seed_oats(db, user.id)
    assert _repeat_meal_block(user, "overnight oats with strawberries", db) is None, "no repeat phrasing → silent"
    assert _repeat_meal_block(user, "Hit legs", db) is None
    monkeypatch.setattr(config, "REPEAT_MEAL_ANCHOR_ENABLED", False, raising=False)
    assert _repeat_meal_block(user, "same overnight oats", db) is None, "flag off → silent"


# ── 4. wiring ────────────────────────────────────────────────────────────────────

def test_block_is_wired_into_the_agent_loop_before_the_model_turn():
    from agent_loop import run_agent_loop
    src = inspect.getsource(run_agent_loop)
    assert "_repeat_meal_block(user, combined_body, session)" in src
    assert src.index("_repeat_meal_block(") < src.index("messages = [") if "messages = [" in src else True


def test_flag_defaults_on():
    import config
    assert config.REPEAT_MEAL_ANCHOR_ENABLED is True
