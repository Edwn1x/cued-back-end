"""save_menu: persist a user-sent menu (dining hall / frat house / meal prep) so
"I ate the Wednesday burrito" logs from saved macros instead of being read once and
lost. Covers the pure store logic, the tool handler, TTL/staleness, caps, and the
build_loop_context injection that surfaces it to the coach."""
from __future__ import annotations

import copy
from datetime import datetime, timezone, timedelta

import pytest

import config
import saved_menus
from tests.factories import make_user

MENU = [
    {"item": "burrito", "calories": 720, "protein_g": 34, "note": "ground beef, rice, beans"},
    {"item": "chicken bowl", "calories": 650, "protein_g": 45},
    {"item": "side salad", "protein_g": 8},          # partial macros OK
    {"name": "not-an-item-key"},                      # 'name' alias → kept
    {"calories": 100},                                # no item name → dropped
]


# ─── pure store logic ─────────────────────────────────────────────────────────

def test_apply_menu_cleans_items_and_keys_by_normalized_name():
    m = saved_menus.apply_menu(None, "  Frat  House Lunch ", MENU)
    assert list(m.keys()) == ["frat house lunch"]
    items = m["frat house lunch"]["items"]
    assert [i["item"] for i in items] == ["burrito", "chicken bowl", "side salad", "not-an-item-key"]
    assert items[0]["calories"] == 720 and items[0]["note"].startswith("ground beef")
    assert "calories" not in items[2]  # side salad had none → omitted, not invented


def test_resend_replaces_same_menu_not_appends():
    m = saved_menus.apply_menu(None, "frat lunch", [{"item": "burrito", "calories": 700}])
    m = saved_menus.apply_menu(m, "Frat Lunch", [{"item": "burrito", "calories": 720, "protein_g": 34}])
    assert len(m) == 1
    assert m["frat lunch"]["items"][0]["calories"] == 720


def test_empty_or_unusable_items_do_not_store():
    assert saved_menus.apply_menu(None, "x", []) == {}
    assert saved_menus.apply_menu(None, "x", [{"calories": 5}]) == {}  # no item name anywhere


def test_prunes_to_the_most_recent_max(monkeypatch):
    monkeypatch.setattr(config, "SAVED_MENU_MAX", 3)
    m = {}
    for i in range(5):
        m = saved_menus.apply_menu(m, f"menu {i}", [{"item": f"dish {i}"}])
    assert len(m) == 3
    # the 3 most recent kept (menu 2,3,4)
    assert set(m.keys()) == {"menu 2", "menu 3", "menu 4"}


def test_item_cap(monkeypatch):
    monkeypatch.setattr(config, "SAVED_MENU_MAX_ITEMS", 2)
    m = saved_menus.apply_menu(None, "big", [{"item": f"d{i}"} for i in range(10)])
    assert len(m["big"]["items"]) == 2


# ─── render / staleness ───────────────────────────────────────────────────────

def test_render_fresh_menu_block():
    m = saved_menus.apply_menu(None, "frat house lunch", MENU)
    blk = saved_menus.render_menus_block(m)
    assert "SAVED MENUS" in blk
    assert "burrito — 720 cal, 34g protein (ground beef, rice, beans)" in blk
    assert "side salad — 8g protein" in blk


def test_stale_menu_is_not_rendered(monkeypatch):
    monkeypatch.setattr(config, "SAVED_MENU_TTL_DAYS", 14)
    m = saved_menus.apply_menu(None, "old", MENU)
    m["old"]["captured_at"] = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=20)).isoformat()
    assert saved_menus.render_menus_block(m) == ""
    assert saved_menus.render_menus_block(None) == ""


# ─── tool handler + DB ────────────────────────────────────────────────────────

def test_handle_save_menu_persists_and_errors(db):
    from agent_tools import handle_save_menu
    from models import get_session, User
    user = make_user(db)
    out = handle_save_menu(user.id, {"name": "frat house lunch", "items": MENU})
    assert out.startswith("ok:") and "4 items" in out
    s = get_session()
    try:
        saved = s.get(User, user.id).saved_menus
        assert "frat house lunch" in saved and len(saved["frat house lunch"]["items"]) == 4
    finally:
        s.close()
    # guards
    assert handle_save_menu(user.id, {"name": "", "items": MENU}).startswith("error")
    assert handle_save_menu(user.id, {"name": "x", "items": []}).startswith("error")
    assert handle_save_menu(user.id, {"name": "x", "items": [{"calories": 1}]}).startswith("error")


# ─── it reaches the coach: context injection + tool wiring ─────────────────────

def test_saved_menu_appears_in_loop_context(db, monkeypatch):
    monkeypatch.setattr(config, "SAVE_MENU_TOOL_ENABLED", True)
    from agent_tools import handle_save_menu
    from agent_loop import build_loop_context
    from models import get_session, User
    user = make_user(db)
    handle_save_menu(user.id, {"name": "frat house lunch", "items": MENU})
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "SAVED MENUS" in ctx and "burrito — 720 cal, 34g protein" in ctx


def test_save_menu_tool_is_wired():
    import agent_tools
    assert agent_tools._HANDLERS.get("save_menu") is agent_tools.handle_save_menu
    assert agent_tools.SAVE_MENU_TOOL["name"] == "save_menu"
    # items became optional on the wire (a confirm-by-reference names a staged menu); the
    # handler still refuses a first save with no items
    assert agent_tools.SAVE_MENU_TOOL["input_schema"]["required"] == ["name"]


# ─── a menu the COACH found waits on their yes (2026-11-02, Grizzly Chefs) ─────
#
# The public page showed lunch + dinner; Alexey's house gets breakfast + dinner. A menu
# the coach finds itself goes through the same code-enforced confirm gate the calendar
# writes use; the staged proposal carries the items, so the confirming call only names it.
# A menu THEY sent still saves on the spot. A menu that names its week ages out with it.

WEB = "https://images.squarespace-cdn.com/x/Fall+11%3A2%3A2026+Menu+.png"
DINNERS = [{"item": "Mon dinner: Bruschetta Chicken", "note": "bacon mashed potatoes"},
           {"item": "Tue dinner: Taco Bar", "note": "mexican rice, corn tortillas"}]


def _menus(user_id):
    from models import get_session, User
    s = get_session()
    try:
        return dict(s.get(User, user_id).saved_menus or {})
    finally:
        s.close()


def test_week_of_parses_to_monday_and_the_menu_ages_out_with_its_week(monkeypatch):
    assert saved_menus.parse_week_of("2026-11-04") == "2026-11-02"      # a Wednesday → that Monday
    assert saved_menus.parse_week_of("2026-11-02T10:00:00") == "2026-11-02"
    assert saved_menus.parse_week_of("nov 2") is None and saved_menus.parse_week_of(None) is None
    today = datetime.now(timezone.utc).date()
    this_monday = (today - timedelta(days=today.weekday())).isoformat()
    last_monday = (today - timedelta(days=today.weekday() + 7)).isoformat()
    m = saved_menus.apply_menu(None, "house menu", DINNERS, week_of=last_monday, source_url=WEB)
    assert m["house menu"]["week_of"] == last_monday and m["house menu"]["source_url"] == WEB
    assert saved_menus.render_menus_block(m) == ""                       # captured today, but its week is over
    m = saved_menus.apply_menu(None, "house menu", DINNERS, week_of=this_monday, source_url=WEB)
    blk = saved_menus.render_menus_block(m)
    assert "week of" in blk and "from their menu page" in blk and "Taco Bar" in blk


def test_found_menu_is_held_until_their_yes_and_confirms_by_reference(db):
    from agent_tools import handle_save_menu, _pending_list
    user = make_user(db)
    first = handle_save_menu(user.id, {"name": "house menu", "items": DINNERS, "source_url": WEB,
                                       "week_of": "2026-11-02"}, message_id="m1")
    assert first.startswith("not saved yet") and "confirm with the user first" in first
    assert "2 items" in first and "week of Nov 2" in first and "send_image" in first
    assert "am/pm" not in first
    assert _menus(user.id) == {}
    staged = _pending_list(user.id)
    assert len(staged) == 1 and staged[0]["kind"] == "save_menu" and staged[0]["n"] == 2
    # confirmed in the SAME turn → refused (they haven't seen it)
    same = handle_save_menu(user.id, {"name": "house menu", "source_url": WEB, "confirmed": True}, message_id="m1")
    assert same.startswith("not saved") and "SAME turn" in same and _menus(user.id) == {}
    # next turn, their yes: the model names it (no items) and the staged payload is what's saved
    ok = handle_save_menu(user.id, {"name": "house menu", "source_url": WEB, "confirmed": True}, message_id="m2")
    assert ok.startswith("ok: saved 'house menu' (week of Nov 2) with 2 items")
    saved = _menus(user.id)["house menu"]
    assert [i["item"] for i in saved["items"]] == [i["item"] for i in DINNERS]
    assert saved["source_url"] == WEB and saved["week_of"] == "2026-11-02"
    assert _pending_list(user.id) == []                                  # the entry is cleared after the write


def test_confirming_a_menu_that_was_never_staged_or_whose_url_changed_is_refused(db):
    from agent_tools import handle_save_menu
    user = make_user(db)
    out = handle_save_menu(user.id, {"name": "house menu", "items": DINNERS, "source_url": WEB, "confirmed": True},
                           message_id="m1")
    assert out.startswith("not saved") and "nothing was staged" in out and _menus(user.id) == {}
    # a confirm that names a DIFFERENT picture than the one staged is a new proposal, not a write
    out = handle_save_menu(user.id, {"name": "house menu", "items": DINNERS, "source_url": WEB + "?other",
                                     "confirmed": True}, message_id="m2")
    assert out.startswith("not saved") and _menus(user.id) == {}
    # and confirming by name alone with nothing staged can't invent items
    out = handle_save_menu(user.id, {"name": "ghost", "source_url": WEB, "confirmed": True}, message_id="m3")
    assert out.startswith("error:") and "nothing staged" in out


def test_a_menu_they_sent_still_saves_on_the_spot_and_the_flag_turns_the_gate_off(db, monkeypatch):
    from agent_tools import handle_save_menu
    user = make_user(db)
    out = handle_save_menu(user.id, {"name": "frat house lunch", "items": MENU, "week_of": "2026-11-03"}, message_id="m1")
    assert out.startswith("ok: saved 'frat house lunch' (week of Nov 2) with 4 items")
    assert _menus(user.id)["frat house lunch"]["week_of"] == "2026-11-02"
    monkeypatch.setattr(config, "SAVE_MENU_CONFIRM_WEB_ENABLED", False)
    out = handle_save_menu(user.id, {"name": "house menu", "items": DINNERS, "source_url": WEB}, message_id="m1")
    assert out.startswith("ok:") and _menus(user.id)["house menu"]["source_url"] == WEB


def test_a_staged_menu_and_a_staged_calendar_event_wait_side_by_side(db, monkeypatch):
    from agent_tools import handle_save_menu, _pending_list, _set_pending_calendar_event
    user = make_user(db)
    _set_pending_calendar_event(user.id, {"kind": "create", "summary": "gym", "start_utc": "2026-11-03T16:00:00",
                                          "staged_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                                          "turn": "m0", "reflect": "gym tue 8am"})
    out = handle_save_menu(user.id, {"name": "house menu", "items": DINNERS, "source_url": WEB}, message_id="m1")
    assert "ALSO waiting on their yes: gym tue 8am" in out
    kinds = sorted(p["kind"] for p in _pending_list(user.id))
    assert kinds == ["create", "save_menu"]
    handle_save_menu(user.id, {"name": "house menu", "source_url": WEB, "confirmed": True}, message_id="m2")
    assert [p["kind"] for p in _pending_list(user.id)] == ["create"]    # only ITS entry is cleared


def test_tool_schema_and_prompt_carry_the_found_menu_rule():
    import agent_tools
    props = agent_tools.SAVE_MENU_TOOL["input_schema"]["properties"]
    assert {"source_url", "week_of", "confirmed"} <= set(props)
    assert agent_tools.SAVE_MENU_TOOL["input_schema"]["required"] == ["name"]
    assert "A menu YOU found" in agent_tools.SAVE_MENU_TOOL["description"]
    voice = open("prompts/voice.md").read()
    assert "A menu YOU found" in voice and "never save a found menu silently" in voice
