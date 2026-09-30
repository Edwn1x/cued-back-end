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


def has_logged_sets(session, ws_id: int) -> bool:
    """Real logged work on the session: any done set (card / text / tapback / coach).
    The line between FINALIZE (keep it) and abandon (nothing to keep)."""
    return session.query(SetLog.id).filter(SetLog.session_id == ws_id, SetLog.done.is_(True)).first() is not None


def reset_active_session(user_id: int) -> dict:
    """Clear the user's ACTIVE workout session so a fresh card can go out on demand
    (the gym-deadlock fix). NEVER discards logged work: a session with any done set is
    FINALIZED (status='done' + the normal summary + legacy mirror); an empty one is
    abandoned. Returns {"status": "finalized"|"abandoned"|"none", "session_id",
    "template_key", "sets_logged"} so the coach can relay it plainly."""
    ws_id = active_session_id(user_id)
    if not ws_id:
        return {"status": "none", "session_id": None, "template_key": None, "sets_logged": 0}
    session = get_session()
    try:
        ws = session.get(WorkoutSession, ws_id)
        key = ws.template_key if ws else None
        logged = has_logged_sets(session, ws_id)
        n = (session.query(SetLog.id).filter(SetLog.session_id == ws_id, SetLog.done.is_(True)).count()
             if logged else 0)
    finally:
        session.close()
    if logged:
        # finalize — same path as a normal close: summary text + legacy mirror. Work is kept.
        close_session(ws_id, via="reset")
        logger.info("WORKOUT_SESSION_RESET_FINALIZED user=%s session=%s key=%s sets=%s", user_id, ws_id, key, n)
        return {"status": "finalized", "session_id": ws_id, "template_key": key, "sets_logged": n}
    session = get_session()
    try:
        ws = session.get(WorkoutSession, ws_id)
        if ws and ws.status in ("planned", "active"):
            ws.status = "abandoned"
            session.commit()
    finally:
        session.close()
    logger.info("WORKOUT_SESSION_RESET_ABANDONED user=%s session=%s key=%s", user_id, ws_id, key)
    return {"status": "abandoned", "session_id": ws_id, "template_key": key, "sets_logged": 0}


def active_session_brief(user) -> str | None:
    """A compact loop-context block naming the CURRENT in-progress session's REAL type,
    start time, and logged-set count — so the coach stops confabulating the day (it called
    a pull session 'push'). None when nothing is open. Read-only."""
    from workouts.templates import day_label
    ws_id = active_session_id(user.id)
    if not ws_id:
        return None
    session = get_session()
    try:
        ws = session.get(WorkoutSession, ws_id)
        if not ws:
            return None
        n = session.query(SetLog.id).filter(SetLog.session_id == ws_id, SetLog.done.is_(True)).count()
        key = ws.template_key
        started = ws.started_at or ws.date
    finally:
        session.close()
    when = ""
    try:
        import config as _cfg
        if started and getattr(_cfg, "CONTEXT_LOCAL_TIME_ENABLED", True):
            from timefmt import render_time
            when = f", started {render_time(started, user, relative=False)}"
    except Exception:  # noqa: BLE001 — a time-format hiccup must not drop the block
        when = ""
    logged = f"{n} set{'s' if n != 1 else ''} logged" if n else "no sets logged yet"
    return ("## ACTIVE WORKOUT SESSION\n"
            f"{day_label(key)} day (session #{ws_id}){when} — {logged}. This is the in-progress "
            f"session RIGHT NOW; refer to it as {day_label(key)}, never a different day. To send a "
            f"fresh card you must clear it first with reset_workout_session (it finalizes the session "
            f"if sets are logged so nothing is lost, else clears the empty one).")


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


# --- reconstruct a routine from logged history (READ-ONLY) --------------------
# Coach affordance for the false-negative-affordance family (live incident user 31:
# 8 completed push sessions on record, no saved `push` custom_template, and the coach
# repeatedly told him it "didn't have the exercises off the old card" — a lie, the
# sets are right here in SetLog). These helpers rebuild what a user ACTUALLY did on a
# given day from their logged sets so the coach can reflect it back and offer to save
# it (save_routine). They only SELECT — they never write, never mutate a session.

# db/bb/ohp are how sets get logged tersely; expanded so "incline db press" and
# "incline dumbbell bench press" collapse to one lift.
_RECON_ABBREV = {"db": "dumbbell", "bb": "barbell", "ohp": "overhead press",
                 "sldl": "stiff leg deadlift", "rdl": "romanian deadlift"}
# Known garble seen in real logs ("single_are_tricep_pushdown" = "single arm …").
_RECON_GARBLE = {"are": "arm", "aer": "arm", "arn": "arm"}
# Pure noise words — dropped from a lift's signature. Kept deliberately SMALL: real
# movement words ("bench", "machine", "seated") distinguish lifts, so they stay.
_RECON_FILLER = {"the", "a", "an", "of", "x"}


def _recon_tokens(name: str) -> list[str]:
    import re
    out: list[str] = []
    for t in re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split():
        t = _RECON_GARBLE.get(t, t)
        out.extend(_RECON_ABBREV.get(t, t).split())  # abbrev may expand to 2 words
    return out


def _recon_sig(name: str) -> frozenset:
    return frozenset(t for t in _recon_tokens(name) if t not in _RECON_FILLER)


def _recon_head(name: str) -> str:
    """The positional lead token ('incline', 'overhead', 'single', 'bench') — used so a
    subset merge only fires between variants of the SAME movement family."""
    toks = [t for t in _recon_tokens(name) if t not in _RECON_FILLER]
    return toks[0] if toks else ""


def _recon_same_lift(sig_a: frozenset, head_a: str, sig_b: frozenset, head_b: str) -> bool:
    """Two logged names are the same lift when their signatures are equal, or one is a
    subset of the other differing by exactly ONE token, they share a lead token, and the
    smaller has >=2 tokens. That collapses 'incline dumbbell bench press' / 'incline db
    press' without swallowing 'bench press' into an incline slot."""
    if sig_a == sig_b:
        return True
    small, big = (sig_a, sig_b) if len(sig_a) <= len(sig_b) else (sig_b, sig_a)
    return (small < big and len(big) - len(small) == 1 and len(small) >= 2
            and head_a == head_b)


def _recon_garble_score(name: str) -> int:
    import re
    return sum(1 for t in re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
               if t in _RECON_GARBLE)


def _recon_clean_label(label: str, slug: str) -> str:
    """Display form: fix whole-word garble, unify separators, lowercase — never leave
    'single_are_tricep_pushdown' in front of the user."""
    import re
    base = re.sub(r"[_]+", " ", (label or slug or "").strip().lower())
    words = [_RECON_GARBLE.get(w, w) for w in base.split()]
    return re.sub(r"\s+", " ", " ".join(words)).strip() or "exercise"


def completed_template_keys(user_id: int) -> list[str]:
    """Distinct template_keys the user has a DONE session for, newest activity first.
    Cheap DISTINCT — feeds the PRIOR SESSIONS context note. Read-only, fail-open."""
    session = get_session()
    try:
        rows = (session.query(WorkoutSession.template_key)
                .filter(WorkoutSession.user_id == user_id,
                        WorkoutSession.status == "done",
                        WorkoutSession.template_key.isnot(None))
                .order_by(WorkoutSession.id.desc()).all())
        seen: list[str] = []
        for (k,) in rows:
            if k and k not in seen:
                seen.append(k)
        return seen
    except Exception as e:  # noqa: BLE001 — a context note must never break the turn
        logger.warning("COMPLETED_TEMPLATE_KEYS_FAILED user=%s err=%s", user_id, e)
        return []
    finally:
        session.close()


def reconstruct_from_history(user_id: int, template_key: str | None = None) -> dict:
    """Rebuild what the user actually did on their most recent DONE session for
    `template_key` (or, if none matches / none given, their most recent done session
    of any key). READ-ONLY: only SELECTs run.

    Returns one of:
      {"status": "none", "requested_key": <norm key or None>}
      {"status": "ok"|"fallback", "template_key", "requested_key", "date_label",
       "exercises": [{"label": str, "sets": int}, ...]}
    'fallback' means the requested day had no session so a different day was used.
    """
    from sqlalchemy import func
    from workouts.templates import normalize_template_key, day_key_from_phrase

    raw = (template_key or "").strip()
    key = None
    if raw:
        key = normalize_template_key(raw) or day_key_from_phrase(raw)

    session = get_session()
    try:
        base = (session.query(WorkoutSession)
                .filter(WorkoutSession.user_id == user_id,
                        WorkoutSession.status == "done"))
        # Newest by when it was finished, falling back to the session date.
        recency = func.coalesce(WorkoutSession.finished_at, WorkoutSession.date).desc()

        ws = None
        used_fallback = False
        if key:
            ws = (base.filter(WorkoutSession.template_key == key)
                  .order_by(recency, WorkoutSession.id.desc()).first())
        if ws is None:
            ws = base.order_by(recency, WorkoutSession.id.desc()).first()
            used_fallback = ws is not None and bool(key)
        if ws is None:
            return {"status": "none", "requested_key": key}

        sets = (session.query(SetLog)
                .filter(SetLog.session_id == ws.id)
                .order_by(SetLog.set_index, SetLog.id).all())

        # Distinct exercises in first-appearance order, collapsing noisy variants:
        # two names merge when either signature is a subset of the other (after
        # abbrev-expansion + garble-fix + filler-drop). Representative label = the
        # cleanest (fewest garble tokens, then shortest).
        groups: list[dict] = []
        for s in sets:
            src = s.exercise_label or s.exercise
            sig = _recon_sig(src)
            if not sig:
                continue
            head = _recon_head(src)
            match = None
            for g in groups:
                if _recon_same_lift(sig, head, g["sig"], g["head"]):
                    match = g
                    break
            label = _recon_clean_label(s.exercise_label, s.exercise)
            score = (_recon_garble_score(s.exercise_label or s.exercise), len(label))
            if match is None:
                groups.append({"sig": sig, "head": head, "sets": 1, "label": label, "score": score})
            else:
                match["sig"] = match["sig"] | sig  # widen so later variants still catch
                match["sets"] += 1
                if score < match["score"]:
                    match["label"] = label
                    match["score"] = score

        when = ws.finished_at or ws.date
        date_label = when.strftime("%a %m-%d") if when else None
        return {
            "status": "fallback" if used_fallback else "ok",
            "template_key": ws.template_key,
            "requested_key": key,
            "date_label": date_label,
            "exercises": [{"label": g["label"], "sets": g["sets"]} for g in groups],
        }
    finally:
        session.close()
