"""
Tier-2 (live) — macro-accuracy Phase C: the dining-menu lookup in the model's hands.

Tier-1 proved the matcher; this checks the MODEL looks campus food up instead of
eyeballing: "just had the halal chicken bowl at crossroads" must log menu-derived
macros, not a generic estimate.

Run: pytest --run-tier2 -s tests/tier2/test_dining_match.py
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

pytestmark = pytest.mark.tier2


def _enable(monkeypatch):
    import config
    for f in ("LOG_MEAL_TOOL_ENABLED", "DINING_MATCH_TOOL_ENABLED",
              "MEAL_HISTORY_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)


def test_campus_meal_logs_menu_macros_not_a_guess(db, monkeypatch):
    """Menu truth: 736 cal / 47g protein — numbers a generic 'chicken bowl' guess
    would not land on. The logged meal must reflect the scraped values."""
    _enable(monkeypatch)
    from models import DiningMenuItem, get_session, Meal, active
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    today = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")
    db.add(DiningMenuItem(scraped_date=today, hall="crossroads", meal_period="lunch",
                          station="entrees", item_name="Roasted Garlic Halal Chicken Rice Bowl",
                          calories=736, protein_g=47.0, carbs_g=71.0, fat_g=23.0,
                          serving_size="1 bowl"))
    db.commit()

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    reply = run_agent_loop(user, "just had the halal chicken bowl at crossroads", "freeform")
    print(f"\n[DINING reply] {reply!r}")

    s = get_session()
    try:
        meals = active(s, Meal, user_id=user.id).all()
        for m in meals:
            print(f"[DINING logged] {m.description!r} {m.calories}cal/{m.protein_g}g")
        total_cal = sum(m.calories or 0 for m in meals)
        total_pro = sum(m.protein_g or 0 for m in meals)
    finally:
        s.close()

    assert meals, f"campus meal never logged: {reply!r}"
    # Tight bands around the menu truth: the signature that the lookup (not a
    # generic estimate) produced the numbers.
    assert 700 <= total_cal <= 780, \
        f"menu macros not used (736 expected): logged {total_cal}: {reply!r}"
    assert 42 <= total_pro <= 52, \
        f"menu protein not used (47 expected): logged {total_pro}: {reply!r}"
