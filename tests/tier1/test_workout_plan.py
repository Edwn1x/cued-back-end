"""
Workout logger Phase 1 — data + plan generation (no UI, no channel).
  build_session: no history → template defaults; history → progression; legacy
  Workout rows count as history. check_pr on the site's exact example
  (185×3 prior → 190×4 is a PR). finish_session: lines + volume on the brief's card.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user

FOUNDER = dict(name="Nau", onboarding_step=3, current_split="ppl", split_pointer_day="pull",
               height_ft=5, height_in=6, weight_lbs=139, age=20, gender="male", goal="fat_loss,muscle_building")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sets(session_id):
    from models import get_session, SetLog
    s = get_session()
    try:
        return [(x.exercise, x.set_index, x.planned_weight, x.planned_reps, x.done, x.actual_weight, x.actual_reps, x.source)
                for x in s.query(SetLog).filter_by(session_id=session_id).order_by(SetLog.id).all()]
    finally:
        s.close()


def _complete(session_id, results: dict, *, source="card", when=None):
    """results: {exercise: [(w, r), ...] per set index}; None = leave undone."""
    from models import get_session, SetLog, WorkoutSession
    s = get_session()
    try:
        ws = s.get(WorkoutSession, session_id)
        ws.status, ws.started_at = "active", (when or _now())
        for x in s.query(SetLog).filter_by(session_id=session_id).all():
            plan = results.get(x.exercise)
            if plan is None or x.set_index >= len(plan) or plan[x.set_index] is None:
                continue
            x.actual_weight, x.actual_reps = plan[x.set_index]
            x.done, x.done_at, x.source = True, (when or _now()), source
        s.commit()
    finally:
        s.close()


# ─── build_session ───────────────────────────────────────────────────────────

def test_plan_from_no_history_uses_template_defaults(db):
    from workouts.plan import build_session
    user = make_user(db, **FOUNDER)
    ws = build_session(user, "push")
    assert ws.status == "planned" and ws.template_key == "push"
    rows = _sets(ws.id)
    by_ex = {}
    for ex, idx, w, r, *_ in rows:
        by_ex.setdefault(ex, []).append((w, r))
    assert list(by_ex) == ["bench_press", "incline_db_press", "cable_fly", "tricep_pushdown"]
    assert [len(v) for v in by_ex.values()] == [4, 3, 3, 3]
    assert by_ex["bench_press"] == [(135.0, 5)] * 4 and by_ex["incline_db_press"] == [(40.0, 10)] * 3
    assert all(not done for *_, done, _aw, _ar, _src in rows)


def test_plan_accepts_aliases_and_rejects_unknown(db):
    from workouts.plan import build_session
    user = make_user(db, **FOUNDER)
    assert build_session(user, "Full Body").template_key == "full_body"
    assert build_session(user, "chest_back").template_key == "upper"
    with pytest.raises(ValueError):
        build_session(user, "tuesday")


def test_plan_progresses_when_every_rep_was_hit_and_holds_when_not(db):
    from workouts.plan import build_session
    user = make_user(db, **FOUNDER)
    first = build_session(user, "push")
    # bench: all 4 sets at plan → +5. incline: missed reps on set 3 → hold at the best weight.
    _complete(first.id, {"bench_press": [(135, 5)] * 4, "incline_db_press": [(40, 10), (40, 10), (40, 8)]})
    second = build_session(user, "push")
    by_ex = {}
    for ex, idx, w, r, *_ in _sets(second.id):
        by_ex.setdefault(ex, []).append((w, r))
    assert by_ex["bench_press"] == [(140.0, 5)] * 4, by_ex["bench_press"]
    assert by_ex["incline_db_press"] == [(40.0, 10)] * 3
    assert by_ex["cable_fly"] == [(20.0, 12)] * 3          # never done → template default


def test_plan_baselines_on_the_best_completed_set_not_the_last(db):
    from workouts.plan import build_session
    user = make_user(db, **FOUNDER)
    first = build_session(user, "push")
    _complete(first.id, {"bench_press": [(185, 5), (185, 5), (190, 4), (185, 5)]})  # a deviation up, reps missed on it
    second = build_session(user, "push")
    bench = [(w, r) for ex, _i, w, r, *_ in _sets(second.id) if ex == "bench_press"]
    assert bench == [(190.0, 5)] * 4  # best completed set 190; not every rep hit → no bump


def test_lower_body_progression_is_a_10lb_step(db):
    from workouts.plan import build_session
    user = make_user(db, **FOUNDER)
    first = build_session(user, "legs")
    _complete(first.id, {"squat": [(155, 5)] * 4})
    second = build_session(user, "legs")
    squat = {(w, r) for ex, _i, w, r, *_ in _sets(second.id) if ex == "squat"}
    assert squat == {(165.0, 5)}


def test_legacy_workout_rows_count_as_history(db):
    """GATE 1 for the founder: his pre-card 'bench 135x3x8' (legacy Workout.exercises)
    must inform the first card — 8 reps ≥ the template's 5 → bench planned at 140."""
    from workouts.plan import build_session
    from models import get_session, Workout
    user = make_user(db, **FOUNDER)
    s = get_session()
    try:
        s.add(Workout(user_id=user.id, workout_type="push", completed=True, date=_now() - timedelta(days=2),
                      exercises=[{"name": "bench press", "sets": 3, "reps": 8, "weight": 135}]))
        s.commit()
    finally:
        s.close()
    ws = build_session(user, "push")
    bench = {(w, r) for ex, _i, w, r, *_ in _sets(ws.id) if ex == "bench_press"}
    assert bench == {(140.0, 5)}


# ─── PRs ─────────────────────────────────────────────────────────────────────

def test_pr_on_the_sites_exact_example(db):
    from workouts.plan import build_session
    from workouts.prs import check_pr, epley_1rm
    from models import get_session
    user = make_user(db, **FOUNDER)
    prior = build_session(user, "push")
    _complete(prior.id, {"bench_press": [(185, 3), (185, 3), (185, 3), (185, 3)]})
    s = get_session()
    try:
        pr = check_pr(s, user.id, "bench_press", 190, 4)
        assert pr is not None and pr.kind == "e1rm"
        assert pr.message == "190 × 4 is a PR 🎉 last time was 185 × 3."
        assert check_pr(s, user.id, "bench_press", 185, 3) is None          # a repeat is not a PR
        assert check_pr(s, user.id, "bench_press", 185, 4) is not None      # more reps at the same weight
        assert check_pr(s, user.id, "bench_press", 175, 4) is None          # e1rm 198.3 < 203.5
        assert check_pr(s, user.id, "cable_fly", 30, 12) is None           # first ever = baseline, not a PR
        # "prior" = previous sessions: judged from inside a session that already has
        # 185×5 done today, 190×4 is still a PR over last time's 185×3.
        today = build_session(user, "push")
        _complete(today.id, {"bench_press": [(185, 5), (185, 5)]})
        assert check_pr(s, user.id, "bench_press", 190, 4, exclude_session_id=today.id).message == \
            "190 × 4 is a PR 🎉 last time was 185 × 3."
        assert check_pr(s, user.id, "bench_press", 190, 4) is None         # without the exclusion, today's 185×5 wins
    finally:
        s.close()
    assert round(epley_1rm(190, 4), 1) == 215.3 and round(epley_1rm(185, 3), 1) == 203.5


# ─── summary ─────────────────────────────────────────────────────────────────

BRIEF_CARD = {
    "bench_press": [(185, 5), (185, 5), (190, 4), (185, 5)],
    "incline_db_press": [(65, 10), (65, 9), (65, 8)],
    "cable_fly": [(30, 12), (30, 12), (30, 11)],
    "tricep_pushdown": [(50, 12), (50, 12), (50, 10)],
}


def test_summary_lines_volume_prs_and_closer_on_the_briefs_card(db):
    from workouts.plan import build_session
    from workouts.summary import finish_session, format_summary
    from models import get_session, WorkoutSession, SetLog
    user = make_user(db, **FOUNDER)
    # history so 190×4 is a PR over 185×3 (the brief's example)
    prior = build_session(user, "push")
    _complete(prior.id, {"bench_press": [(185, 3)] * 4}, when=_now() - timedelta(days=7))
    ws = build_session(user, "push", now=datetime(2026, 9, 16, 22, 0))      # a wednesday
    start = datetime(2026, 9, 16, 22, 0)
    _complete(ws.id, BRIEF_CARD, when=start)
    s = get_session()
    try:  # three taps + one text, the rest by tapback (per-exercise 👍)
        rows = s.query(SetLog).filter_by(session_id=ws.id).order_by(SetLog.id).all()
        taps_left = 3
        for x in rows:
            if x.actual_weight == 190:
                x.source = "text"          # the one deviation typed in
            elif taps_left:
                x.source, taps_left = "card", taps_left - 1
            else:
                x.source = "tapback"
        s.get(WorkoutSession, ws.id).started_at = start
        s.commit()
    finally:
        s.close()
    summary = finish_session(ws.id, now=start + timedelta(minutes=48))
    text = format_summary(summary)
    assert text.splitlines()[0] == "push · wed · 48 min"
    assert text.splitlines()[1] == "bench press — 185×5 · 185×5 · 190×4 · 185×5"
    assert text.splitlines()[2] == "incline db press — 65×10 · 65×9 · 65×8"
    assert text.splitlines()[3] == "cable fly — 30×12 · 30×12 · 30×11"
    assert text.splitlines()[4] == "tricep pushdown — 50×12 · 50×12 · 50×10"
    # Σ actual_weight × actual_reps over the brief's lines = 8,040 (the brief says 9,240;
    # the site spec file is missing — see PR notes). 1 PR: 190×4 over 185×3.
    assert summary["volume_lb"] == 8040 and summary["pr_count"] == 1
    assert text.splitlines()[-1] == "8,040 lb total · 1 PR"
    assert summary["prs"] == ["190 × 4 is a PR 🎉 last time was 185 × 3."]
    assert summary["taps"] == 3 and summary["texts"] == 1 and summary["tapbacks"] == 9
    s = get_session()
    try:
        row = s.get(WorkoutSession, ws.id)
        assert row.status == "done" and row.total_volume_lb == 8040 and row.pr_count == 1
    finally:
        s.close()


def test_finish_is_idempotent_and_counts_only_done_sets(db):
    from workouts.plan import build_session
    from workouts.summary import finish_session
    user = make_user(db, **FOUNDER)
    ws = build_session(user, "push")
    _complete(ws.id, {"bench_press": [(135, 5), (135, 5), None, None]})
    a = finish_session(ws.id)
    b = finish_session(ws.id)
    assert a["volume_lb"] == b["volume_lb"] == 1350 and a["sets_done"] == 2 and a["sets_planned"] == 13
