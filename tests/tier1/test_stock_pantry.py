"""
stock_pantry — food on hand, NOT eaten, from the coach's tool (live 2026-10-05, user 48:
a NY strip + raspberries package were read, estimated, "lmk when u eat it" — and written
NOWHERE; the later log depended on chat history alone). Rows carry the estimate per 100 g,
the PANTRY block shows an "(… if eaten)" figure, and the existing manage_log delete path
takes the row off when they eat it.
"""
from __future__ import annotations

import pytest

from tests.factories import make_user


@pytest.fixture
def pantry_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "RECEIPTS_ENABLED", True)
    monkeypatch.setattr(config, "STOCK_PANTRY_TOOL_ENABLED", True)


def _rows(uid):
    from models import get_session, PantryItem
    s = get_session()
    try:
        return s.query(PantryItem).filter_by(user_id=uid).order_by(PantryItem.id).all()
    finally:
        s.close()


STEAK = {"label": "NY strip steak", "est_grams": 358, "qty": 0.79, "unit": "lb", "calories": 750, "protein_g": 80}
BERRIES = {"label": "raspberries", "est_grams": 170, "qty": 6, "unit": "oz", "calories": 110, "protein_g": 1}


def test_tool_writes_rows_with_the_estimate_per_100g(db, pantry_on):
    from agent_tools import handle_stock_pantry, _TURN_STATE
    u = make_user(db)
    _TURN_STATE[u.id] = {"has_image": True}
    out = handle_stock_pantry(u.id, {"items": [STEAK, BERRIES]})
    assert out.startswith("ok: on hand, not eaten — NY strip steak, raspberries") and "don't say it's logged" in out
    rows = _rows(u.id)
    assert [(r.label, r.source, r.est_grams) for r in rows] == [("NY strip steak", "photo", 358.0), ("raspberries", "photo", 170.0)]
    steak = rows[0]
    assert steak.kcal_per_100g == pytest.approx(209.5, abs=0.1) and steak.protein_per_100g == pytest.approx(22.3, abs=0.1)
    assert steak.depleted_at is None


def test_text_turns_are_marked_text_and_bad_items_are_skipped(db, pantry_on):
    from agent_tools import handle_stock_pantry, _TURN_STATE
    u = make_user(db)
    _TURN_STATE.pop(u.id, None)
    out = handle_stock_pantry(u.id, {"items": [{"label": "eggs", "qty": 12, "unit": "each"}, {"est_grams": "x"}, {"label": "milk", "est_grams": "lots"}]})
    assert out.startswith("ok: on hand, not eaten — eggs") and "skipped: ?, milk" in out
    rows = _rows(u.id)
    assert len(rows) == 1 and rows[0].source == "text" and rows[0].kcal_per_100g is None
    assert handle_stock_pantry(u.id, {"items": []}).startswith("error")
    assert handle_stock_pantry(u.id, {"items": [{"qty": 1}]}).startswith("error")


def test_restocking_the_same_label_merges_instead_of_duplicating(db, pantry_on):
    from receipts import stock_items
    u = make_user(db)
    stock_items(u.id, [STEAK], source="photo")
    stock_items(u.id, [dict(STEAK, est_grams=400, calories=840)], source="photo")
    rows = _rows(u.id)
    assert len(rows) == 1 and rows[0].est_grams == 758.0 and rows[0].kcal_per_100g == pytest.approx(210.0, abs=0.1)


def test_pantry_block_shows_the_if_eaten_figure(db, pantry_on):
    from receipts import stock_items
    from agent_loop import build_loop_context
    from models import get_session, User
    u = make_user(db)
    stock_items(u.id, [STEAK, BERRIES, {"label": "tortillas", "qty": 10, "unit": "each"}], source="photo")
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, u.id), s)
    finally:
        s.close()
    assert "## PANTRY (what they have at home" in ctx
    assert "NY strip steak (~0.79 lb) (~750 cal, 80g protein if eaten)" in ctx, ctx
    assert "raspberries (~6 oz) (~110 cal, 1g protein if eaten)" in ctx
    assert "tortillas (10)" in ctx and "tortillas (10) (" not in ctx     # no estimate → no suffix
    assert "the number to log from when they ate the whole thing" in ctx


def test_eating_it_later_takes_the_row_off_through_manage_log(db, pantry_on):
    from receipts import stock_items
    from agent_tools import handle_manage_log
    u = make_user(db)
    stock_items(u.id, [STEAK], source="photo")
    out = handle_manage_log(u.id, {"entity": "pantry", "action": "delete", "label": "steak"})
    assert out.startswith("ok: took 1 pantry item") and "log_meal" in out
    assert _rows(u.id)[0].depleted_at is not None


def test_tool_is_offered_only_with_both_flags():
    """The loop's tool list is built inline in run_agent_loop; the registration is gated on
    both flags (the PANTRY block that makes the row useful is RECEIPTS_ENABLED-gated)."""
    import inspect, agent_loop
    src = inspect.getsource(agent_loop.run_agent_loop)
    assert "if config.STOCK_PANTRY_TOOL_ENABLED and config.RECEIPTS_ENABLED:" in src
    assert "tools.append(STOCK_PANTRY_TOOL)" in src
    from agent_tools import _HANDLERS, STOCK_PANTRY_TOOL
    assert _HANDLERS["stock_pantry"].__name__ == "handle_stock_pantry" and STOCK_PANTRY_TOOL["name"] == "stock_pantry"


def test_registry_and_voice_know_the_tool():
    from capabilities import CAPABILITIES
    from agent_loop import _VOICE_PATH
    assert "stock_pantry" in {t for c in CAPABILITIES for t in c.tools}
    raw = open(_VOICE_PATH, encoding="utf-8").read()
    assert "Call **stock_pantry** instead" in raw and "with **remember** instead" not in raw
