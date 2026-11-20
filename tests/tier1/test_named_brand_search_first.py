"""
Live 2026-11-20 12:13 PT, user 51: "Gonna eat an insomnia cc cookie" → the coach wrote
"insomnia classic chocolate chunk cookie — estimate" at 370 cal / 4g / 50c / 18f without a
search; the posted number is 250 / 3 / 34 / 12. It knew the brand and the item and still
guessed: prompts/meal_routing.md (on in prod, cached right after voice) put the web rung
BELOW "identifiable generic food" with "only when nothing above covers it" — a named cookie
is always covered by a generic cookie — while voice.md's ladder said the opposite. Over the
prior 60 days 99 logged rows named a brand or venue; 3 carried a source.

Fix, two layers: the routing ladder's named-brand rung moves above generic food (search
BEFORE any estimate); and the log tool's RESULT carries the affordance when a brand-named
row arrives with no search this turn and no source in its text (named_brands.py) — the
surface that held when description-level rules did not (macro-accuracy lesson).
"""
from __future__ import annotations

import logging

import config
from tests.factories import make_user


# ── named_brands.py ───────────────────────────────────────────────────────────

def test_names_brand_finds_chains_venues_and_packaged_goods():
    from named_brands import names_brand
    assert names_brand("insomnia classic chocolate chunk cookie — estimate") == "insomnia cookies"
    assert names_brand("Gonna eat an insomnia cc cookie") == "insomnia cookies"
    assert names_brand("Chipotle chicken bowl — double rice, pinto beans") == "chipotle"
    assert names_brand("in-n-out double double") == "in-n-out"
    assert names_brand("In N Out fries, ~half order") == "in-n-out"
    assert names_brand("TJ's avocado mash, half the container") == "trader joe's"
    assert names_brand("denny's maple filled donut holes x2") == "denny's"
    assert names_brand("Denny's scrambled eggs, 6") == "denny's"
    assert names_brand("premier protein shake, caffe latte (11oz)") == "premier protein"
    assert names_brand("turkey avocado panini, Free Speech Cafe (Moffitt)") == "free speech movement cafe"
    assert names_brand("kirkland summit roast k-cup, black") == "costco"
    assert names_brand("hotboys oakland jumbo fried tender") == "hotboys"
    assert names_brand("El Pollo Loco double chicken tostada salad") == "el pollo loco"
    assert names_brand("dunkin' iced coffee") == "dunkin"


def test_names_brand_leaves_generic_food_dining_halls_and_lookalikes_alone():
    from named_brands import names_brand
    assert names_brand("chicken and rice") is None
    assert names_brand("a chocolate chip cookie from the bake sale") is None
    assert names_brand("overnight oats w/ strawberries") is None
    # a scraped dining hall has its own tool (match_dining_item) — never a brand nudge,
    # even when the plate name carries a brand-like word ("chipotle" the seasoning)
    assert names_brand("crossroads halal chipotle chicken ~2 servings") is None
    assert names_brand("cafe 3 pasta bolognese") is None
    # word boundaries: substrings and the ambiguous short words stay quiet
    assert names_brand("insomniac snack at 2am") is None
    assert names_brand("primed the pan, then 2 eggs") is None
    assert names_brand("monster of a burrito") is None
    assert names_brand("a quest for protein: 2 eggs") is None
    assert names_brand("") is None and names_brand(None) is None


def test_extra_brands_come_from_the_env(monkeypatch):
    import named_brands
    monkeypatch.setenv("NAMED_BRANDS_EXTRA", "berkeley bowl, saul's")
    named_brands._COMPILED = None
    try:
        assert named_brands.names_brand("saul's pastrami sandwich") == "saul's"
        assert named_brands.names_brand("berkeley bowl hot bar plate") == "berkeley bowl"
    finally:
        named_brands._COMPILED = None


def test_has_source_tag():
    from named_brands import has_source_tag
    assert has_source_tag("insomnia classic chocolate chunk cookie (official nutrition)")
    assert has_source_tag("insomnia classic chocolate chunk cookie (official)")
    assert has_source_tag("chobani greek yogurt cup, 150g (label)")
    assert has_source_tag("tj mango pico ~1/2 cup (8 tbsp, label: 10 cal per 2 tbsp)")
    assert has_source_tag("crossroads beef burger (menu)")
    assert not has_source_tag("insomnia classic chocolate chunk cookie — estimate")
    assert not has_source_tag("chipotle chicken bowl")


# ── the routing ladder ────────────────────────────────────────────────────────

def test_routing_ladder_puts_the_named_brand_rung_above_generic_food():
    from agent_loop import _meal_routing_prompt
    text = _meal_routing_prompt()
    assert "kind of information" in text   # the marker tests/tier1/test_meal_routing_prompt.py keys on
    dining = text.index("match_dining_item")
    brand = text.index("A NAMED brand, chain, restaurant or packaged product")
    usda = text.index("usda_food_lookup")
    ask = text.index("Ask the user")
    assert dining < brand < usda < ask, "named brand must sit above generic food, below the dining tool"
    assert "only when nothing above covers it" not in text
    assert "BEFORE you write any estimate" in text
    assert "does not cover a named one" in text
    assert "names a brand" in text   # the cross-ladder rule that reads the log_meal nudge


# ── the log tool's result ─────────────────────────────────────────────────────

def _log(user_id, desc, **kw):
    from agent_tools import handle_log_meal
    item = {"description": desc, "calories": 370, "protein_g": 4, "carbs_g": 50, "fat_g": 18}
    item.update(kw)
    return handle_log_meal(user_id, item)


def test_brand_row_with_no_search_gets_the_nudge_in_the_tool_result(db, caplog):
    from agent_tools import begin_turn
    user = make_user(db)
    begin_turn(user.id)
    caplog.set_level(logging.INFO)
    out = _log(user.id, "insomnia classic chocolate chunk cookie — estimate")
    assert out.startswith("ok: logged 'insomnia classic chocolate chunk cookie — estimate' id=")
    assert "names a brand (insomnia cookies) and no search ran this turn" in out
    assert "web_search the brand's OFFICIAL nutrition" in out
    assert "manage_log edit id" in out and "'(official)'" in out
    assert f"LOG_MEAL_BRAND_UNSOURCED user={user.id}" in caplog.text
    assert "brand='insomnia cookies'" in caplog.text


def test_a_search_this_turn_means_the_row_is_sourced_and_gets_no_nudge(db, caplog):
    from agent_tools import begin_turn, peek_turn_state
    user = make_user(db)
    begin_turn(user.id)
    peek_turn_state(user.id)["web_search_used"] = True
    caplog.set_level(logging.INFO)
    out = _log(user.id, "insomnia classic chocolate chunk cookie", calories=250, protein_g=3, carbs_g=34, fat_g=12)
    assert "names a brand" not in out
    assert f"LOG_MEAL_BRAND_SOURCED user={user.id}" in caplog.text and "searched=True" in caplog.text
    assert "LOG_MEAL_BRAND_UNSOURCED" not in caplog.text


def test_a_source_in_the_description_means_no_nudge(db, caplog):
    from agent_tools import begin_turn
    user = make_user(db)
    begin_turn(user.id)
    caplog.set_level(logging.INFO)
    out = _log(user.id, "insomnia classic chocolate chunk cookie (official nutrition)", calories=250, protein_g=3, carbs_g=34, fat_g=12)
    assert "names a brand" not in out
    assert "LOG_MEAL_BRAND_SOURCED" in caplog.text and "searched=False" in caplog.text
    begin_turn(user.id)
    out = _log(user.id, "chobani greek yogurt cup, 150g (label)", calories=110, protein_g=12, carbs_g=6, fat_g=0)
    assert "names a brand" not in out


def test_generic_food_and_flag_off_get_no_nudge(db, monkeypatch, caplog):
    from agent_tools import begin_turn
    user = make_user(db)
    begin_turn(user.id)
    caplog.set_level(logging.INFO)
    out = _log(user.id, "chicken and rice", calories=600, protein_g=45, carbs_g=60, fat_g=15)
    assert "names a brand" not in out and "LOG_MEAL_BRAND" not in caplog.text
    monkeypatch.setattr(config, "LOG_MEAL_BRAND_HINT_ENABLED", False)
    begin_turn(user.id)
    out = _log(user.id, "chipotle chicken bowl", calories=900, protein_g=50, carbs_g=100, fat_g=30)
    assert "names a brand" not in out and "LOG_MEAL_BRAND" not in caplog.text


def test_only_the_brand_item_in_a_multi_item_plate_is_flagged(db):
    from agent_tools import begin_turn, handle_log_meal
    user = make_user(db)
    begin_turn(user.id)
    out = handle_log_meal(user.id, {"items": [
        {"description": "chipotle chicken bowl", "calories": 900, "protein_g": 50, "carbs_g": 100, "fat_g": 30},
        {"description": "an apple", "calories": 95, "protein_g": 0, "carbs_g": 25, "fat_g": 0},
    ]})
    assert out.startswith("ok: logged 2 items")
    assert out.count("names a brand") == 1
    assert "'chipotle chicken bowl' [id " in out and "names a brand (chipotle)" in out
    assert "'an apple' [id" not in out


def test_the_other_affordances_still_ride_the_same_result(db):
    """The brand nudge is one more segment of the tail — portions-guessed stays."""
    from agent_tools import begin_turn
    user = make_user(db)
    begin_turn(user.id)
    out = _log(user.id, "chipotle chicken bowl, ~1.5 scoops rice", calories=900, protein_g=50, carbs_g=100, fat_g=30,
               portion_guessed=True)
    assert "portions guessed:" in out and "names a brand (chipotle)" in out


# ── the loop marks a searched turn ────────────────────────────────────────────

def test_agent_loop_marks_the_turn_when_the_model_searched(db, anthropic_stub, monkeypatch):
    from tests._fake_anthropic import WebSearchUse
    from agent_loop import run_agent_loop
    import agent_tools
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    user = make_user(db)
    anthropic_stub.reply_with(lambda kw: WebSearchUse("insomnia cookies classic chocolate chunk nutrition",
                                                      text="250 cal, 3g protein — logged"))
    run_agent_loop(user, "gonna eat an insomnia cc cookie", "freeform")
    assert agent_tools._TURN_STATE.get(user.id, {}).get("web_search_used") is True


def test_a_turn_without_a_search_is_not_marked(db, anthropic_stub):
    from agent_loop import run_agent_loop
    import agent_tools
    user = make_user(db)
    anthropic_stub.reply_with(lambda kw: "noted")
    run_agent_loop(user, "hey", "freeform")
    assert not agent_tools._TURN_STATE.get(user.id, {}).get("web_search_used")


def test_end_to_end_the_nudge_reaches_the_model_on_the_next_call(db, anthropic_stub, monkeypatch):
    """The live shape: the model goes straight to log_meal with a guess. The tool result it
    reads next carries the nudge (search now, edit this id)."""
    from tests._fake_anthropic import ToolUse
    from agent_loop import run_agent_loop
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)
    user = make_user(db)
    seen = {}

    def handler(kw):
        last = kw["messages"][-1]
        if (last["role"] == "user" and isinstance(last["content"], list) and last["content"]
                and last["content"][0].get("type") == "tool_result"):
            seen["result"] = last["content"][0]["content"]
            return "logged the cookie, ~370 cal (estimate)"
        return ToolUse("log_meal", {"description": "insomnia classic chocolate chunk cookie — estimate",
                                    "calories": 370, "protein_g": 4, "carbs_g": 50, "fat_g": 18})

    anthropic_stub.reply_with(handler)
    run_agent_loop(user, "gonna eat an insomnia cc cookie", "freeform")
    assert "names a brand (insomnia cookies) and no search ran this turn" in seen["result"], seen
    assert "manage_log edit id" in seen["result"]


def test_end_to_end_a_search_before_the_log_means_no_nudge(db, anthropic_stub, monkeypatch):
    """The wanted shape after the ladder change: search first, then log — the result is quiet."""
    from tests._fake_anthropic import _Response, _ToolUseBlock, _Block
    from agent_loop import run_agent_loop
    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)
    monkeypatch.setattr(config, "WEB_SEARCH_TOOL_ENABLED", True)
    user = make_user(db)
    seen = {}

    def handler(kw):
        last = kw["messages"][-1]
        if (last["role"] == "user" and isinstance(last["content"], list) and last["content"]
                and last["content"][0].get("type") == "tool_result"):
            seen["result"] = last["content"][0]["content"]
            return "logged the cookie at 250, their posted number"
        # one response: the server-side search blocks (already executed), then the log_meal call
        r = _Response("")
        srv = _ToolUseBlock("web_search", {"query": "insomnia cookies classic chocolate chunk nutrition"}, id="srvtoolu_1")
        srv.type = "server_tool_use"
        r.content = [srv, _Block("", type="web_search_tool_result"),
                     _ToolUseBlock("log_meal", {"description": "insomnia classic chocolate chunk cookie (official)",
                                                "calories": 250, "protein_g": 3, "carbs_g": 34, "fat_g": 12})]
        r.stop_reason = "tool_use"
        return r

    anthropic_stub.reply_with(handler)
    run_agent_loop(user, "gonna eat an insomnia cc cookie", "freeform")
    assert "result" in seen and "names a brand" not in seen["result"], seen
