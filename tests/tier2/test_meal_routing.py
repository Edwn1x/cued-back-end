"""
Tier-2 (live) — macro-accuracy Phase E: the escalation ladder in the model's hands.

The spec's five judged cases, with CLIENT tool dispatches recorded via a
passthrough wrapper around agent_loop's dispatch — so "stopped at rung 0" and
"climbed to USDA" are observable, not vibes. (web_search is server-side and not
observable here; its absence is not asserted.)

Run: pytest --run-tier2 -s tests/tier2/test_meal_routing.py
Fixtures: tests/fixtures/nutrition_label.png, plate_meal.png (see
generate_macro_fixtures.py; realism is a first-run validation item).
USDA: uses USDA_API_KEY from env, falling back to DEMO_KEY.
"""

from __future__ import annotations

import base64
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.tier2

_FIXDIR = os.path.join(os.path.dirname(__file__), "..", "fixtures")

_DB_TOOLS = {"match_meal_history", "match_dining_item", "usda_food_lookup"}


def _img(name) -> dict:
    with open(os.path.join(_FIXDIR, name), "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


def _enable_all(monkeypatch):
    import config
    for f in ("READ_IMAGE_ENABLED", "MEAL_ESTIMATION_PROMPT_ENABLED",
              "MEAL_ROUTING_PROMPT_ENABLED", "LOG_MEAL_TOOL_ENABLED",
              "MEAL_HISTORY_TOOL_ENABLED", "DINING_MATCH_TOOL_ENABLED",
              "USDA_LOOKUP_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED",
              "REMEMBER_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "USDA_API_KEY",
                        os.getenv("USDA_API_KEY") or "DEMO_KEY")


def _record_dispatches(monkeypatch):
    """Passthrough wrapper: real tools run, but every dispatch is recorded."""
    import agent_tools
    real = agent_tools.dispatch_tool
    calls: list[str] = []

    def recording(name, tool_input, user_id, *, message_id=None):
        calls.append(name)
        return real(name, tool_input, user_id, message_id=message_id)

    # agent_loop imports dispatch_tool inside the loop body from agent_tools,
    # so patching the module attribute catches it.
    monkeypatch.setattr(agent_tools, "dispatch_tool", recording)
    return calls


def _meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).all()
    finally:
        s.close()


def test_clear_label_resolves_at_rung_zero_no_database_tools(db, monkeypatch):
    """Easy case: printed numbers in view. The don't-force-all-tools guard —
    zero database-tool dispatches, label math in the log."""
    _enable_all(monkeypatch)
    calls = _record_dispatches(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "just had one serving of this granola", "freeform",
                           image_data=_img("nutrition_label.png"))
    print(f"\n[RUNG0 reply] {reply!r}\n[RUNG0 dispatches] {calls}")

    assert _meals(user.id), f"label meal never logged: {reply!r}"
    used_db = [c for c in calls if c in _DB_TOOLS]
    assert not used_db, f"easy label case escalated needlessly: {used_db}"
    total = sum(m.calories or 0 for m in _meals(user.id))
    assert 200 <= total <= 290, f"label (240/serving) not used: {total}"


def test_generic_food_escalates_to_usda_not_dining(db, monkeypatch):
    _enable_all(monkeypatch)
    calls = _record_dispatches(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "had a bowl of plain oatmeal, about a cup cooked",
                           "freeform")
    print(f"\n[GENERIC reply] {reply!r}\n[GENERIC dispatches] {calls}")

    assert _meals(user.id), f"generic meal never logged: {reply!r}"
    assert "usda_food_lookup" in calls, "generic food should reach the USDA rung"
    assert "match_dining_item" not in calls, "non-campus food consulted the dining menu"


def test_campus_item_uses_dining_match(db, monkeypatch):
    _enable_all(monkeypatch)
    calls = _record_dispatches(monkeypatch)
    from models import DiningMenuItem
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    today = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    db.add(DiningMenuItem(scraped_date=today, hall="crossroads", meal_period="dinner",
                          station="entrees", item_name="Roasted Garlic Halal Chicken Rice Bowl",
                          calories=736, protein_g=47.0, carbs_g=71.0, fat_g=23.0,
                          serving_size="1 bowl"))
    db.commit()

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "grabbed the halal chicken bowl at crossroads for dinner",
                           "freeform")
    print(f"\n[CAMPUS reply] {reply!r}\n[CAMPUS dispatches] {calls}")

    assert "match_dining_item" in calls, "campus food skipped the dining rung"
    meals = _meals(user.id)
    assert meals and 700 <= sum(m.calories or 0 for m in meals) <= 780, \
        f"menu macros (736) not used: {reply!r}"


def test_irreducible_portion_ends_in_one_question(db, monkeypatch):
    """No photo, no weight, shared dish: after the data rungs there's nothing left
    but the user. The reply should ask (one short question), not silently guess."""
    _enable_all(monkeypatch)
    calls = _record_dispatches(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "picked at my roommate's pasta bake tonight, no idea "
                                 "how much i actually had", "freeform")
    print(f"\n[ASK reply] {reply!r}\n[ASK dispatches] {calls}")

    assert "?" in reply, f"irreducible portion never asked about: {reply!r}"
    low = reply.lower()
    assert any(w in low for w in ("how much", "how many", "a cup", "plate", "bowl",
                                  "serving", "handful", "roughly", "ballpark")), \
        f"the question isn't about the portion: {reply!r}"


def test_rough_estimate_communicates_uncertainty(db, monkeypatch):
    """Confidence as communication: a no-label plate photo estimate should say it's
    rough and/or invite the cheap correction — not present false precision."""
    _enable_all(monkeypatch)
    _record_dispatches(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "dinner", "freeform", image_data=_img("plate_meal.png"))
    print(f"\n[ROUGH reply] {reply!r}")

    assert _meals(user.id), f"photo meal never logged: {reply!r}"
    low = reply.lower()
    assert any(w in low for w in ("~", "ish", "rough", "about", "around", "roughly",
                                  "lmk", "let me know", "correct me", "off on")), \
        f"estimate presented as precise with no uncertainty communicated: {reply!r}"
