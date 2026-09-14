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


def build_state(session, ws: WorkoutSession) -> dict:
    from workouts.prs import check_pr
    sets = session.query(SetLog).filter(SetLog.session_id == ws.id).order_by(SetLog.id).all()
    exercises, order = {}, []
    volume, done_count = 0, 0
    for s in sets:
        if s.exercise not in exercises:
            exercises[s.exercise] = {"slug": s.exercise, "label": s.exercise_label or s.exercise, "sets": []}
            order.append(s.exercise)
        edited = bool(s.done and s.actual_weight is not None and s.actual_reps is not None
                      and (float(s.actual_weight) != float(s.planned_weight or 0) or int(s.actual_reps) != int(s.planned_reps or 0)))
        pr = None
        if s.done and s.actual_weight and s.actual_reps:
            volume += int(round(float(s.actual_weight) * int(s.actual_reps)))
            done_count += 1
            p = check_pr(session, ws.user_id, s.exercise, float(s.actual_weight), int(s.actual_reps), exclude_session_id=ws.id)
            pr = p.message if p else None
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
    from workouts.prs import check_pr
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
        pr = check_pr(session, ws.user_id, set_row.exercise, float(set_row.actual_weight), int(set_row.actual_reps),
                      exclude_session_id=ws.id)
        return pr.message if pr else None
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
    if went_active or pr:
        # The bubble's captions follow the session: first set → "in progress";
        # a PR is worth showing in the thread too. Best-effort, off the request path.
        from workouts.card import refresh_card_async
        refresh_card_async(session_id)
    return jsonify({"ok": True, "pr": pr, **state})


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
