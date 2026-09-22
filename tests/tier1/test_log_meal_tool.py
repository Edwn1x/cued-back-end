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
    out = handle_log_meal(user.id, {"description": "chicken bowl", "calories": 600, "protein_g": 45, "carbs_g": 0, "fat_g": 0})
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
    handle_log_meal(user.id, {"description": "protein shake", "calories": 300, "protein_g": 30, "carbs_g": 0, "fat_g": 0})
    first_id = _active_meals(user.id)[0].id

    # model saw the first shake and judged this a distinct second serving
    out = handle_log_meal(user.id, {"description": "protein shake", "calories": 300,
                                    "protein_g": 30, "carbs_g": 0, "fat_g": 0, "saw_similar": [first_id]})
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
                                        "calories": 700, "protein_g": 50, "carbs_g": 0, "fat_g": 0})
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
        {"description": "chicken", "calories": 400, "protein_g": 40, "carbs_g": 0, "fat_g": 0},
        {"description": "rice", "calories": 200, "protein_g": 5, "carbs_g": 0, "fat_g": 0},
        {"description": "coke", "calories": 140, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
    ]})
    assert out.startswith("ok") and "3 items" in out, out
    assert len(_active_meals(user.id)) == 3
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert u.calories_today == 740 and u.protein_today == 45  # summed once from all three
    finally:
        s.close()


def test_log_meal_return_names_the_item(db):
    """The tool result must NAME what was logged (not just macros) so the coach's
    confirmation can say 'logged the chicken wrap, ~650 cal' — founder feedback
    2026-09-18: a macros-only confirmation can't be verified without asking."""
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    user = make_user(db)
    out = handle_log_meal(user.id, {"description": "chicken caesar wrap", "calories": 650, "protein_g": 38, "carbs_g": 0, "fat_g": 0})
    assert "chicken caesar wrap" in out, out
    assert "650cal" in out

    # batch form names each item too
    out2 = handle_log_meal(user.id, {"items": [
        {"description": "banana", "calories": 100, "protein_g": 1, "carbs_g": 0, "fat_g": 0},
        {"description": "greek yogurt", "calories": 150, "protein_g": 15, "carbs_g": 0, "fat_g": 0},
    ]})
    assert "banana" in out2 and "greek yogurt" in out2, out2


def test_log_meal_returns_fresh_day_total(db):
    """The tool result must hand back the recomputed DAY TOTAL NOW so the coach quotes
    it instead of hand-adding to the (turn-start-stale) totals block — the 2026-09-19
    protein-drift fix. A running sequence must reflect the cumulative total."""
    from tests.factories import make_user
    from agent_tools import handle_log_meal
    user = make_user(db, protein_target=140)
    out1 = handle_log_meal(user.id, {"description": "eggs", "calories": 300, "protein_g": 20, "carbs_g": 0, "fat_g": 0})
    assert "DAY TOTAL NOW: 300 cal, 20g protein" in out1, out1
    out2 = handle_log_meal(user.id, {"description": "chicken", "calories": 500, "protein_g": 45, "carbs_g": 0, "fat_g": 0})
    assert "DAY TOTAL NOW: 800 cal, 65g protein" in out2, out2   # cumulative, not just this meal
    assert "75g protein left of 140" in out2, out2


def test_manage_log_edit_meal_returns_fresh_day_total(db):
    from tests.factories import make_user
    from agent_tools import handle_log_meal, handle_manage_log
    user = make_user(db)
    out = handle_log_meal(user.id, {"description": "bowl", "calories": 600, "protein_g": 45, "carbs_g": 0, "fat_g": 0})
    mid = int(out.split("id=")[1].split(" ")[0])
    edited = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                         "fields": {"calories": 400, "protein_g": 30, "carbs_g": 0, "fat_g": 0}})
    assert "DAY TOTAL NOW: 400 cal, 30g protein" in edited, edited
