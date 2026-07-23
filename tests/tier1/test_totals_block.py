"""
Burn-in item 2 — macro totals computed in code, not by the model. The TODAY'S TOTALS
block sums the active meals in the user's local day (soft-delete filtered, one source of
truth) and computes remaining-vs-target; the model quotes these instead of re-adding.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _naive_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _meal(user_id, cal, pro=0, *, at, desc="m", deleted=False):
    from models import Meal
    return Meal(user_id=user_id, description=desc, calories=cal, protein_g=pro,
                eaten_at=at, source="text", log_type="user_reported",
                deleted_at=at if deleted else None)


def _ctx(user_id):
    from models import get_session, User
    from agent_loop import build_loop_context
    s = get_session()
    try:
        return build_loop_context(s.get(User, user_id), s)
    finally:
        s.close()


def test_totals_sum_excludes_soft_deleted(db):
    """The sum is over ACTIVE meals only — the deleted row's macros never reach the
    prompt (routes through the same soft-delete chokepoint as every reader)."""
    from tests.factories import make_user
    from models import get_session

    user = make_user(db, calorie_target=2000, protein_target=150)
    now = _naive_utcnow()
    s = get_session()
    try:
        s.add(_meal(user.id, 300, 20, at=now, desc="eggs"))
        s.add(_meal(user.id, 400, 30, at=now, desc="chicken"))
        s.add(_meal(user.id, 999, 99, at=now, desc="deleted dupe", deleted=True))
        s.commit()
    finally:
        s.close()

    ctx = _ctx(user.id)
    assert "calories: 700 | protein: 50g" in ctx, ctx
    assert "999" not in ctx, "soft-deleted meal leaked into the totals or list"


def test_remaining_vs_target_is_exact(db):
    from tests.factories import make_user
    from models import get_session

    user = make_user(db, calorie_target=2000, protein_target=150)
    now = _naive_utcnow()
    s = get_session()
    try:
        s.add(_meal(user.id, 300, 20, at=now))
        s.add(_meal(user.id, 400, 30, at=now))
        s.commit()
    finally:
        s.close()

    ctx = _ctx(user.id)
    assert "calories remaining vs target: 1300 (700/2000)" in ctx, ctx
    assert "protein remaining vs target: 100g (50/150g)" in ctx, ctx


def test_no_target_omits_remaining_line(db):
    from tests.factories import make_user
    from models import get_session

    user = make_user(db)  # no targets
    s = get_session()
    try:
        s.add(_meal(user.id, 300, 20, at=_naive_utcnow()))
        s.commit()
    finally:
        s.close()

    ctx = _ctx(user.id)
    assert "calories: 300" in ctx and "remaining vs target" not in ctx


def test_local_day_windowing_11pm_counts_today_not_tomorrow(db):
    """A meal near the end of the local day counts today; one just into the next local
    day does not — the totals window is the user's LOCAL day, not a UTC day."""
    from tests.factories import make_user
    from timefmt import local_day_bounds
    from models import get_session

    user = make_user(db)
    start, end = local_day_bounds(user)  # today's local day, as naive UTC
    late_today = end - timedelta(minutes=30)       # ~11:30pm local, still today
    early_tomorrow = end + timedelta(minutes=30)   # just past local midnight
    s = get_session()
    try:
        s.add(_meal(user.id, 250, at=late_today, desc="late night snack"))
        s.add(_meal(user.id, 500, at=early_tomorrow, desc="tomorrows breakfast"))
        s.commit()
    finally:
        s.close()

    ctx = _ctx(user.id)
    assert "late night snack" in ctx and "calories: 250" in ctx
    assert "tomorrows breakfast" not in ctx, "next local day's meal leaked into today's totals"
