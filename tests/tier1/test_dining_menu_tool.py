"""
Phase 3 tool 4 — get_dining_menu. On-demand read of today's scraped hall menu
(replaces always-on context injection).
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def _today_pacific():
    return datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")


def _seed_menu(hall="crossroads", period="lunch"):
    from models import get_session, DiningMenuItem
    s = get_session()
    try:
        today = _today_pacific()
        s.add(DiningMenuItem(scraped_date=today, hall=hall, meal_period=period,
                             item_name="grilled chicken breast", calories=200, protein_g=38.0))
        s.add(DiningMenuItem(scraped_date=today, hall=hall, meal_period=period,
                             item_name="brown rice", calories=220, protein_g=5.0))
        s.commit()
    finally:
        s.close()


def test_get_dining_menu_returns_todays_items(db):
    from tests.factories import make_user
    from agent_tools import handle_get_dining_menu

    _seed_menu()
    user = make_user(db)
    out = handle_get_dining_menu(user.id, {"hall": "crossroads", "meal_period": "lunch"})
    assert out.startswith("ok"), out
    assert "grilled chicken breast" in out and "38g protein" in out


def test_get_dining_menu_empty_is_error(db):
    from tests.factories import make_user
    from agent_tools import handle_get_dining_menu

    user = make_user(db)
    out = handle_get_dining_menu(user.id, {"hall": "foothill"})
    assert out.startswith("error")


def test_get_dining_menu_via_loop(db, driver, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "GET_DINING_MENU_TOOL_ENABLED", True)
    _seed_menu()

    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        loop_calls.append(1)
        if len(loop_calls) == 1:
            return ToolUse("get_dining_menu", {"hall": "crossroads", "meal_period": "lunch"})
        return "grab the grilled chicken — 200cal/38g, best protein on the line"

    anthropic_stub.reply_with(handler)
    user = make_user(db, which_gym="rsf")
    replies = driver.send(user, "what's good at crossroads for lunch")
    assert len(replies) >= 1
