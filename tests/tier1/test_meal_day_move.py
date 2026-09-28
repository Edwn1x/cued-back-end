"""
Meal day-move + meal grouping (2026-09-27, PR meal-edit-fixes). Two real incidents:

  1. A move blocked on a photo turn. "photo + 'add this and switch the eggs from last night
     to yesterday's log'" — the #120 photo-reread delete guard blocked the DELETE half of a
     move because the message carried an image and the regex only knew delete words, not
     move/switch. Result: a temporary DOUBLE-LOG. Fix: (a) a day-move is a single `date`
     edit on the existing row (no add-new-then-delete-old), and (b) the guard recognizes an
     explicit move/switch directive and never blocks it.

  2. No "meal" grouping. Items logged together were independent rows, so "move the eggs
     MEAL to yesterday" moved only the eggs. Fix: one log_meal batch shares a meal_group_id;
     manage_log scope='meal' moves/deletes the whole group atomically.

The guard's REAL purpose (block a SILENT photo re-read delete with no intent) must survive.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config


# ── helpers ──────────────────────────────────────────────────────────────────

def _log(user_id, tool_input):
    from agent_tools import handle_log_meal
    return handle_log_meal(user_id, tool_input)


def _one_meal(user_id, desc="2 eggs", cal=140):
    return _log(user_id, {"description": desc, "calories": cal,
                          "protein_g": 12, "carbs_g": 1, "fat_g": 10})


def _local_date(dt, tz_str="America/Los_Angeles"):
    return dt.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_str)).date()


def _photo_turn(user_id, caption):
    from agent_tools import begin_turn, peek_turn_state
    begin_turn(user_id)
    st = peek_turn_state(user_id)
    st["has_image"] = True
    st["caption"] = caption


# ── 1. an explicit move on a PHOTO turn: not blocked, not double-logged ────────

def test_move_meal_to_yesterday_is_a_single_date_edit_no_double_log(db, monkeypatch):
    """The core fix: moving a meal to another day re-dates the EXISTING row — one op, one
    row. No add-new-then-delete-old, so the count never goes to two."""
    monkeypatch.setattr(config, "MEAL_DAY_MOVE_ENABLED", True)
    monkeypatch.setattr(config, "PHOTO_REREAD_DELETE_GUARD_ENABLED", True)
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, active

    user = make_user(db)
    _one_meal(user.id)
    s = get_session()
    try:
        m = active(s, Meal, user_id=user.id).one()
        mid, was_today = m.id, _local_date(m.eaten_at)
    finally:
        s.close()
    assert was_today == datetime.now(ZoneInfo("America/Los_Angeles")).date()

    # A photo turn (image present) with an explicit move directive in the caption.
    _photo_turn(user.id, "add this and switch the eggs from last night to yesterday's log")
    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                      "fields": {"date": "yesterday"}})
    assert out.startswith("ok:"), out

    s = get_session()
    try:
        rows = active(s, Meal, user_id=user.id).all()
        assert len(rows) == 1, f"a move must not create a second row (double-log): {rows}"
        moved = rows[0]
        assert moved.id == mid, "the move edits the EXISTING row, not a new one"
        yest = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)
        assert _local_date(moved.eaten_at) == yest, "eaten_at's local date moved to yesterday"
        assert any(e.get("field") == "date" for e in (moved.edits or [])), "the move is audited"
    finally:
        s.close()


def test_explicit_move_directive_not_blocked_by_photo_guard(db, monkeypatch):
    """Belt-and-suspenders: even a DELETE on a photo turn is allowed when the caption is an
    explicit move/switch — a directive is never a silent re-read. Guard broadening (1b)."""
    monkeypatch.setattr(config, "MEAL_DAY_MOVE_ENABLED", True)
    monkeypatch.setattr(config, "PHOTO_REREAD_DELETE_GUARD_ENABLED", True)
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, active

    user = make_user(db)
    _one_meal(user.id)
    s = get_session()
    try:
        mid = active(s, Meal, user_id=user.id).one().id
    finally:
        s.close()

    _photo_turn(user.id, "move that to yesterday's log")
    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": mid})
    assert out.startswith("ok:"), out  # not blocked — move intent present


# ── 2. move/delete the whole meal (grouping) ──────────────────────────────────

def test_move_the_eggs_meal_moves_all_items_in_the_batch(db, monkeypatch):
    """Items logged in one log_meal call are one meal. scope='meal' moves ALL of them in a
    single op — not just the eggs, and no 'what about the rice' second step."""
    monkeypatch.setattr(config, "MEAL_DAY_MOVE_ENABLED", True)
    monkeypatch.setattr(config, "MEAL_GROUP_ENABLED", True)
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, active

    user = make_user(db)
    _log(user.id, {"items": [
        {"description": "2 eggs", "calories": 140, "protein_g": 12, "carbs_g": 1, "fat_g": 10},
        {"description": "rice", "calories": 200, "protein_g": 4, "carbs_g": 44, "fat_g": 1},
        {"description": "avocado", "calories": 120, "protein_g": 1, "carbs_g": 6, "fat_g": 11},
    ]})
    s = get_session()
    try:
        rows = active(s, Meal, user_id=user.id).all()
        assert len({r.meal_group_id for r in rows}) == 1, "one batch → one shared group id"
        assert all(r.meal_group_id for r in rows), "every batched item gets a group id"
        eggs = next(r for r in rows if "egg" in r.description).id
    finally:
        s.close()

    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": eggs,
                                      "scope": "meal", "fields": {"date": "yesterday"}})
    assert out.startswith("ok:"), out
    assert "3 items" in out, out

    s = get_session()
    try:
        rows = active(s, Meal, user_id=user.id).all()
        assert len(rows) == 3, "still three rows — a move never deletes/duplicates"
        yest = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)
        assert all(_local_date(r.eaten_at) == yest for r in rows), \
            "ALL items moved to yesterday, not just the eggs"
    finally:
        s.close()


def test_delete_the_whole_meal_group_is_atomic(db, monkeypatch):
    monkeypatch.setattr(config, "MEAL_GROUP_ENABLED", True)
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, active

    user = make_user(db)
    _log(user.id, {"items": [
        {"description": "2 eggs", "calories": 140, "protein_g": 12, "carbs_g": 1, "fat_g": 10},
        {"description": "toast", "calories": 90, "protein_g": 3, "carbs_g": 16, "fat_g": 1},
    ]})
    s = get_session()
    try:
        anchor = active(s, Meal, user_id=user.id).all()[0].id
    finally:
        s.close()

    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": anchor,
                                      "scope": "meal"})
    assert out.startswith("ok:") and "2 items" in out, out
    s = get_session()
    try:
        assert active(s, Meal, user_id=user.id).count() == 0, "the whole meal is gone"
    finally:
        s.close()


def test_group_edit_rejects_non_date_fields(db, monkeypatch):
    """scope='meal' is for move/delete only — a macro/description edit across a group would
    clobber distinct foods, so it's steered back to item scope."""
    monkeypatch.setattr(config, "MEAL_GROUP_ENABLED", True)
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, active

    user = make_user(db)
    _log(user.id, {"items": [
        {"description": "2 eggs", "calories": 140, "protein_g": 12, "carbs_g": 1, "fat_g": 10},
        {"description": "rice", "calories": 200, "protein_g": 4, "carbs_g": 44, "fat_g": 1},
    ]})
    s = get_session()
    try:
        mid = active(s, Meal, user_id=user.id).all()[0].id
    finally:
        s.close()

    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": mid,
                                      "scope": "meal", "fields": {"calories": 999}})
    assert out.startswith("error:") and "scope='item'" in out, out


def test_item_scope_moves_only_one_of_a_group(db, monkeypatch):
    """Default (item) scope moves just the one row even when it belongs to a group — a
    genuine single-item correction still works."""
    monkeypatch.setattr(config, "MEAL_DAY_MOVE_ENABLED", True)
    monkeypatch.setattr(config, "MEAL_GROUP_ENABLED", True)
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, active

    user = make_user(db)
    _log(user.id, {"items": [
        {"description": "2 eggs", "calories": 140, "protein_g": 12, "carbs_g": 1, "fat_g": 10},
        {"description": "rice", "calories": 200, "protein_g": 4, "carbs_g": 44, "fat_g": 1},
    ]})
    s = get_session()
    try:
        eggs = next(r for r in active(s, Meal, user_id=user.id).all() if "egg" in r.description).id
    finally:
        s.close()

    out = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": eggs,
                                      "fields": {"date": "yesterday"}})  # scope defaults to item
    assert out.startswith("ok:"), out
    s = get_session()
    try:
        yest = datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)
        today = datetime.now(ZoneInfo("America/Los_Angeles")).date()
        by_desc = {r.description: _local_date(r.eaten_at) for r in active(s, Meal, user_id=user.id).all()}
        assert by_desc["2 eggs"] == yest and by_desc["rice"] == today, \
            f"item scope moves only the eggs: {by_desc}"
    finally:
        s.close()


# ── 3. the guard's real purpose survives ──────────────────────────────────────

def test_silent_photo_reread_delete_still_blocked(db, monkeypatch):
    """#120 regression: a photo turn with NO delete AND NO move intent must still be blocked
    from deleting a confirmed meal (the yogurt-photo-deletes-the-banana case)."""
    monkeypatch.setattr(config, "MEAL_DAY_MOVE_ENABLED", True)
    monkeypatch.setattr(config, "PHOTO_REREAD_DELETE_GUARD_ENABLED", True)
    from tests.factories import make_user
    from agent_tools import handle_manage_log
    from models import get_session, Meal, active

    user = make_user(db)
    _one_meal(user.id, desc="2 bananas", cal=210)
    s = get_session()
    try:
        mid = active(s, Meal, user_id=user.id).one().id
    finally:
        s.close()

    _photo_turn(user.id, "yogurt")  # a new food, no delete AND no move words
    out = handle_manage_log(user.id, {"action": "delete", "entity": "meal", "id": mid})
    assert out.startswith("error:"), out
    assert "SEPARATE entry" in out or "ADDS food" in out
    s = get_session()
    try:
        assert active(s, Meal, user_id=user.id).count() == 1, "the confirmed meal must NOT be deleted"
    finally:
        s.close()


def test_move_intent_regex_covers_the_incident_phrasings():
    from agent_tools import _MOVE_INTENT_RE
    for p in ("switch the eggs from last night to yesterday's log", "move that to yesterday",
              "reassign the rice to today", "put the eggs on yesterday", "change it to friday",
              "log those for yesterday", "count that as yesterday"):
        assert _MOVE_INTENT_RE.search(p), p
    for n in ("yogurt", "here is my lunch", "chicken and rice", "add this to my log"):
        assert not _MOVE_INTENT_RE.search(n), n
