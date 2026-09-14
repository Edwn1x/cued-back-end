"""PR detection — a higher Epley e1RM than any prior COMPLETED set for the
exercise, or more reps at the same-or-higher weight. Returns the previous best
so the message can say "last time was 185 × 3"."""

from __future__ import annotations

from dataclasses import dataclass


def epley_1rm(weight: float, reps: int) -> float:
    if reps <= 0 or weight <= 0:
        return 0.0
    return weight * (1 + reps / 30.0)


@dataclass(frozen=True)
class PR:
    exercise: str
    weight: float
    reps: int
    prev_weight: float | None
    prev_reps: int | None
    kind: str  # "e1rm" | "reps_at_weight" | "first"

    @property
    def message(self) -> str:
        """The site's exact line: `190 × 4 is a PR 🎉 last time was 185 × 3.`"""
        me = f"{_fmt(self.weight)} × {self.reps} is a PR 🎉"
        if self.prev_weight is None:
            return f"{me} first time logged."
        return f"{me} last time was {_fmt(self.prev_weight)} × {self.prev_reps}."


def _fmt(w: float) -> str:
    return f"{w:g}"


def prior_completed_sets(session, user_id: int, exercise: str, *, exclude_session_id=None) -> list[tuple[float, int]]:
    """(weight, reps) of every DONE set for this exercise in the user's PREVIOUS
    sessions. "Prior" means last time, not earlier today: the site's example
    (185×5 · 185×5 · 190×4 — "190 × 4 is a PR, last time was 185 × 3") only
    holds if today's other sets don't count, and a set must never be judged
    against sets logged after it."""
    from models import SetLog, WorkoutSession
    q = (session.query(SetLog.actual_weight, SetLog.actual_reps)
         .join(WorkoutSession, SetLog.session_id == WorkoutSession.id)
         .filter(WorkoutSession.user_id == user_id, SetLog.exercise == exercise, SetLog.done.is_(True),
                 SetLog.actual_weight.isnot(None), SetLog.actual_reps.isnot(None)))
    if exclude_session_id is not None:
        q = q.filter(SetLog.session_id != exclude_session_id)
    return [(float(w), int(r)) for w, r in q.all() if w and r]


def check_pr(session, user_id: int, exercise: str, weight: float, reps: int, *, exclude_session_id=None) -> PR | None:
    """Judge one set against previous sessions. Pass the CURRENT session's id so
    today's other sets are excluded."""
    if not weight or not reps:
        return None
    prior = prior_completed_sets(session, user_id, exercise, exclude_session_id=exclude_session_id)
    if not prior:
        return None  # a first-ever set is a baseline, not a PR to celebrate
    best_e1rm = max(prior, key=lambda p: epley_1rm(*p))
    if epley_1rm(weight, reps) > epley_1rm(*best_e1rm) + 1e-9:
        return PR(exercise, weight, reps, best_e1rm[0], best_e1rm[1], "e1rm")
    # More reps at the SAME weight (a heavier weight with more reps is already an
    # e1RM PR; a lighter weight with more reps is not a PR — 175×4 vs 185×3).
    same = [p for p in prior if p[0] == weight]
    if same:
        best_reps = max(same, key=lambda p: p[1])
        if reps > best_reps[1]:
            return PR(exercise, weight, reps, best_reps[0], best_reps[1], "reps_at_weight")
    return None
