"""Guard the naive-UTC timestamp convention.

Aware `datetime.now(timezone.utc)` column defaults get rotated to the DB
*session* timezone when stored into a `timestamp WITHOUT time zone` column, so
in any non-UTC session the stored instant is shifted by the offset. Every
"was this done today?" query computes a naive-UTC day window, so a shifted
default silently breaks day-boundary logic (target-change explanations, gym-beat
dedup, meal day windowing) in the early-local-morning hours.

This test is clock-INDEPENDENT: the aware-vs-naive bug shifts the stored value by
the whole TZ offset (hours), so `stored ≈ utcnow` fails regardless of wall-clock
whenever the session TZ isn't UTC (as the test cluster's is not).
"""
from datetime import datetime, timezone


def test_default_timestamps_store_naive_utc(db):
    from tests.factories import make_user
    from models import get_session, User, Message, TargetAdjustment, Meal

    user = make_user(db)
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="out", body="hi", message_type="gym_line_d1"))
        s.add(TargetAdjustment(user_id=user.id, old_target=1, new_target=2, changed=True, reason="x"))
        s.add(Meal(user_id=user.id, description="x", calories=1, protein_g=1, carbs_g=1, fat_g=1))
        s.commit()
        utcnow = datetime.now(timezone.utc).replace(tzinfo=None)
        for obj, field in ((s.query(Message).filter_by(user_id=user.id).first(), "created_at"),
                           (s.query(TargetAdjustment).filter_by(user_id=user.id).first(), "at"),
                           (s.query(Meal).filter_by(user_id=user.id).first(), "eaten_at"),
                           (s.query(Meal).filter_by(user_id=user.id).first(), "logged_at")):
            val = getattr(obj, field)
            assert val.tzinfo is None, f"{type(obj).__name__}.{field} stored tz-aware"
            drift = abs((val - utcnow).total_seconds())
            assert drift < 120, (
                f"{type(obj).__name__}.{field} drifted {drift:.0f}s from utcnow — "
                f"aware default rotated by the DB session TZ (stored {val}, utcnow {utcnow})")
    finally:
        s.close()
