"""
Vision-thoroughness Fix 1, deterministic half (rewrite/vision-thoroughness).

The model judges what's eaten vs. merely visible; CODE stores each in the right
place. This pins the storage seam through the real loop on an image turn: items
the model designates seen-but-not-eaten land in food_on_hand (the TTL-aged,
non-immortal category — never constraints), and never touch the meal log or the
eaten totals. Green regression guard on plumbing that exists (PR #24); whether
the model actually sweeps the frame is tier-2.
"""

from __future__ import annotations

_IMG = {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QQ=="}}


def _enable(monkeypatch):
    import config
    for name in ("SINGLE_AGENT_LOOP_ENABLED", "READ_IMAGE_ENABLED",
                 "MEAL_ESTIMATION_PROMPT_ENABLED", "LOG_MEAL_TOOL_ENABLED",
                 "REMEMBER_TOOL_ENABLED"):
        monkeypatch.setattr(config, name, True)


def test_seen_not_eaten_routes_to_food_on_hand_never_meals_or_totals(db, monkeypatch,
                                                                     anthropic_stub):
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import get_session, User, Meal, active

    _enable(monkeypatch)
    user = make_user(db)

    steps = []

    def handler(kw):
        steps.append(1)
        if len(steps) == 1:
            return ToolUse("log_meal", {"description": "scrambled eggs ~3 eggs",
                                        "calories": 320, "protein_g": 24})
        if len(steps) == 2:
            return ToolUse("remember", {
                "action": "add", "category": "food_on_hand",
                "text": "has strawberry jam, egg whites carton, and a loaf of bread at home",
            })
        return "eggs logged — and I see you've got jam and bread there too"

    anthropic_stub.reply_with(handler)
    reply = run_agent_loop(user, "breakfast", "freeform", image_data=_IMG)
    assert "logged" in reply

    s = get_session()
    try:
        meals = active(s, Meal, user_id=user.id).all()
        u = s.query(User).filter(User.id == user.id).one()
        profile = dict(u.user_profile_memory or {})
        # eaten → exactly one meal, and totals reflect ONLY it (no inflation)
        assert len(meals) == 1
        assert "eggs" in meals[0].description
        assert u.calories_today == 320 and u.protein_today == 24
    finally:
        s.close()

    # seen-but-not-eaten → food_on_hand, and nowhere near constraints
    on_hand = profile.get("food_on_hand") or []
    assert any("jam" in e.get("text", "") for e in on_hand), \
        f"on-hand capture missing from food_on_hand: {profile!r}"
    assert not any("jam" in e.get("text", "") for e in (profile.get("constraints") or [])), \
        "visible groceries must never land in constraints"

    # non-immortal: the entry dies under the TTL sweep like any grocery fact
    from memory import expire_stale_entries
    for e in on_hand:
        e["ts"] = "2020-01-01T00:00:00+00:00"
    expire_stale_entries(profile, user_id=user.id)
    assert not any("jam" in e.get("text", "") for e in (profile.get("food_on_hand") or [])), \
        "food_on_hand entry from an image turn must age out like any grocery fact"
