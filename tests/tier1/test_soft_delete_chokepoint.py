"""
Phase 3 — soft-delete chokepoint (note #1). A deleted row must not leak through
ANY reader. The chokepoint (models.active) is the single filter; these assert it
holds across the correction-round-trip-critical paths: the accessor itself, the
recomputed daily totals (denormalized counters), and the loop's unified context.
"""

from __future__ import annotations

from datetime import datetime, timezone


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_active_and_ghost(db_user_id):
    from models import get_session, Meal
    s = get_session()
    try:
        now = _utcnow_naive()
        s.add(Meal(user_id=db_user_id, description="real pork chops", calories=650,
                   protein_g=72, eaten_at=now))
        s.add(Meal(user_id=db_user_id, description="ghost pork chops", calories=650,
                   protein_g=72, eaten_at=now, deleted_at=now))
        s.commit()
    finally:
        s.close()


def test_active_accessor_excludes_deleted(db):
    from tests.factories import make_user
    from models import get_session, Meal, active
    user = make_user(db)
    _seed_active_and_ghost(user.id)
    s = get_session()
    try:
        descs = [m.description for m in active(s, Meal, user_id=user.id).all()]
    finally:
        s.close()
    assert "real pork chops" in descs
    assert "ghost pork chops" not in descs, "chokepoint leaked a deleted row"


def test_recompute_daily_totals_excludes_deleted(db):
    from tests.factories import make_user
    from models import get_session, User, recompute_daily_totals
    user = make_user(db)
    _seed_active_and_ghost(user.id)
    recompute_daily_totals(user.id)
    s = get_session()
    try:
        u = s.query(User).get(user.id)
        cals, prot = u.calories_today, u.protein_today
    finally:
        s.close()
    assert cals == 650 and prot == 72, f"deleted meal's macros linger in totals: {cals}/{prot}"


def test_loop_context_excludes_deleted_meal(db):
    from tests.factories import make_user
    from models import get_session
    from agent_loop import build_loop_context
    user = make_user(db)
    _seed_active_and_ghost(user.id)
    s = get_session()
    try:
        ctx = build_loop_context(user, s)
    finally:
        s.close()
    assert "real pork chops" in ctx
    assert "ghost pork chops" not in ctx, "loop context surfaced a deleted meal"
