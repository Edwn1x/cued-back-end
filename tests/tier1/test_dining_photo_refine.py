"""
Dining-photo refine (PR: dining-photo-refine). Live 2026-09-28: founder sent a
Crossroads plate PHOTO + "From crossroads"; the coach eyeballed all items and did NOT
match the scraped menu (mexican rice logged 210 cal vs the menu's 120). #120's
named-dining refine only fired on TEXT follow-ups, not the photo turn.

This pins the code-side refine: after log_meal writes the eyeballed rows, when the turn
names a scraped hall, each row is matched against that hall's LATEST scrape and a
CONFIDENT match replaces the macros/label — while a weak/ambiguous match KEEPS the
eyeball (precision over recall, from the live nit). Flag-gated, fails open.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _la_today(days_ago=0):
    return (datetime.now(ZoneInfo("America/Los_Angeles")) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _add_item(db, item_name, *, hall="crossroads", meal_period="lunch", days_ago=0,
              calories=120, protein_g=2.1, carbs_g=25.0, fat_g=1.0, serving_size="1/2 cup"):
    from models import DiningMenuItem
    row = DiningMenuItem(scraped_date=_la_today(days_ago), hall=hall,
                         meal_period=meal_period, station="grains",
                         item_name=item_name, calories=calories, protein_g=protein_g,
                         carbs_g=carbs_g, fat_g=fat_g, serving_size=serving_size)
    db.add(row)
    db.commit()
    return row


def _log_with_caption(user_id, caption, items, has_image=True):
    """Simulate a photo/log turn: begin_turn, stamp the caption + has_image the way
    agent_loop does, then run the tool."""
    import agent_tools
    agent_tools.begin_turn(user_id)
    st = agent_tools.peek_turn_state(user_id)
    st["has_image"] = has_image
    st["caption"] = caption
    return agent_tools.handle_log_meal(user_id, {"items": items})


def _meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).order_by(Meal.id).all()
    finally:
        s.close()


def test_photo_from_crossroads_refines_mexican_rice_to_menu(db, monkeypatch):
    """The headline case: photo + 'From crossroads', mexican rice eyeballed at 210 →
    replaced with the menu's 120 (macros + label), day total reflects the refine."""
    import config
    from tests.factories import make_user
    from models import get_session, User
    monkeypatch.setattr(config, "DINING_PHOTO_REFINE_ENABLED", True)

    _add_item(db, "Mexican Rice", calories=120, protein_g=2.1, carbs_g=25.0, fat_g=1.0)
    user = make_user(db)

    out = _log_with_caption(user.id, "From crossroads", [
        {"description": "mexican rice", "calories": 210, "protein_g": 4, "carbs_g": 40, "fat_g": 5},
    ])
    assert out.startswith("ok"), out
    assert "menu-matched to crossroads" in out, out

    meals = _meals(user.id)
    assert len(meals) == 1
    m = meals[0]
    # Meal.protein_g is an integer column, so the menu's 2.1 stores as 2 (was eyeball 4).
    assert m.calories == 120 and m.protein_g == 2, "macros not taken from the menu"
    assert m.description == "Mexican Rice", "menu dish name (item ID) not adopted"
    assert "crossroads menu match" in (m.notes or "")

    s = get_session()
    try:
        assert s.get(User, user.id).calories_today == 120, "day total didn't reflect the refine"
    finally:
        s.close()


def test_weak_match_keeps_eyeball(db, monkeypatch):
    """The live nit: 'roasted carrots + sweet potato' must NOT silently become
    'Carrot Sticks 39' (nor 'Potato Wedges 252'). A weak match keeps the estimate."""
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "DINING_PHOTO_REFINE_ENABLED", True)

    _add_item(db, "Carrot Sticks", calories=39, protein_g=1.0, carbs_g=9.0, fat_g=0.0)
    _add_item(db, "Potato Wedges", calories=252, protein_g=4.0, carbs_g=34.0, fat_g=11.0)
    user = make_user(db)

    _log_with_caption(user.id, "from crossroads", [
        {"description": "roasted carrots and sweet potato", "calories": 150,
         "protein_g": 3, "carbs_g": 30, "fat_g": 3},
    ])
    m = _meals(user.id)[0]
    assert m.calories == 150, "a weak match wrongly overwrote the eyeball"
    assert m.description == "roasted carrots and sweet potato"
    assert "menu match" not in (m.notes or "")


def test_single_generic_word_does_not_force_a_specific_dish(db, monkeypatch):
    """A bare 'chicken' is fully CONTAINED in 'Roasted Garlic Halal Chicken Rice Bowl'
    (one-way containment 1.0) but is a weak identification — the refine must not adopt a
    specific bowl's macros for a generic word."""
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "DINING_PHOTO_REFINE_ENABLED", True)

    _add_item(db, "Roasted Garlic Halal Chicken Rice Bowl",
              calories=720, protein_g=42.0, carbs_g=68.0, fat_g=24.0)
    user = make_user(db)

    _log_with_caption(user.id, "from crossroads", [
        {"description": "chicken", "calories": 300, "protein_g": 35, "carbs_g": 0, "fat_g": 8},
    ])
    m = _meals(user.id)[0]
    assert m.calories == 300 and m.description == "chicken", "generic word forced a specific dish"


def test_ambiguous_two_dishes_keeps_eyeball(db, monkeypatch):
    """Two differently-named strong candidates → we can't be sure which they ate; keep
    the estimate rather than pick one."""
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "DINING_PHOTO_REFINE_ENABLED", True)

    _add_item(db, "Chicken Fried Rice", calories=430, protein_g=18.0, carbs_g=55.0, fat_g=14.0)
    _add_item(db, "Chicken Fried Noodles", calories=520, protein_g=20.0, carbs_g=60.0, fat_g=18.0)
    user = make_user(db)

    _log_with_caption(user.id, "from crossroads", [
        {"description": "chicken fried something", "calories": 400, "protein_g": 15, "carbs_g": 50, "fat_g": 12},
    ])
    m = _meals(user.id)[0]
    assert m.calories == 400, "ambiguous match wrongly overwrote the eyeball"


def test_no_hall_named_no_refine(db, monkeypatch):
    """No hall on the turn → the plate is not known to be dining-hall food; eyeball stands
    even when a same-named menu row exists."""
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "DINING_PHOTO_REFINE_ENABLED", True)

    _add_item(db, "Mexican Rice", calories=120)
    user = make_user(db)

    out = _log_with_caption(user.id, "just had some rice", [
        {"description": "mexican rice", "calories": 210, "protein_g": 4, "carbs_g": 40, "fat_g": 5},
    ])
    assert "menu-matched" not in out
    assert _meals(user.id)[0].calories == 210, "refined without a hall named"


def test_no_menu_data_no_refine(db, monkeypatch):
    """Hall named but nothing scraped today (closed / scraper gap) → eyeball stands."""
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "DINING_PHOTO_REFINE_ENABLED", True)
    # No dining rows added at all.
    user = make_user(db)

    out = _log_with_caption(user.id, "from crossroads", [
        {"description": "mexican rice", "calories": 210, "protein_g": 4, "carbs_g": 40, "fat_g": 5},
    ])
    assert "menu-matched" not in out
    assert _meals(user.id)[0].calories == 210, "refined with no menu data"


def test_flag_off_is_inert(db, monkeypatch):
    """Flag off → today's eyeball behavior, no refine even on a perfect match."""
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "DINING_PHOTO_REFINE_ENABLED", False)

    _add_item(db, "Mexican Rice", calories=120)
    user = make_user(db)

    out = _log_with_caption(user.id, "from crossroads", [
        {"description": "mexican rice", "calories": 210, "protein_g": 4, "carbs_g": 40, "fat_g": 5},
    ])
    assert "menu-matched" not in out
    assert _meals(user.id)[0].calories == 210, "refine ran with the flag off"
