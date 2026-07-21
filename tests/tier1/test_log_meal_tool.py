"""
Phase 3 — log_meal (note #2 vehicle). The model does the read-before-write
judgment (today's meals are injected into its context); this handler writes the
meal and records saw_similar so an intentional near-duplicate is auditable.
"""

from __future__ import annotations


def _active_meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).all()
    finally:
        s.close()


def test_log_meal_creates_and_recomputes(db):
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    from models import get_session, User

    user = make_user(db)
    out = handle_log_meal(user.id, {"description": "chicken bowl", "calories": 600, "protein_g": 45})
    assert out.startswith("ok"), out

    s = get_session()
    try:
        u = s.query(User).get(user.id)
    finally:
        s.close()
    assert u.calories_today == 600 and u.protein_today == 45
    assert len(_active_meals(user.id)) == 1


def test_log_meal_legit_second_serving_records_saw_similar(db):
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    from models import get_session, User

    user = make_user(db)
    handle_log_meal(user.id, {"description": "protein shake", "calories": 300, "protein_g": 30})
    first_id = _active_meals(user.id)[0].id

    # model saw the first shake and judged this a distinct second serving
    out = handle_log_meal(user.id, {"description": "protein shake", "calories": 300,
                                    "protein_g": 30, "saw_similar": [first_id]})
    assert "saw_similar" in out, out

    meals = _active_meals(user.id)
    assert len(meals) == 2, "a legitimate second serving must not be silently dropped"
    s = get_session()
    try:
        assert s.query(User).get(user.id).calories_today == 600  # both counted
    finally:
        s.close()
    assert any("saw_similar" in (m.notes or "") for m in meals), "saw_similar not recorded for audit"


def test_log_meal_via_loop(db, driver, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)

    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        loop_calls.append(1)
        if len(loop_calls) == 1:
            return ToolUse("log_meal", {"description": "chipotle burrito bowl",
                                        "calories": 700, "protein_g": 50})
        return "logged, that's 700 cal / 50g — solid lunch"

    anthropic_stub.reply_with(handler)
    user = make_user(db)
    driver.send(user, "just had a chipotle burrito bowl")
    assert len(_active_meals(user.id)) == 1, "log_meal tool did not persist the meal"


def test_log_meal_batch_items_recomputes_once(db):
    """A multi-item plate via `items` → one Meal each, totals recomputed once."""
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    from models import get_session, User

    user = make_user(db)
    out = handle_log_meal(user.id, {"items": [
        {"description": "chicken", "calories": 400, "protein_g": 40},
        {"description": "rice", "calories": 200, "protein_g": 5},
        {"description": "coke", "calories": 140, "protein_g": 0},
    ]})
    assert out.startswith("ok") and "3 items" in out, out
    assert len(_active_meals(user.id)) == 3
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert u.calories_today == 740 and u.protein_today == 45  # summed once from all three
    finally:
        s.close()
