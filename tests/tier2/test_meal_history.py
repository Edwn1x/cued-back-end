"""
Tier-2 (live) — macro-accuracy Phase B: the user-history prior in the model's hands.

Tier-1 proved the matcher; these check the MODEL consults it and stays honest:
a repeat meal's estimate reflects the user's own prior (and says so), a novel meal
never claims a history it lacks.

Run: pytest --run-tier2 -s tests/tier2/test_meal_history.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.tier2


def _enable(monkeypatch):
    import config
    for f in ("LOG_MEAL_TOOL_ENABLED", "MEAL_HISTORY_TOOL_ENABLED",
              "MANAGE_LOG_TOOL_ENABLED", "REMEMBER_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)


def _seed_meal(db, user_id, description, calories, protein_g, days_ago):
    from models import Meal
    when = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    db.add(Meal(user_id=user_id, description=description, calories=calories,
                protein_g=protein_g, eaten_at=when, source="text",
                log_type="user_reported"))
    db.commit()


def _todays_meals(user_id):
    from models import get_session, Meal, active
    s = get_session()
    try:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        return [m for m in active(s, Meal, user_id=user_id).all() if m.eaten_at >= cutoff]
    finally:
        s.close()


def test_repeat_meal_reflects_users_own_prior_and_says_so(db, monkeypatch):
    """Ten logged 'chicken and rice' at ~650 cal: a new 'usual chicken and rice'
    should land near THEIR number (not a generic guess) and the reply should own
    the source ('your usual' / 'like last time' phrasing)."""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    for d in range(2, 12):
        _seed_meal(db, user.id, "chicken and rice", 650, 45, days_ago=d)

    reply = run_agent_loop(user, "just had my usual chicken and rice", "freeform")
    print(f"\n[REPEAT reply] {reply!r}")

    new = _todays_meals(user.id)
    for m in new:
        print(f"[REPEAT logged] {m.description!r} {m.calories}cal/{m.protein_g}g")
    assert new, f"repeat meal never logged: {reply!r}"
    total_cal = sum(m.calories or 0 for m in new)
    # Their prior is 650/45. A generic chicken-and-rice guess ranges wildly; landing
    # within ±15% of THEIR median is the signal the prior was used.
    assert 550 <= total_cal <= 750, \
        f"estimate ignored the user's own prior (650): logged {total_cal}: {reply!r}"

    low = reply.lower()
    assert any(p in low for p in ("usual", "last time", "like always", "same as",
                                  "your typical", "you usually")), \
        f"prior used but never owned to the user: {reply!r}"


def test_novel_meal_claims_no_history(db, monkeypatch):
    """Honesty invariant: first-ever meal → no 'your usual' language, no invented
    history. (History exists for OTHER meals, so the tool is live and could confuse.)"""
    _enable(monkeypatch)
    from tests.factories import make_user
    from agent_loop import run_agent_loop

    user = make_user(db, name="Sam", calorie_target=2600, protein_target=160)
    for d in range(2, 8):
        _seed_meal(db, user.id, "chicken and rice", 650, 45, days_ago=d)

    reply = run_agent_loop(user, "tried a lamb shawarma wrap from that new spot", "freeform")
    print(f"\n[NOVEL reply] {reply!r}")

    low = reply.lower()
    for banned in ("your usual", "you usually", "like last time", "same as last",
                   "as always", "logged this before"):
        assert banned not in low, f"claimed a history that doesn't exist: {reply!r}"
    assert _todays_meals(user.id), f"novel meal never logged: {reply!r}"
