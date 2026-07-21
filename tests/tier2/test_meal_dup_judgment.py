"""
Tier-2 (live) — note #2's core claim: deterministic visibility, model judgment.
With today's meals injected into context + the log_meal read-before-write tool,
the real model should SKIP a duplicate re-mention but LOG a genuine second serving.
Both branches asserted. Run: pytest --run-tier2 -s.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.tier2


def _naive_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _active_meal_count(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        return active(s, Meal, user_id=user_id).count()
    finally:
        s.close()


def _seed_meal(user_id, desc, cal, protein):
    from models import get_session, Meal
    s = get_session()
    try:
        s.add(Meal(user_id=user_id, description=desc, calories=cal, protein_g=protein,
                   source="text", log_type="user_reported", eaten_at=_naive_now()))
        s.commit()
    finally:
        s.close()


def test_duplicate_re_mention_is_not_double_logged(db, monkeypatch):
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)
    user = make_user(db)
    _seed_meal(user.id, "chicken burrito bowl from chipotle", 700, 50)
    before = _active_meal_count(user.id)

    reply = run_agent_loop(user, "ngl that chipotle bowl from earlier really hit", "freeform")
    after = _active_meal_count(user.id)
    print(f"\n[BRANCH A duplicate] reply: {reply}\n  meals {before} -> {after}")
    assert after == before, "model re-logged a meal already in today's context"


def test_genuine_second_serving_is_logged(db, monkeypatch):
    import config
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    monkeypatch.setattr(config, "LOG_MEAL_TOOL_ENABLED", True)
    user = make_user(db)
    _seed_meal(user.id, "protein shake", 300, 30)
    before = _active_meal_count(user.id)

    reply = run_agent_loop(user, "just made my second protein shake of the day, another 300 cal 30g",
                           "freeform")
    after = _active_meal_count(user.id)
    print(f"\n[BRANCH B second serving] reply: {reply}\n  meals {before} -> {after}")
    assert after == before + 1, "model dropped a legitimate second serving (the worse bug)"
