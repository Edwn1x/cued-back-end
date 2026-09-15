"""
Session operations shared by the card, the text path (Phase 4), the tapback path
(Phase 5), the coach tool (Phase 3), and log_workout: find the active session,
apply a text deviation, close, mirror to the legacy workouts table + split
pointer, and the 6-hour abandon sweep.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, timedelta

from models import get_session, User, WorkoutSession, SetLog, Workout
from card_page import apply_set_update, build_state
from workouts.parse import parse_set_text, SetUpdate

logger = logging.getLogger("cued.workouts")

ABANDON_AFTER_H = 6
SWAPPED = "swapped it in."


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def active_session_id(user_id: int) -> int | None:
    session = get_session()
    try:
        ws = (session.query(WorkoutSession)
              .filter(WorkoutSession.user_id == user_id, WorkoutSession.status.in_(("planned", "active")))
              .order_by(WorkoutSession.id.desc()).first())
        return ws.id if ws else None
    finally:
        session.close()


def _session_exercises(session, ws_id: int) -> list[tuple[str, str]]:
    seen, out = set(), []
    for s in session.query(SetLog).filter(SetLog.session_id == ws_id).order_by(SetLog.id).all():
        if s.exercise not in seen:
            seen.add(s.exercise); out.append((s.exercise, s.exercise_label or s.exercise))
    return out


def _resolve_exercise(session, ws_id: int, upd: SetUpdate) -> str | None:
    rows = session.query(SetLog).filter(SetLog.session_id == ws_id).order_by(SetLog.id).all()
    ex = upd.exercise
    if ex:
        return ex
    done = [r for r in rows if r.done and r.done_at]
    if done:
        return max(done, key=lambda r: r.done_at).exercise
    first_undone = next((r for r in rows if not r.done), None)
    return first_undone.exercise if first_undone else None


def _apply_multi(session, ws, ws_id: int, upd: SetUpdate) -> int:
    """A structured "135 for 3 sets 7 reps" report: fill/create that many sets of the
    exercise at the given weight/reps. Supersedes this exercise's prior TEXT sets in
    the session (an earlier terse mis-parse), keeping card/tapback/coach sets."""
    from card_page import apply_set_update
    ex = _resolve_exercise(session, ws_id, upd)
    if not ex:
        return 0
    label = next((r.exercise_label for r in session.query(SetLog).filter(SetLog.session_id == ws_id, SetLog.exercise == ex)), None) or ex
    for r in session.query(SetLog).filter(SetLog.session_id == ws_id, SetLog.exercise == ex,
                                          SetLog.source == "text", SetLog.done.is_(True)).all():
        session.delete(r)
    session.flush()
    undone = session.query(SetLog).filter(SetLog.session_id == ws_id, SetLog.exercise == ex,
                                          SetLog.done.is_(False)).order_by(SetLog.set_index).all()
    existing = session.query(SetLog).filter(SetLog.session_id == ws_id, SetLog.exercise == ex).count()
    applied = 0
    for i in range(upd.sets or 1):
        if i < len(undone):
            apply_set_update(session, ws, undone[i], done=True, actual_weight=upd.weight, actual_reps=upd.reps, source="text")
        else:
            new = SetLog(session_id=ws_id, exercise=ex, exercise_label=label, set_index=existing + i,
                         planned_weight=upd.weight, planned_reps=upd.reps)
            session.add(new); session.flush()
            apply_set_update(session, ws, new, done=True, actual_weight=upd.weight, actual_reps=upd.reps, source="text")
        applied += 1
    return applied


def _target_set(session, ws_id: int, upd: SetUpdate) -> SetLog | None:
    rows = session.query(SetLog).filter(SetLog.session_id == ws_id).order_by(SetLog.id).all()
    ex = upd.exercise
    if not ex:
        done = [r for r in rows if r.done and r.done_at]
        if done:
            ex = max(done, key=lambda r: r.done_at).exercise
        else:
            first_undone = next((r for r in rows if not r.done), None)
            ex = first_undone.exercise if first_undone else None
    if not ex:
        return None
    mine = [r for r in rows if r.exercise == ex]
    if upd.set_index is not None:
        return next((r for r in mine if r.set_index == upd.set_index), None)
    return next((r for r in mine if not r.done), None) or (mine[-1] if mine else None)


def apply_text_update(user_id: int, text: str) -> str | None:
    """If the user has an open session and `text` is a set/skip/close, apply it and
    return the ONE reply line (or "" for a close, which sends its own summary).
    None → not a workout input; the normal turn proceeds."""
    ws_id = active_session_id(user_id)
    if not ws_id:
        return None
    session = get_session()
    try:
        upd = parse_set_text(text, _session_exercises(session, ws_id))
        if not upd:
            return None
        ws = session.get(WorkoutSession, ws_id)
        if upd.kind == "close":
            session.close()
            close_session(ws_id, via="text")
            return ""
        if upd.kind == "skip":
            n = 0
            for r in session.query(SetLog).filter(SetLog.session_id == ws_id, SetLog.exercise == upd.exercise,
                                                  SetLog.done.is_(False)).all():
                session.delete(r); n += 1
            session.commit()
            logger.info("WORKOUT_TEXT_SKIP user=%s session=%s exercise=%s removed=%s", user_id, ws_id, upd.exercise, n)
            return "skipped it." if n else "nothing left to skip there."
        if (upd.sets or 1) > 1:
            n = _apply_multi(session, ws, ws_id, upd)
            logger.info("WORKOUT_TEXT_MULTISET user=%s session=%s exercise=%s sets=%s w=%s r=%s",
                        user_id, ws_id, upd.exercise, n, upd.weight, upd.reps)
            reply = f"logged {n} sets." if n else None
            if reply is None:
                return None
            session.close()
            from workouts.card import refresh_card_async
            refresh_card_async(ws_id)
            return reply
        row = _target_set(session, ws_id, upd)
        if not row:
            return None
        weight = upd.weight if upd.weight is not None else (row.actual_weight or row.planned_weight)
        reps = upd.reps if upd.reps is not None else (row.actual_reps or row.planned_reps)
        pr = apply_set_update(session, ws, row, done=True, actual_weight=weight, actual_reps=reps, source="text")
        logger.info("WORKOUT_TEXT_SET user=%s session=%s set=%s w=%s r=%s pr=%s", user_id, ws_id, row.id, weight, reps, bool(pr))
    finally:
        session.close()
    from workouts.card import refresh_card_async
    refresh_card_async(ws_id)
    return pr or SWAPPED


def apply_tapback(user_id: int, target_message_id: str, emoji: str) -> bool:
    """👍 on a per-exercise message (Phase 5) → all its sets done at plan."""
    if emoji not in ("👍", "like", "❤️", "love", "‼️", "emphasize"):
        return False
    session = get_session()
    try:
        rows = (session.query(SetLog).join(WorkoutSession, SetLog.session_id == WorkoutSession.id)
                .filter(WorkoutSession.user_id == user_id, WorkoutSession.status.in_(("planned", "active")),
                        SetLog.provider_message_ref == target_message_id).all())
        if not rows:
            return False
        ws = session.get(WorkoutSession, rows[0].session_id)
        for r in rows:
            if not r.done:
                apply_set_update(session, ws, r, done=True, source="tapback")
        logger.info("WORKOUT_TAPBACK user=%s session=%s exercise=%s sets=%s", user_id, ws.id, rows[0].exercise, len(rows))
        return True
    finally:
        session.close()


def mirror_to_legacy(session_id: int) -> int | None:
    """The rest of the system (RECENT WORKOUTS, profile page, split pointer,
    training-day inference) reads the legacy `workouts` table. A closed session
    writes one completed row there and advances the pointer — sessions are the
    truth; the legacy row mirrors them."""
    from split_pointer import advance_split_pointer, SPLIT_CYCLES, _normalize_system
    from models import confirm_workout_today
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        if not ws:
            return None
        done = [s for s in session.query(SetLog).filter(SetLog.session_id == ws.id, SetLog.done.is_(True)).order_by(SetLog.id).all()
                if s.actual_weight and s.actual_reps]
        if not done:
            return None
        exercises, order = {}, []
        for s in done:
            if s.exercise not in exercises:
                exercises[s.exercise] = {"name": s.exercise_label or s.exercise, "sets": 0, "reps": 0, "weight": 0.0, "detail": []}
                order.append(s.exercise)
            e = exercises[s.exercise]
            e["sets"] += 1
            e["detail"].append([float(s.actual_weight), int(s.actual_reps)])
            if (float(s.actual_weight), int(s.actual_reps)) > (e["weight"], e["reps"]):
                e["weight"], e["reps"] = float(s.actual_weight), int(s.actual_reps)
        user = session.get(User, ws.user_id)
        w = Workout(user_id=ws.user_id, workout_type=ws.template_key or "logged", completed=True,
                    date=ws.finished_at or ws.date, exercises=[exercises[k] for k in order],
                    user_notes=f"card session #{ws.id}")
        session.add(w)
        session.commit()
        wid, uid, key = w.id, ws.user_id, ws.template_key
        split = user.current_split or user.confirmed_training_split
    finally:
        session.close()
    confirm_workout_today(uid)
    cycle = SPLIT_CYCLES.get(_normalize_system(split or "")) or []
    advance_split_pointer(uid, named_day=key if key in cycle else None)
    logger.info("WORKOUT_MIRRORED user=%s session=%s workout_id=%s", uid, session_id, wid)
    return wid


def close_session(session_id: int, *, via: str = "card", fill_as_planned: bool = False) -> dict | None:
    """Finish + bubble refresh + summary text + legacy mirror. `fill_as_planned`
    (text 'done' on the tapback/SMS path): undone sets are marked done at plan
    with source='coach' — assumed, not tapped, so they never count as taps."""
    from workouts.summary import finish_session
    from workouts.close import send_session_summary
    from workouts.card import refresh_card
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        if not ws:
            return None
        if ws.status == "done":
            return None
        if fill_as_planned:
            for r in session.query(SetLog).filter(SetLog.session_id == ws.id, SetLog.done.is_(False)).all():
                apply_set_update(session, ws, r, done=True, source="coach")
        uid = ws.user_id
    finally:
        session.close()
    s = finish_session(session_id)
    logger.info("WORKOUT_CLOSED user=%s session=%s via=%s volume=%s prs=%s", uid, session_id, via, s["volume_lb"], s["pr_count"])

    def _after():
        refresh_card(session_id)
        send_session_summary(session_id)
        mirror_to_legacy(session_id)
    threading.Thread(target=_after, daemon=True).start()
    return s


def abandon_stale(now: datetime | None = None) -> int:
    """Scheduler: a session still open ABANDON_AFTER_H after it started (or was
    sent, if never started) → abandoned. No message, ever."""
    now = now or _utcnow()
    cutoff = now - timedelta(hours=ABANDON_AFTER_H)
    session = get_session()
    try:
        n = 0
        for ws in session.query(WorkoutSession).filter(WorkoutSession.status.in_(("planned", "active"))).all():
            anchor = ws.started_at or ws.date
            if anchor and anchor < cutoff:
                ws.status = "abandoned"; n += 1
                logger.info("WORKOUT_ABANDONED user=%s session=%s started=%s", ws.user_id, ws.id, anchor)
        session.commit()
        return n
    finally:
        session.close()
