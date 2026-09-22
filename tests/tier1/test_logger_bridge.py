"""
Logger bridge (rewrite/logger-bridge/CHANGESPEC.md) — tier-1, fake SDK, real loop.
The Sep 19 replay is the anchor: a MyNetDiary screenshot of a breakfast the coach
already photo-estimated must REPLACE the estimate (one row, app numbers), never add.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.factories import make_user

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}}


def _enable(monkeypatch):
    import config
    for name in ("SINGLE_AGENT_LOOP_ENABLED", "READ_IMAGE_ENABLED", "MEAL_ESTIMATION_PROMPT_ENABLED",
                 "LOG_MEAL_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED", "REMEMBER_TOOL_ENABLED",
                 "FOOD_LOGGER_BRIDGE_ENABLED", "SET_FOOD_LOGGER_TOOL_ENABLED"):
        monkeypatch.setattr(config, name, True)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local_noonish(user, hour):
    """naive-UTC instant at `hour` local today for this user."""
    from timefmt import resolve_tz, local_day_bounds
    tz = resolve_tz(user)
    start, _ = local_day_bounds(user)
    local = start.replace(tzinfo=timezone.utc).astimezone(tz).replace(hour=hour, minute=0, second=0, microsecond=0)
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).order_by(Meal.id).all()
    finally:
        s.close()


def _user(uid):
    from models import get_session, User
    s = get_session()
    try:
        s.expire_all()
        u = s.get(User, uid)
        s.refresh(u)
        return u
    finally:
        s.close()


# ── normalisation ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("net dairy", "mynetdiary"), ("My Net Diary", "mynetdiary"), ("mfp", "myfitnesspal"),
    ("MyFitnessPal", "myfitnesspal"), ("cronometer", "cronometer"), ("lose it", "loseit"),
    ("some calorie app", "other"), ("none", None), ("", None), (None, None),
])
def test_normalize_app(raw, expected):
    from food_logger import normalize_app
    assert normalize_app(raw) == expected


# ── t1: screenshot logs app-sourced rows and flips the user to coexist ──────

def test_screenshot_turn_logs_app_rows_and_sets_coexist(db, monkeypatch, anthropic_stub):
    from tests._fake_anthropic import ToolUse
    from agent_loop import run_agent_loop
    _enable(monkeypatch)
    user = make_user(db)
    assert user.food_logger_status is None
    calls = []

    def handler(kw):
        calls.append(1)
        if len(calls) == 1:
            return ToolUse("log_meal", {"from_app": "mynetdiary", "slot": "lunch", "items": [
                {"description": "nonfat greek yogurt 170g", "calories": 100, "protein_g": 17},
                {"description": "buttermilk pancakes 2", "calories": 260, "protein_g": 6},
                {"description": "peanut butter 1 tbsp", "calories": 95, "protein_g": 4},
            ]})
        return "got it, 455 for lunch"
    anthropic_stub.reply_with(handler)
    run_agent_loop(_user(user.id), "(the user sent an image)", "freeform", image_data=_IMG)

    rows = _meals(user.id)
    assert len(rows) == 3 and all(r.source == "app" and r.log_type == "app_reported" for r in rows)
    assert sum(r.calories for r in rows) == 455
    u = _user(user.id)
    assert u.calories_today == 455
    assert (u.food_logger, u.food_logger_status) == ("mynetdiary", "coexist") and u.food_logger_since


# ── t2: replace-not-add (the Sep 19 case) ───────────────────────────────────

def test_screenshot_for_an_estimated_slot_replaces_via_manage_log(db, monkeypatch):
    """The Sep 19 case through PR #91's code path (slot refusal + parity) with the
    bridge's side effects: one row, app-sourced, coexist state recorded, parity signal."""
    from agent_tools import handle_log_meal, handle_manage_log
    from models import get_session, Meal, Signal
    _enable(monkeypatch)
    user = make_user(db)
    s = get_session()
    try:
        s.add(Meal(user_id=user.id, description="chicken + egg sandwich", calories=430, protein_g=43,
                   carbs_g=30, fat_g=17, source="photo", log_type="user_reported",
                   eaten_at=_local_noonish(_user(user.id), 8), logged_at=_now()))
        s.commit()
    finally:
        s.close()
    est_id = _meals(user.id)[0].id
    out = handle_log_meal(user.id, {"from_app": "mynetdiary", "slot": "breakfast",
                                    "description": "turkey + egg white sandwich", "calories": 551, "protein_g": 54})
    assert out.startswith("error: breakfast already has") and f"[id {est_id}]" in out, out
    assert "manage_log edit" in out
    assert len(_meals(user.id)) == 1
    out2 = handle_manage_log(user.id, {"action": "edit", "entity": "meal", "id": est_id, "from_app": "mynetdiary",
                                       "fields": {"description": "turkey + egg white sandwich", "calories": 551,
                                                  "protein_g": 54, "carbs_g": 44, "fat_g": 17}})
    assert out2.startswith("ok: edited meal"), out2
    assert "PARITY" in out2 and "+28%" in out2, out2
    rows = _meals(user.id)
    assert len(rows) == 1 and rows[0].source == "app" and rows[0].calories == 551 and rows[0].protein_g == 54
    assert _user(user.id).calories_today == 551
    s = get_session()
    try:
        sig = s.query(Signal).filter(Signal.user_id == user.id, Signal.kind == "parity").one()
        assert sig.payload["cued_cal"] == 430 and sig.payload["app_cal"] == 551 and sig.payload["delta_pct"] == 28
    finally:
        s.close()
    assert _user(user.id).food_logger_status == "coexist"   # the bridge's side effect on the edit path


def test_coexist_side_effect_is_off_without_the_flag(db, monkeypatch):
    from agent_tools import handle_log_meal
    import config
    _enable(monkeypatch)
    monkeypatch.setattr(config, "FOOD_LOGGER_BRIDGE_ENABLED", False)
    user = make_user(db)
    assert handle_log_meal(user.id, {"from_app": "mynetdiary", "slot": "dinner", "description": "eggs", "calories": 310}).startswith("ok")
    assert _user(user.id).food_logger_status is None


# ── t2b: partial macros stay null ───────────────────────────────────────────

def test_calories_only_screenshot_leaves_macros_null(db, monkeypatch):
    from agent_tools import handle_log_meal
    _enable(monkeypatch)
    user = make_user(db)
    out = handle_log_meal(user.id, {"from_app": "myfitnesspal", "slot": "dinner",
                                    "description": "ground turkey bowl", "calories": 615})
    assert out.startswith("ok") and "screenshot" in out
    row = _meals(user.id)[0]
    assert row.calories == 615 and row.protein_g is None and row.carbs_g is None and row.confidence == "high"


# ── t3: the context block ───────────────────────────────────────────────────

def test_context_block_only_for_coexist_with_code_computed_counts(db, monkeypatch):
    from food_logger import context_block, set_food_logger
    from models import get_session, User, Meal
    _enable(monkeypatch)
    user = make_user(db)
    s = get_session()
    try:
        assert context_block(s.get(User, user.id), s) is None
    finally:
        s.close()
    set_food_logger(user.id, "mfp", "coexist")
    s = get_session()
    try:
        u = s.get(User, user.id)
        s.add(Meal(user_id=user.id, description="a", calories=1, source="app", eaten_at=_now() - timedelta(days=1)))
        s.add(Meal(user_id=user.id, description="b", calories=1, source="text", eaten_at=_now() - timedelta(days=2)))
        s.add(Meal(user_id=user.id, description="c", calories=1, source="text", eaten_at=_now() - timedelta(days=1)))
        s.commit()
        blk = context_block(u, s)
    finally:
        s.close()
    assert blk.startswith("## OTHER FOOD LOGGER") and "MyFitnessPal" in blk
    assert "Screenshot days in the last 7: 1" in blk and "cued-only days in the last 7: 1" in blk
    assert "EMPTY day here is NOT an unlogged day" in blk and "Parity:" not in blk
    set_food_logger(user.id, None, "switched")
    s = get_session()
    try:
        assert context_block(s.get(User, user.id), s) is None
    finally:
        s.close()


def test_context_block_reaches_the_loop_and_known_gaps(db, monkeypatch):
    from food_logger import set_food_logger
    from agent_loop import build_loop_context
    from models import get_session, User
    _enable(monkeypatch)
    user = make_user(db)
    set_food_logger(user.id, "mynetdiary", "coexist")
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## OTHER FOOD LOGGER" in ctx and "probably in their other app" in ctx


# ── t5: set_food_logger both branches ──────────────────────────────────────

def test_set_food_logger_tool_both_branches(db, monkeypatch):
    from agent_tools import handle_set_food_logger
    _enable(monkeypatch)
    user = make_user(db)
    out = handle_set_food_logger(user.id, {"app": "my net diary", "status": "coexist"})
    assert out.startswith("ok: coexisting with MyNetDiary"), out
    u = _user(user.id)
    assert (u.food_logger, u.food_logger_status) == ("mynetdiary", "coexist")
    out = handle_set_food_logger(user.id, {"status": "switched"})
    assert out.startswith("ok: they've switched to cued from MyNetDiary"), out
    u = _user(user.id)
    assert u.food_logger_status == "switched"
    prof = u.user_profile_memory or {}
    texts = [e["text"] for c in prof.values() if isinstance(c, list) for e in c]
    assert any("switched to cued from MyNetDiary on 20" in t for t in texts), texts
    assert handle_set_food_logger(user.id, {"status": "coexist"}).startswith("ok: coexisting")  # app remembered
    assert handle_set_food_logger(user.id, {"status": "bogus"}).startswith("error")


def test_tool_is_offered_and_capability_covered(db, monkeypatch):
    import config
    from capabilities import CAPABILITIES
    ids = {c.id for c in CAPABILITIES}
    assert {"app_screenshot_logging", "food_logger_state"} <= ids
    assert any("set_food_logger" in c.tools for c in CAPABILITIES)


# ── t6: parity once-line ────────────────────────────────────────────────────

def _parity_signal(user_id, cued, app):
    from models import get_session, Signal
    s = get_session()
    try:
        s.add(Signal(user_id=user_id, kind="parity", source="cronometer", ts=_now(),
                     payload={"meal_id": 1, "cued_cal": cued, "app_cal": app,
                              "delta_pct": round((app - cued) * 100 / cued)}))
        s.commit()
    finally:
        s.close()


def test_parity_three_close_earns_one_line_then_never_again(db, monkeypatch):
    from food_logger import set_food_logger, context_block, mark_parity_suggested
    from models import get_session, User
    _enable(monkeypatch)
    user = make_user(db)
    set_food_logger(user.id, "cronometer", "coexist")
    for cued, app in ((640, 610), (500, 520), (700, 660)):
        _parity_signal(user.id, cued, app)
    s = get_session()
    try:
        blk = context_block(s.get(User, user.id), s)
    finally:
        s.close()
    assert "Parity: 3 of 3 close" in blk and "drop Cronometer" in blk
    mark_parity_suggested(user.id)
    s = get_session()
    try:
        blk = context_block(s.get(User, user.id), s)
    finally:
        s.close()
    assert "Parity:" not in blk
    # a miss blocks the line
    user2 = make_user(db)
    set_food_logger(user2.id, "cronometer", "coexist")
    for cued, app in ((640, 610), (500, 520), (700, 660), (400, 600)):
        _parity_signal(user2.id, cued, app)
    s = get_session()
    try:
        assert "Parity:" not in context_block(s.get(User, user2.id), s)
    finally:
        s.close()


# ── t7: graduation ──────────────────────────────────────────────────────────

def test_graduation_after_a_clean_fortnight(db, monkeypatch):
    from food_logger import set_food_logger, graduate
    from models import get_session, User, Meal
    _enable(monkeypatch)
    user = make_user(db)
    set_food_logger(user.id, "mfp", "coexist")
    s = get_session()
    try:
        u = s.get(User, user.id)
        u.food_logger_since = _now() - timedelta(days=20)
        for d in range(1, 12):
            s.add(Meal(user_id=user.id, description="m", calories=500, source="text",
                       eaten_at=_now() - timedelta(days=d, hours=2)))
        s.commit()
    finally:
        s.close()
    r = graduate(user.id)
    assert r["status"] == "graduated" and r["cued_days"] >= 10
    assert _user(user.id).food_logger_status == "switched"
    # too few cued days → holding; an app meal in the window → holding; too recent → too_recent
    user2 = make_user(db)
    set_food_logger(user2.id, "mfp", "coexist")
    s = get_session()
    try:
        s.get(User, user2.id).food_logger_since = _now() - timedelta(days=20)
        for d in range(1, 6):
            s.add(Meal(user_id=user2.id, description="m", calories=500, source="text", eaten_at=_now() - timedelta(days=d)))
        s.commit()
    finally:
        s.close()
    assert graduate(user2.id)["status"] == "holding"
    user3 = make_user(db)
    set_food_logger(user3.id, "mfp", "coexist")
    assert graduate(user3.id)["status"] == "too_recent"


# ── t8: adaptive explain names the screenshot fix ──────────────────────────

def test_adaptive_no_change_explain_for_coexist(db, monkeypatch):
    from adaptive_targets import todays_adjustment_context
    from food_logger import set_food_logger
    from models import get_session, User, TargetAdjustment
    _enable(monkeypatch)
    user = make_user(db, calorie_target=1700)
    set_food_logger(user.id, "mynetdiary", "coexist")
    s = get_session()
    try:
        s.add(TargetAdjustment(user_id=user.id, at=_now(), old_target=1700, new_target=1700, changed=False,
                               reason="only 3 of the last 14 days had 2+ meals logged (need 10)"))
        s.commit()
        ctx = todays_adjustment_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "screenshot of each day" in ctx


# ── heartbeat inherits the block ───────────────────────────────────────────

def test_heartbeat_context_carries_the_block(db, monkeypatch):
    from food_logger import set_food_logger
    from heartbeat import _proactive_context
    from models import get_session, User
    _enable(monkeypatch)
    user = make_user(db)
    set_food_logger(user.id, "loseit", "coexist")
    s = get_session()
    try:
        ctx = _proactive_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## OTHER FOOD LOGGER" in ctx and "Lose It" in ctx
