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
    assert agent_tools.SAVE_MENU_TOOL["input_schema"]["required"] == ["name", "items"]
