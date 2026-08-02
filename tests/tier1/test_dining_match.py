"""
Macro-accuracy Phase C — dining-hall menu match: campus food is looked up, not
estimated. Deterministic matcher over today's scraped DiningMenuItem rows
(containment scoring — menu names are verbose, user phrasing compressed), surfaced
as the read-only match_dining_item tool. Tier-1 pins the spec cases: a Crossroads
item matches with menu macros, a non-dining meal doesn't spuriously match, and
absent/stale data falls through cleanly (halls really do close — scraper returns
0 items). Whether the model prefers the lookup to eyeballing is tier-2.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def _la_today(days_ago=0):
    return (datetime.now(ZoneInfo("America/Los_Angeles")) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _add_item(db, item_name, *, hall="crossroads", meal_period="lunch", days_ago=0,
              calories=720, protein_g=42.0, carbs_g=68.0, fat_g=24.0,
              serving_size="1 bowl"):
    from models import DiningMenuItem
    row = DiningMenuItem(scraped_date=_la_today(days_ago), hall=hall,
                         meal_period=meal_period, station="entrees",
                         item_name=item_name, calories=calories, protein_g=protein_g,
                         carbs_g=carbs_g, fat_g=fat_g, serving_size=serving_size)
    db.add(row)
    db.commit()
    return row


def test_compressed_phrasing_matches_verbose_menu_item(db):
    from dining_scraper import match_dining_items

    _add_item(db, "Roasted Garlic Halal Chicken Rice Bowl")
    matches, had_data = match_dining_items("halal chicken bowl", hall="crossroads")

    assert had_data
    assert matches, "user's compressed phrasing must find the verbose menu item"
    top = matches[0]
    assert top.item_name == "Roasted Garlic Halal Chicken Rice Bowl"
    assert top.calories == 720 and top.protein_g == 42.0


def test_non_dining_meal_does_not_spuriously_match(db):
    from dining_scraper import match_dining_items

    _add_item(db, "Roasted Garlic Halal Chicken Rice Bowl")
    matches, had_data = match_dining_items("homemade lamb curry", hall="crossroads")

    assert had_data
    assert matches == [], "unrelated food matched a menu item (too-loose failure)"


def test_absent_or_stale_data_falls_through_cleanly(db):
    """Halls close / scraper gaps: yesterday's rows must NOT serve as today's menu,
    and the caller can tell 'no data' from 'no match'."""
    from dining_scraper import match_dining_items

    matches, had_data = match_dining_items("halal chicken bowl")
    assert matches == [] and had_data is False

    _add_item(db, "Roasted Garlic Halal Chicken Rice Bowl", days_ago=1)  # stale
    matches, had_data = match_dining_items("halal chicken bowl")
    assert matches == [] and had_data is False, "stale scraped_date leaked into today"


def test_hall_filters_hard_period_prefers(db):
    """Hall is a HARD filter (halls have different menus — cross-hall macros are
    wrong data). meal_period is only a RANKING preference: the model guesses the
    period from time-of-day and a wrong guess must not hide the item."""
    from dining_scraper import match_dining_items

    _add_item(db, "Halal Chicken Rice Bowl", hall="crossroads", meal_period="lunch")
    _add_item(db, "Halal Chicken Rice Bowl", hall="foothill", meal_period="dinner",
              calories=800)

    matches, _ = match_dining_items("halal chicken bowl", hall="foothill")
    assert len(matches) == 1 and matches[0].calories == 800

    # period preference: both rows match; the named period ranks first
    matches, _ = match_dining_items("halal chicken bowl", meal_period="lunch")
    assert len(matches) == 2 and matches[0].calories == 720

    # hall aliases canonicalize ("Clark Kerr Campus" style inputs)
    matches, _ = match_dining_items("halal chicken bowl", hall="Foothill")
    assert len(matches) == 1 and matches[0].calories == 800


def test_wrong_period_guess_does_not_mask_data(db):
    """Live run 1 bug: at dinner time the model passed meal_period='dinner' for an
    item scraped under 'lunch'; the period-as-filter design returned had_data=False
    ('hall closed') and the coach fell back to a guess. The period must never turn
    real data into a no-data answer."""
    from dining_scraper import match_dining_items

    _add_item(db, "Roasted Garlic Halal Chicken Rice Bowl", meal_period="lunch")

    matches, had_data = match_dining_items("halal chicken bowl", hall="crossroads",
                                           meal_period="dinner")
    assert had_data is True
    assert matches and matches[0].calories == 720, \
        "wrong period guess hid a real menu match"


def test_ranking_prefers_fuller_match_among_distractors(db):
    from dining_scraper import match_dining_items

    _add_item(db, "Chicken Caesar Wrap", calories=610)
    _add_item(db, "Halal Chicken Rice Bowl", calories=720)
    _add_item(db, "BBQ Chicken Pizza", calories=550)

    matches, _ = match_dining_items("halal chicken rice bowl")
    assert matches and matches[0].item_name == "Halal Chicken Rice Bowl"


# ---- tool surface -----------------------------------------------------------

def test_tool_handler_match_and_both_fallthrough_branches(db):
    from agent_tools import dispatch_tool
    from tests.factories import make_user

    user = make_user(db)

    out = dispatch_tool("match_dining_item", {"description": "halal chicken bowl"}, user.id)
    assert "no menu data" in out, "absent-data branch must be a clean tool answer"

    _add_item(db, "Roasted Garlic Halal Chicken Rice Bowl")
    out = dispatch_tool("match_dining_item",
                        {"description": "halal chicken bowl", "hall": "crossroads"}, user.id)
    assert out.startswith("ok:"), out
    assert "720" in out and "42" in out and "crossroads" in out

    out = dispatch_tool("match_dining_item", {"description": "homemade lamb curry"}, user.id)
    assert "no menu match" in out, "no-match branch must be a clean tool answer"


def test_loop_offers_tool_iff_flag_on(db, monkeypatch, anthropic_stub):
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    captured = {}

    def handler(kw):
        captured["tools"] = [t.get("name") for t in kw.get("tools", [])]
        return "sounds good"

    anthropic_stub.reply_with(handler)
    user = make_user(db)

    monkeypatch.setattr(config, "DINING_MATCH_TOOL_ENABLED", True)
    run_agent_loop(user, "lunch at crossroads", "freeform")
    assert "match_dining_item" in captured["tools"]

    monkeypatch.setattr(config, "DINING_MATCH_TOOL_ENABLED", False)
    run_agent_loop(user, "lunch at crossroads", "freeform")
    assert "match_dining_item" not in captured["tools"]
