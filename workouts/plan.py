"""build_session — the 'i remember' half. For each exercise in the template the
planned weight/reps come from the user's most recent completed sets for that
exercise (best completed set as the baseline; hit every planned rep last time →
bump weight by the plate step). No history → template defaults. Legacy
`Workout.exercises` rows (pre-card logs like "bench 135x3x8") count as history
too, so the first card isn't blind to what they told the coach last week."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from models import get_session, SetLog, WorkoutSession, Workout, active

logger = logging.getLogger("cued.workouts")
from workouts.templates import TEMPLATES, normalize_template_key, slug_for_name, plate_step_for_slug, templates_for, day_template


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _last_session_sets(session, user_id: int, exercise: str) -> list[SetLog]:
    """All SetLogs for this exercise from the user's most recent session that has
    at least one DONE set of it (the plan reads a whole session, not a lone set)."""
    last = (session.query(WorkoutSession)
            .join(SetLog, SetLog.session_id == WorkoutSession.id)
            .filter(WorkoutSession.user_id == user_id, SetLog.exercise == exercise, SetLog.done.is_(True))
            .order_by(WorkoutSession.date.desc(), WorkoutSession.id.desc()).first())
    if not last:
        return []
    return (session.query(SetLog).filter(SetLog.session_id == last.id, SetLog.exercise == exercise)
            .order_by(SetLog.set_index).all())


def _legacy_baseline(session, user_id: int, exercise: str):
    """(weight, reps, sets_hit_all) from the newest legacy Workout row naming this
    exercise with a weight, or None."""
    rows = (active(session, Workout, user_id=user_id).filter(Workout.exercises.isnot(None))
            .order_by(Workout.date.desc(), Workout.id.desc()).limit(20).all())
    for w in rows:
        for e in (w.exercises or []):
            if not isinstance(e, dict):
                continue
            if slug_for_name(e.get("name")) == exercise and e.get("weight") and e.get("reps"):
                return float(e["weight"]), int(e["reps"]), True
    return None


def _next_bodyweight_targets(session, user_id: int, tmpl) -> tuple[float, int, str]:
    """Bodyweight progression: reps, not plates. Hit every planned rep last time →
    +rep_step on the last plan; otherwise repeat it. A weight the user typed in
    (a vest, a backpack) is kept, never invented."""
    sets = _last_session_sets(session, user_id, tmpl.slug)
    done = [s for s in sets if s.done and s.actual_reps]
    if not done:
        return 0.0, tmpl.reps, "template"
    base = max(int(s.planned_reps or tmpl.reps) for s in sets)
    hit_all = all(int(s.actual_reps or 0) >= int(s.planned_reps or tmpl.reps) for s in done) \
        and len(done) >= len(sets)
    weight = max(float(s.actual_weight or 0) for s in done)
    return weight, base + (tmpl.rep_step if hit_all else 0), "history"


def next_targets(session, user_id: int, tmpl, user=None) -> tuple[float, int, str]:
    """(planned_weight, planned_reps, source) for one exercise. With no direct
    history the load is CALIBRATED to the person (workouts/calibrate.py): a stated
    anchor for the lift, a related lift they've done, else strength standards from
    their sex / bodyweight / level. The bare template default is the last resort
    (no profile at all). Pass `user` to enable calibration."""
    if tmpl.default_weight == 0 and tmpl.rep_step:
        return _next_bodyweight_targets(session, user_id, tmpl)
    sets = _last_session_sets(session, user_id, tmpl.slug)
    done = [s for s in sets if s.done and s.actual_weight and s.actual_reps]
    if done:
        best = max(done, key=lambda s: (float(s.actual_weight), int(s.actual_reps)))
        hit_all = all((s.actual_reps or 0) >= (s.planned_reps or tmpl.reps) for s in sets if s.done) \
            and len(done) >= len(sets)
        weight = float(best.actual_weight) + (tmpl.plate_step if hit_all else 0.0)
        return weight, tmpl.reps, "history"
    legacy = _legacy_baseline(session, user_id, tmpl.slug)
    if legacy:
        w, r, _ = legacy
        return (w + tmpl.plate_step) if r >= tmpl.reps else w, tmpl.reps, "legacy"
    if user is not None and tmpl.default_weight:
        from workouts.calibrate import calibrated_load
        w, src = calibrated_load(session, user, tmpl.slug, tmpl.reps, tmpl.default_weight, tmpl.plate_step)
        return w, tmpl.reps, src
    return tmpl.default_weight, tmpl.reps, "template"


def build_session(user, template_key: str, *, now=None) -> WorkoutSession:
    key = normalize_template_key(template_key)
    if not key:
        raise ValueError(f"unknown template {template_key!r}")
    now = now or _utcnow()
    session = get_session()
    try:
        ws = WorkoutSession(user_id=user.id, date=now, template_key=key, status="planned")
        session.add(ws)
        session.flush()
        sources: dict[str, str] = {}
        for tmpl in day_template(user, key):
            weight, reps, src = next_targets(session, user.id, tmpl, user=user)
            sources[tmpl.slug] = src
            for i in range(tmpl.sets):
                session.add(SetLog(session_id=ws.id, exercise=tmpl.slug, exercise_label=tmpl.label,
                                   set_index=i, planned_weight=weight, planned_reps=reps, done=False))
        logger.info("PLAN_BUILT user=%s session=%s key=%s sources=%s", user.id, ws.id, key,
                    ",".join(f"{k}:{v}" for k, v in sources.items()))
        session.commit()
        session.refresh(ws)
        _ = ws.sets  # load before detaching
        session.expunge(ws)
        return ws
    finally:
        session.close()
