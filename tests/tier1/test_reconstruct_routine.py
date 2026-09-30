"""
reconstruct_routine_from_history (READ-ONLY): rebuild what the user actually did on a
past day from their logged SetLog rows, so the coach reflects the REAL exercises back
and offers to save them — instead of confabulating "I don't have your old card's
exercises" (live incident user 31: 8 completed push sessions on record, coach claimed
the data was gone). See workouts/session_ops.reconstruct_from_history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.factories import make_user


def _utc(days_ago=0):
    # naive UTC, matching the app's DateTime columns
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)


def _session(db, user_id, template_key, when, sets):
    """Seed a done WorkoutSession + its SetLog rows. `sets` = list of
    (exercise_slug, exercise_label, set_index)."""
    from models import WorkoutSession, SetLog
    ws = WorkoutSession(user_id=user_id, template_key=template_key, status="done",
                        date=when, finished_at=when)
    db.add(ws)
    db.commit()
    db.refresh(ws)
    for slug, label, idx in sets:
        db.add(SetLog(session_id=ws.id, exercise=slug, exercise_label=label,
                      set_index=idx, actual_weight=100, actual_reps=8, done=True,
                      done_at=when))
    db.commit()
    return ws.id


# A real push session with NOISY duplicate names: incline_dumbbell_bench_press and
# incline_db_press are the same lift; single_are_tricep_pushdown is garbled "single
# arm tricep pushdown".
NOISY_PUSH = [
    ("bench_press", "bench press", 0),
    ("bench_press", "bench press", 1),
    ("bench_press", "bench press", 2),
    ("bench_press", "bench press", 3),
    ("incline_dumbbell_bench_press", "incline dumbbell bench press", 4),
    ("incline_db_press", "incline db press", 5),
    ("incline_db_press", "incline db press", 6),
    ("overhead_press", "overhead press", 7),
    ("overhead_press", "overhead press", 8),
    ("overhead_press", "overhead press", 9),
    ("single_are_tricep_pushdown", "single are tricep pushdown", 10),
    ("single_are_tricep_pushdown", "single are tricep pushdown", 11),
    ("single_are_tricep_pushdown", "single are tricep pushdown", 12),
]


def _table_snapshot(db):
    from models import WorkoutSession, SetLog
    ws = {(w.id, w.status, w.template_key, w.finished_at)
          for w in db.query(WorkoutSession).all()}
    sl = {(s.id, s.session_id, s.exercise, s.exercise_label, s.set_index,
           s.actual_weight, s.actual_reps, s.done)
          for s in db.query(SetLog).all()}
    return ws, sl


def test_reconstructs_cleaned_distinct_exercises(db):
    from agent_tools import handle_reconstruct_routine_from_history
    user = make_user(db)
    _session(db, user.id, "push", _utc(2), NOISY_PUSH)

    out = handle_reconstruct_routine_from_history(user.id, {"template_key": "push"})

    low = out.lower()
    # It reconstructed — it did NOT claim the data is gone.
    assert not out.startswith("error")
    assert "no completed" not in low
    assert "don't have" not in low and "not saved" not in low
    # and it explicitly steers the coach off the confabulation.
    assert "do not tell them you can't" in low
    # The four distinct lifts, dupes collapsed.
    assert "bench press 4 sets" in low
    assert "overhead press 3 sets" in low
    # incline_dumbbell_bench_press + incline_db_press collapsed to ONE entry (2+1=3 sets).
    assert low.count("incline") == 1
    assert "incline db press 3 sets" in low
    # garble fixed: "are" -> "arm", never shown raw.
    assert "single are" not in low
    assert "arm" in low
    # steers the coach to save via save_routine and to show it back.
    assert "save_routine" in out


def test_no_completed_sessions_returns_honest_none(db):
    from agent_tools import handle_reconstruct_routine_from_history
    user = make_user(db)

    out = handle_reconstruct_routine_from_history(user.id, {"template_key": "push"})

    assert "no completed" in out.lower()
    assert "save_routine" in out


def test_falls_back_and_names_the_day_used(db):
    from agent_tools import handle_reconstruct_routine_from_history
    user = make_user(db)
    # They have a legs session but NO pull session.
    _session(db, user.id, "legs", _utc(1),
             [("squat", "squat", 0), ("squat", "squat", 1),
              ("leg_press", "leg press", 2)])

    out = handle_reconstruct_routine_from_history(user.id, {"template_key": "pull"})

    low = out.lower()
    # Fell back to the most recent done session and named the day it used.
    assert "no completed pull" in low
    assert "legs" in low
    assert "squat 2 sets" in low
    assert "leg press 1 set" in low


def test_no_template_key_uses_most_recent_session(db):
    from agent_tools import handle_reconstruct_routine_from_history
    user = make_user(db)
    _session(db, user.id, "legs", _utc(5), [("squat", "squat", 0)])
    _session(db, user.id, "push", _utc(1), [("bench_press", "bench press", 0)])

    out = handle_reconstruct_routine_from_history(user.id, {})

    low = out.lower()
    assert "push" in low
    assert "bench press 1 set" in low


def test_handler_is_read_only(db):
    from agent_tools import handle_reconstruct_routine_from_history
    user = make_user(db)
    _session(db, user.id, "push", _utc(2), NOISY_PUSH)

    db.expire_all()
    before = _table_snapshot(db)
    handle_reconstruct_routine_from_history(user.id, {"template_key": "push"})
    db.expire_all()
    after = _table_snapshot(db)

    assert before == after, "reconstruct_from_history must not modify any data"


def test_completed_template_keys_distinct_newest_first(db):
    from workouts.session_ops import completed_template_keys
    user = make_user(db)
    _session(db, user.id, "legs", _utc(5), [("squat", "squat", 0)])
    _session(db, user.id, "push", _utc(3), [("bench_press", "bench press", 0)])
    _session(db, user.id, "push", _utc(1), [("bench_press", "bench press", 0)])

    keys = completed_template_keys(user.id)

    assert keys == ["push", "legs"]
