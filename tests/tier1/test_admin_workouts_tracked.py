"""Admin → Metrics: a "Workouts Tracked" tile beside "Meals Logged" (founder,
2026-11-19: "we don't have a workouts tracked metric"). Counts `workouts` rows — the
one ledger a card close, a text log and an "i'm at the gym" all land in — minus
soft-deleted ones."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.factories import make_user


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_tile_counts_active_workouts_total_today_and_week(db, client):
    from models import Workout
    u = make_user(db, name="A")
    now = _utcnow()
    db.add_all([
        Workout(user_id=u.id, workout_type="push", completed=True, date=now - timedelta(minutes=5),
                user_notes="card session #1"),
        Workout(user_id=u.id, workout_type="logged", completed=True, date=now - timedelta(days=3),
                exercises=[], user_notes="said they were at the gym"),
        Workout(user_id=u.id, workout_type="legs", completed=True, date=now - timedelta(days=20)),
        Workout(user_id=u.id, workout_type="pull", completed=True, date=now - timedelta(hours=1),
                deleted_at=now),                                   # soft-deleted: not tracked
    ])
    db.commit()
    html = client.get("/admin").get_data(as_text=True)
    assert "Workouts Tracked" in html
    i = html.index("Workouts Tracked")
    tile = html[i:i + 400]
    assert ">3<" in tile                      # total, deleted row excluded
    assert "1 today" in tile
    assert "2 last 7d" in tile
    # the per-user Workouts column reads the same ledger
    assert "<td>3</td>" in html


def test_empty_db_renders_zero(db, client):
    html = client.get("/admin").get_data(as_text=True)
    i = html.index("Workouts Tracked")
    tile = html[i:i + 400]
    assert ">0<" in tile and "0 today" in tile
