"""
Tier-2 (live) — vision first-pass thoroughness (rewrite/vision-thoroughness Fix 1).

Live incident (Aug 8): a photo of plated eggs with a jam jar, egg-white carton and
bread bag in frame logged ONLY the eggs; the rest was silently ignored until "look
again" proved vision could read all of it. Tier-1 pins the prompt composition and
the storage routing; these judge the MODEL's behavior: one pass captures the plate
AND the surroundings, routed to the right places, acknowledged naturally, with no
macro inflation.

Run: pytest --run-tier2 -s tests/tier2/test_vision_thoroughness.py
Fixture: breakfast_scene.png (synthetic, committed; regenerate via
tests/fixtures/generate_macro_fixtures.py) — plated eggs + labeled STRAWBERRY JAM
jar, EGG WHITES carton, WHOLE WHEAT BREAD bag. Fixture realism is the known
first-run risk: if the model balks at the stylized scene, swap a real photo before
concluding the prompt failed.
"""

from __future__ import annotations

import base64
import os

import pytest

pytestmark = pytest.mark.tier2

_FIXDIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

# The three seen-but-not-eaten items, by the words on their packaging.
_ON_HAND_MARKERS = ("jam", "egg white", "bread")


def _img(name) -> dict:
    with open(os.path.join(_FIXDIR, name), "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _enable(monkeypatch, **flags):
    import config
    defaults = dict(READ_IMAGE_ENABLED=True, MEAL_ESTIMATION_PROMPT_ENABLED=True,
                    LOG_MEAL_TOOL_ENABLED=True, REMEMBER_TOOL_ENABLED=True,
                    MANAGE_LOG_TOOL_ENABLED=True)
    defaults.update(flags)
    for name, val in defaults.items():
        monkeypatch.setattr(config, name, val)


def _meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).all()
    finally:
        s.close()


def _profile(user_id):
    from models import get_session, User
    s = get_session()
    try:
        return dict(s.query(User).filter(User.id == user_id).one().user_profile_memory or {})
    finally:
        s.close()


def test_multi_item_scene_first_pass_captures_plate_and_surroundings(db, monkeypatch):
    """The whole spec in one turn: eaten → meal log; jam/egg whites/bread →
    food_on_hand; reply sees the scene; macros not inflated by the uneaten items."""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "breakfast", "freeform",
                           image_data=_img("breakfast_scene.png"))
    print(f"\n[SCENE reply] {reply!r}")

    meals = _meals(user.id)
    for m in meals:
        print(f"[SCENE logged] {m.description!r} {m.calories}cal/{m.protein_g}g")
    profile = _profile(user.id)
    on_hand = profile.get("food_on_hand") or []
    for e in on_hand:
        print(f"[SCENE on-hand] {e.get('text')!r}")

    # 1a. the eaten item logs as a meal
    assert meals, f"scene photo logged no meal: {reply!r}"
    meal_text = " ".join(m.description or "" for m in meals).lower()
    assert "egg" in meal_text, f"the plated eggs are the eaten item: {meal_text!r}"

    # 1b. the surroundings are CAPTURED, not ignored (the Aug 8 failure) — all
    # three labeled items are large, legible, and unambiguous in this fixture.
    on_hand_text = " ".join(e.get("text", "") for e in on_hand).lower()
    captured = [mk for mk in _ON_HAND_MARKERS if mk in on_hand_text]
    assert len(captured) >= 2, (
        f"first pass ignored the surroundings again (captured only {captured} "
        f"in food_on_hand): {on_hand_text!r} — reply was {reply!r}")

    # 3. no inflation: the uneaten items stay OUT of the meal log, and the
    # calories are an eggs-only number (eggs+jam+bread+egg-whites would not be).
    assert not any(mk in meal_text for mk in _ON_HAND_MARKERS), (
        f"visible-but-uneaten items leaked into the meal log: {meal_text!r}")
    total_cal = sum(m.calories or 0 for m in meals)
    assert total_cal <= 600, (
        f"{total_cal} cal for a plate of eggs suggests the visible groceries "
        f"were folded into the eaten macros: {meal_text!r}")


def test_reply_acknowledges_scene_naturally(db, monkeypatch):
    """A friend seeing the photo mentions the scene ('eggs now — and I see jam and
    bread there too'), instead of replying as if only the plate existed. Judged on
    the reply text alone; separate turn so a capture failure above doesn't mask a
    phrasing failure here."""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "breakfast", "freeform",
                           image_data=_img("breakfast_scene.png"))
    print(f"\n[ACK reply] {reply!r}")

    assert any(mk in reply.lower() for mk in _ON_HAND_MARKERS), (
        f"reply never acknowledges anything beyond the plate: {reply!r}")


def test_plain_plate_logs_cleanly_with_no_spurious_on_hand(db, monkeypatch):
    """Regression + over-capture guard: a photo of ONLY a plated meal (plate, fork,
    table — nothing else edible in frame) still logs, and produces no invented
    food_on_hand entries."""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "lunch", "freeform", image_data=_img("plate_meal.png"))
    print(f"\n[PLAIN reply] {reply!r}")

    assert _meals(user.id), f"plain plate photo regressed — no meal logged: {reply!r}"
    on_hand = _profile(user.id).get("food_on_hand") or []
    for e in on_hand:
        print(f"[PLAIN spurious on-hand] {e.get('text')!r}")
    assert not on_hand, (
        f"nothing else is in this frame; on-hand entries were invented: "
        f"{[e.get('text') for e in on_hand]!r}")
