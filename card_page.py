"""
The workout card — Phase 2. A chromeless page rendered inside an iMessage bubble
(the Spectrum launcher's WebView). No login: a signed, expiring token in the URL
names (user, session). Taps and inline edits POST to /card/api/*; the page polls
so a text deviation (Phase 4) shows up without a resend.

The page itself is on the site (cued-site card.html, design tokens shared with
profile.html); this module is the token + the JSON API behind it. The bubble in
the thread is a static preview refreshed in place (workouts/card.py).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, make_response

import config
from models import get_session, User, WorkoutSession, SetLog

logger = logging.getLogger("cued.card")

card_bp = Blueprint("card", __name__)

TOKEN_TTL_S = 24 * 3600
_TOKEN_BYTES = 18


# ─── token ───────────────────────────────────────────────────────────────────

def _secret() -> bytes:
    return (config.CARD_TOKEN_SECRET or config.PROFILE_TOKEN_SECRET or config.FLASK_SECRET_KEY).encode("utf-8")


def _mac(user_id: int, session_id: int, exp: int) -> str:
    digest = hmac.new(_secret(), f"card:{user_id}:{session_id}:{exp}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:_TOKEN_BYTES]).decode("ascii").rstrip("=")


def card_token(user_id: int, session_id: int, *, exp: int | None = None, ttl_s: int = TOKEN_TTL_S) -> str:
    exp = int(exp if exp is not None else time.time() + ttl_s)
    return f"{int(user_id)}.{int(session_id)}.{exp}.{_mac(int(user_id), int(session_id), exp)}"


def verify_card_token(token: str | None, *, now: float | None = None) -> tuple[int, int] | None:
    """(user_id, session_id) or None — expired, tampered, malformed all → None."""
    if not token or token.count(".") != 3:
        return None
    u, s, e, mac = token.split(".")
    if not (u.isdigit() and s.isdigit() and e.isdigit()) or len(u) > 12 or len(s) > 12 or len(e) > 12:
        return None
    if int(e) < (now if now is not None else time.time()):
        return None
    if not hmac.compare_digest(mac, _mac(int(u), int(s), int(e))):
        return None
    return int(u), int(s)


def card_url(user_id: int, session_id: int, *, version: int | None = None) -> str:
    """The link users see: the site's card page (cued.fit/card.html?t=…), like
    profile.html. `v` busts the extension's cache on in-place updates."""
    url = f"{config.CARD_PAGE_URL}?t={card_token(user_id, session_id)}"
    return f"{url}&v={version}" if version else url


# ─── state ───────────────────────────────────────────────────────────────────

def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt(w) -> str:
    return f"{float(w):g}" if w is not None else ""


def _prior_by_exercise(session, user_id: int, session_id: int, exercises: list[str]) -> dict:
    """One query: (weight, reps) of every done set in PREVIOUS sessions, grouped by
    exercise — so state building is O(1) queries instead of one per set."""
    from models import WorkoutSession as WS
    if not exercises:
        return {}
    q = (session.query(SetLog.exercise, SetLog.actual_weight, SetLog.actual_reps)
         .join(WS, SetLog.session_id == WS.id)
         .filter(WS.user_id == user_id, SetLog.session_id != session_id, SetLog.done.is_(True),
                 SetLog.exercise.in_(exercises), SetLog.actual_weight.isnot(None), SetLog.actual_reps.isnot(None)))
    out: dict = {}
    for ex, w, r in q.all():
        if w and r:
            out.setdefault(ex, []).append((float(w), int(r)))
    return out


def pr_for_set(prior: list[tuple[float, int]], session_best_before: tuple | None, weight: float, reps: int):
    """The badge/message rule: a set earns a PR when it beats PREVIOUS sessions
    (Epley e1RM, or more reps at the same weight) AND is a new best for TODAY
    (strictly heavier, or same weight and more reps, than every earlier done set
    of the exercise). Equal repeats — 185×5 three times — badge once, not thrice.
    Returns the message or None."""
    from workouts.prs import epley_1rm, PR
    if not prior or not weight or not reps:
        return None
    if session_best_before is not None and (weight, reps) <= session_best_before:
        return None
    best = max(prior, key=lambda p: epley_1rm(*p))
    if epley_1rm(weight, reps) > epley_1rm(*best) + 1e-9:
        return PR("", weight, reps, best[0], best[1], "e1rm").message
    same = [p for p in prior if p[0] == weight]
    if same:
        br = max(same, key=lambda p: p[1])
        if reps > br[1]:
            return PR("", weight, reps, br[0], br[1], "reps_at_weight").message
    return None


def build_state(session, ws: WorkoutSession) -> dict:
    sets = session.query(SetLog).filter(SetLog.session_id == ws.id).order_by(SetLog.id).all()
    exercises, order = {}, []
    for s in sets:
        if s.exercise not in exercises:
            exercises[s.exercise] = {"slug": s.exercise, "label": s.exercise_label or s.exercise, "sets": []}
            order.append(s.exercise)
    prior = _prior_by_exercise(session, ws.user_id, ws.id, order)
    best_so_far: dict = {}
    volume, done_count = 0, 0
    for s in sets:
        edited = bool(s.done and s.actual_weight is not None and s.actual_reps is not None
                      and (float(s.actual_weight) != float(s.planned_weight or 0) or int(s.actual_reps) != int(s.planned_reps or 0)))
        pr = None
        if s.done and s.actual_weight and s.actual_reps:
            w, r = float(s.actual_weight), int(s.actual_reps)
            volume += int(round(w * r))
            done_count += 1
            pr = pr_for_set(prior.get(s.exercise, []), best_so_far.get(s.exercise), w, r)
            if best_so_far.get(s.exercise) is None or (w, r) > best_so_far[s.exercise]:
                best_so_far[s.exercise] = (w, r)
        exercises[s.exercise]["sets"].append({
            "id": s.id, "index": s.set_index,
            "planned_weight": s.planned_weight, "planned_reps": s.planned_reps,
            "actual_weight": s.actual_weight, "actual_reps": s.actual_reps,
            "done": bool(s.done), "edited": edited, "source": s.source, "pr": pr,
        })
    when = ws.date or ws.started_at or _utcnow()
    return {
        "session": {"id": ws.id, "template_key": ws.template_key, "status": ws.status,
                    "weekday": when.strftime("%a").lower(), "started_at": ws.started_at.isoformat() if ws.started_at else None,
                    "finished_at": ws.finished_at.isoformat() if ws.finished_at else None},
        "exercises": [exercises[k] for k in order],
        "volume_lb": volume, "done_count": done_count, "set_count": len(sets),
        "updated_at": _utcnow().isoformat(),
    }


def _auth() -> tuple[int, int] | None:
    tok = (request.headers.get("Authorization") or "").replace("Bearer ", "").strip() or request.args.get("t")
    return verify_card_token(tok)


def _load(session, ids) -> WorkoutSession | None:
    user_id, session_id = ids
    ws = session.get(WorkoutSession, session_id)
    if not ws or ws.user_id != user_id:
        return None
    return ws


# ─── routes ──────────────────────────────────────────────────────────────────

@card_bp.route("/card/workout/<token>", methods=["GET"])
def card_workout_page(token):
    """Legacy/dev entry: the page lives on the site now. Valid token → redirect to
    the site page with the token; bad/expired → 401 (no info leak)."""
    from flask import redirect
    ids = verify_card_token(token)
    if not ids:
        return make_response("link expired", 401)
    return redirect(f"{config.CARD_PAGE_URL}?t={token}", code=302)


@card_bp.route("/card/api/session", methods=["GET"])
def card_api_session():
    ids = _auth()
    if not ids:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    session = get_session()
    try:
        ws = _load(session, ids)
        if not ws:
            return jsonify({"ok": False, "error": "not found"}), 404
        return jsonify({"ok": True, **build_state(session, ws)})
    finally:
        session.close()


def apply_set_update(session, ws: WorkoutSession, set_row: SetLog, *, done: bool | None,
                     actual_weight=None, actual_reps=None, source: str) -> dict | None:
    """The one write path for a set (card tap/edit now; text + tapback in Phase 4).
    Returns the PR (message) if this update made one, else None."""
    now = _utcnow()
    if actual_weight is not None:
        set_row.actual_weight = float(actual_weight)
    if actual_reps is not None:
        set_row.actual_reps = int(actual_reps)
    if done is None:
        done = True
    set_row.done = bool(done)
    if set_row.done:
        if set_row.actual_weight is None:
            set_row.actual_weight = set_row.planned_weight
        if set_row.actual_reps is None:
            set_row.actual_reps = set_row.planned_reps
        set_row.done_at, set_row.source = now, source
        if ws.status in ("planned", None):
            ws.status = "active"
        if not ws.started_at:
            ws.started_at = now
    else:
        set_row.done_at, set_row.source = None, None
        set_row.actual_weight, set_row.actual_reps = None, None
    session.commit()
    if set_row.done and set_row.actual_weight and set_row.actual_reps:
        w, r = float(set_row.actual_weight), int(set_row.actual_reps)
        prior = _prior_by_exercise(session, ws.user_id, ws.id, [set_row.exercise]).get(set_row.exercise, [])
        earlier = [(float(x.actual_weight), int(x.actual_reps)) for x in
                   session.query(SetLog).filter(SetLog.session_id == ws.id, SetLog.exercise == set_row.exercise,
                                                SetLog.done.is_(True), SetLog.id != set_row.id).all()
                   if x.actual_weight and x.actual_reps and x.done_at and set_row.done_at and x.done_at <= set_row.done_at]
        best_before = max(earlier) if earlier else None
        return pr_for_set(prior, best_before, w, r)
    return None


@card_bp.route("/card/api/set/<int:set_id>", methods=["POST"])
def card_api_set(set_id):
    ids = _auth()
    if not ids:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    d = request.get_json(silent=True) or {}
    session = get_session()
    try:
        ws = _load(session, ids)
        row = session.get(SetLog, set_id)
        if not ws or not row or row.session_id != ws.id:
            return jsonify({"ok": False, "error": "not found"}), 404
        if ws.status in ("done", "abandoned"):
            return jsonify({"ok": False, "error": "session closed"}), 409
        try:
            aw = float(d["actual_weight"]) if d.get("actual_weight") not in (None, "") else None
            ar = int(d["actual_reps"]) if d.get("actual_reps") not in (None, "") else None
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "weight/reps must be numbers"}), 400
        was_planned = ws.status in ("planned", None)
        pr = apply_set_update(session, ws, row, done=d.get("done"), actual_weight=aw, actual_reps=ar, source="card")
        logger.info("CARD_SET user=%s session=%s set=%s done=%s w=%s r=%s pr=%s",
                    ws.user_id, ws.id, row.id, row.done, row.actual_weight, row.actual_reps, bool(pr))
        state = build_state(session, ws)
        session_id, went_active = ws.id, (was_planned and ws.status == "active")
    finally:
        session.close()
    if went_active:
        # The bubble's captions follow the session: first set → "in progress".
        # (Not on every PR — each edit is a Photon round trip and a thread event.)
        from workouts.card import refresh_card_async
        refresh_card_async(session_id)
    return jsonify({"ok": True, "pr": pr, **state})


def _next_plan_for(session, ws: WorkoutSession, exercise: str, label: str):
    """Planned (weight, reps) for a new set/exercise: this session's last set of it,
    else the user's history via the plan module, else the template default, else 0."""
    last = (session.query(SetLog).filter(SetLog.session_id == ws.id, SetLog.exercise == exercise)
            .order_by(SetLog.id.desc()).first())
    if last:
        return last.planned_weight, last.planned_reps
    from workouts.templates import TEMPLATES, label_for_slug
    from workouts.plan import next_targets
    tmpl = next((e for exs in TEMPLATES.values() for e in exs if e.slug == exercise), None)
    if tmpl:
        w, r, _ = next_targets(session, ws.user_id, tmpl)
        return w, r
    return None, None


@card_bp.route("/card/api/set", methods=["POST"])
def card_api_add_set():
    """Add one set to an exercise in this session ({exercise}), planned like its last set."""
    ids = _auth()
    if not ids:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    d = request.get_json(silent=True) or {}
    ex = (d.get("exercise") or "").strip()
    session = get_session()
    try:
        ws = _load(session, ids)
        if not ws:
            return jsonify({"ok": False, "error": "not found"}), 404
        if ws.status in ("done", "abandoned"):
            return jsonify({"ok": False, "error": "session closed"}), 409
        rows = session.query(SetLog).filter(SetLog.session_id == ws.id, SetLog.exercise == ex).order_by(SetLog.id).all()
        if not rows:
            return jsonify({"ok": False, "error": "unknown exercise in this session"}), 404
        last = rows[-1]
        session.add(SetLog(session_id=ws.id, exercise=ex, exercise_label=last.exercise_label,
                           set_index=(last.set_index or 0) + 1, planned_weight=last.planned_weight,
                           planned_reps=last.planned_reps, done=False))
        session.commit()
        logger.info("CARD_ADD_SET user=%s session=%s exercise=%s", ws.user_id, ws.id, ex)
        return jsonify({"ok": True, **build_state(session, ws)})
    finally:
        session.close()


@card_bp.route("/card/api/set/<int:set_id>", methods=["DELETE"])
def card_api_remove_set(set_id):
    ids = _auth()
    if not ids:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    session = get_session()
    try:
        ws = _load(session, ids)
        row = session.get(SetLog, set_id)
        if not ws or not row or row.session_id != ws.id:
            return jsonify({"ok": False, "error": "not found"}), 404
        if ws.status in ("done", "abandoned"):
            return jsonify({"ok": False, "error": "session closed"}), 409
        session.delete(row)
        session.commit()
        logger.info("CARD_REMOVE_SET user=%s session=%s set=%s", ws.user_id, ws.id, set_id)
        return jsonify({"ok": True, **build_state(session, ws)})
    finally:
        session.close()


@card_bp.route("/card/api/exercise", methods=["POST"])
def card_api_add_exercise():
    """Add an exercise to this session: {name, weight?, reps?, sets?}. Known names
    map to a template slug (history-planned); anything else is a free-text
    exercise planned at the given weight/reps."""
    ids = _auth()
    if not ids:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    d = request.get_json(silent=True) or {}
    name = (d.get("name") or "").strip().lower()[:60]
    if not name:
        return jsonify({"ok": False, "error": "name required"}), 400
    try:
        n_sets = max(1, min(int(d.get("sets") or 3), 10))
        w_in = float(d["weight"]) if d.get("weight") not in (None, "") else None
        r_in = int(d["reps"]) if d.get("reps") not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "sets/weight/reps must be numbers"}), 400
    from workouts.templates import slug_for_name, label_for_slug
    import re as _re
    slug = slug_for_name(name) or _re.sub(r"[^a-z0-9]+", "_", name).strip("_")[:40]
    label = label_for_slug(slug) if slug_for_name(name) else name
    session = get_session()
    try:
        ws = _load(session, ids)
        if not ws:
            return jsonify({"ok": False, "error": "not found"}), 404
        if ws.status in ("done", "abandoned"):
            return jsonify({"ok": False, "error": "session closed"}), 409
        if session.query(SetLog.id).filter(SetLog.session_id == ws.id, SetLog.exercise == slug).first():
            return jsonify({"ok": False, "error": "already in this session — add a set instead"}), 409
        pw, pr_ = _next_plan_for(session, ws, slug, label)
        weight = w_in if w_in is not None else pw
        reps = r_in if r_in is not None else pr_
        if weight is None or reps is None:
            return jsonify({"ok": False, "error": "weight and reps needed for a new exercise"}), 400
        for i in range(n_sets):
            session.add(SetLog(session_id=ws.id, exercise=slug, exercise_label=label, set_index=i,
                               planned_weight=weight, planned_reps=reps, done=False))
        session.commit()
        logger.info("CARD_ADD_EXERCISE user=%s session=%s exercise=%s sets=%s", ws.user_id, ws.id, slug, n_sets)
        return jsonify({"ok": True, **build_state(session, ws)})
    finally:
        session.close()


@card_bp.route("/card/api/exercise/<slug>", methods=["DELETE"])
def card_api_remove_exercise(slug):
    """Remove an exercise's UNDONE sets from this session (done sets stay — they happened)."""
    ids = _auth()
    if not ids:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    session = get_session()
    try:
        ws = _load(session, ids)
        if not ws:
            return jsonify({"ok": False, "error": "not found"}), 404
        if ws.status in ("done", "abandoned"):
            return jsonify({"ok": False, "error": "session closed"}), 409
        rows = session.query(SetLog).filter(SetLog.session_id == ws.id, SetLog.exercise == slug).all()
        if not rows:
            return jsonify({"ok": False, "error": "not found"}), 404
        removed = 0
        for r in rows:
            if not r.done:
                session.delete(r); removed += 1
        session.commit()
        logger.info("CARD_REMOVE_EXERCISE user=%s session=%s exercise=%s removed=%s", ws.user_id, ws.id, slug, removed)
        return jsonify({"ok": True, "removed": removed, **build_state(session, ws)})
    finally:
        session.close()


@card_bp.route("/card/api/finish", methods=["POST"])
def card_api_finish():
    ids = _auth()
    if not ids:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    session = get_session()
    try:
        ws = _load(session, ids)
        if not ws:
            return jsonify({"ok": False, "error": "not found"}), 404
        already = ws.status == "done"
        user_id, session_id = ws.user_id, ws.id
    finally:
        session.close()
    from workouts.summary import finish_session
    summary = finish_session(session_id)
    if not already:
        from workouts.close import send_session_summary
        from workouts.card import refresh_card
        def _close():
            refresh_card(session_id)          # bubble → "done — tap for the log"
            send_session_summary(session_id)  # then the summary text
        threading.Thread(target=_close, daemon=True).start()
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        state = build_state(session, ws)
    finally:
        session.close()
    logger.info("CARD_FINISH user=%s session=%s volume=%s prs=%s", user_id, session_id, summary["volume_lb"], summary["pr_count"])
    return jsonify({"ok": True, "summary": summary, **state})
