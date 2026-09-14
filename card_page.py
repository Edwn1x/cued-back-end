"""
The workout card — Phase 2. A chromeless page rendered inside an iMessage bubble
(the Spectrum launcher's WebView). No login: a signed, expiring token in the URL
names (user, session). Taps and inline edits POST to /card/api/*; the page polls
so a text deviation (Phase 4) shows up without a resend.

Layout facts from GATE 0 (2026-09-14, founder's phone): 300px wide, dark mode
on, the launcher's icon overlays the top-left ~36px, native checkboxes don't
render state — so the circles are drawn from JS state.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import threading
import time
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, make_response, render_template_string

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
    base = config.CARD_BASE_URL.rstrip("/")
    url = f"{base}/card/workout/{card_token(user_id, session_id)}"
    return f"{url}?v={version}" if version else url


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
    ids = verify_card_token(token)
    if not ids:
        return make_response("link expired", 401)
    session = get_session()
    try:
        ws = _load(session, ids)
        if not ws:
            return make_response("not found", 404)
        state = build_state(session, ws)
    finally:
        session.close()
    resp = make_response(render_template_string(CARD_HTML, state=state, token=token))
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
        pr = apply_set_update(session, ws, row, done=d.get("done"), actual_weight=aw, actual_reps=ar, source="card")
        logger.info("CARD_SET user=%s session=%s set=%s done=%s w=%s r=%s pr=%s",
                    ws.user_id, ws.id, row.id, row.done, row.actual_weight, row.actual_reps, bool(pr))
        state = build_state(session, ws)
        return jsonify({"ok": True, "pr": pr, **state})
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
        threading.Thread(target=send_session_summary, args=(session_id,), daemon=True).start()
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        state = build_state(session, ws)
    finally:
        session.close()
    logger.info("CARD_FINISH user=%s session=%s volume=%s prs=%s", user_id, session_id, summary["volume_lb"], summary["pr_count"])
    return jsonify({"ok": True, "summary": summary, **state})


# ─── the page ────────────────────────────────────────────────────────────────

CARD_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>{{ state.session.template_key }}</title>
<style>
  :root { color-scheme: light dark; --ink: #111; --ink2: #6b6b6b; --line: rgba(128,128,128,.28); --blue: #0a84ff; --bg2: rgba(128,128,128,.10); }
  @media (prefers-color-scheme: dark) { :root { --ink: #f2f2f2; --ink2: #9a9a9a; } }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  body { margin: 0; width: 300px; padding: 10px 12px 14px; background: transparent; color: var(--ink);
         font-family: -apple-system, system-ui, sans-serif; font-size: 16px; -webkit-user-select: none; user-select: none; }
  header { padding-left: 40px; min-height: 36px; display: flex; align-items: center; justify-content: space-between; }
  header h1 { font-size: 17px; font-weight: 600; margin: 0; text-transform: lowercase; }
  header .prog { font-size: 13px; color: var(--ink2); }
  .ex { margin-top: 10px; }
  .ex .name { font-size: 13px; color: var(--ink2); text-transform: lowercase; padding: 0 4px 4px; }
  .row { display: flex; align-items: center; justify-content: space-between; min-height: 44px; padding: 0 4px; border-top: 1px solid var(--line); }
  .row.editing { flex-wrap: wrap; }
  .nums { font-variant-numeric: tabular-nums; }
  .nums.edited::after { content: ""; display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--blue); margin-left: 6px; vertical-align: middle; }
  .circle { width: 26px; height: 26px; border-radius: 50%; border: 2px solid var(--ink2); flex: none; }
  .row.done .circle { background: var(--blue); border-color: var(--blue); }
  .pr { flex-basis: 100%; font-size: 13px; color: var(--blue); padding: 0 0 8px; }
  .edit { flex-basis: 100%; display: flex; gap: 8px; align-items: center; padding: 6px 0 10px; }
  .edit input { width: 76px; min-height: 40px; font: inherit; font-size: 16px; text-align: center; border: 1px solid var(--line); border-radius: 10px; background: var(--bg2); color: var(--ink); }
  .edit button, .finish { min-height: 40px; padding: 0 14px; border: 0; border-radius: 999px; background: var(--blue); color: #fff; font: inherit; font-size: 15px; font-weight: 600; }
  footer { margin-top: 12px; display: flex; align-items: center; justify-content: space-between; font-size: 14px; color: var(--ink2); min-height: 44px; }
  .finish { display: none; }
  .finish.show { display: inline-block; }
  .doneall { font-size: 14px; color: var(--ink2); padding: 8px 4px; }
</style></head>
<body>
<header><h1 id="title"></h1><div class="prog" id="prog"></div></header>
<div id="list"></div>
<footer><span id="vol"></span><button class="finish" id="finish" type="button">finish</button></footer>
<script>
(function(){
  const TOKEN = {{ token|tojson }};
  let state = {{ state|tojson }};
  let editing = null;          // set id being edited
  const API = { headers: { 'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json' } };
  const $ = (id) => document.getElementById(id);
  const fmt = (w) => (w === null || w === undefined) ? '' : String(Number(w));

  function render(){
    const s = state.session;
    $('title').textContent = s.template_key.replace('_',' ') + ' · ' + s.weekday;
    $('prog').textContent = state.done_count + '/' + state.set_count;
    const list = $('list'); list.innerHTML = '';
    for (const ex of state.exercises){
      const box = document.createElement('div'); box.className = 'ex';
      const name = document.createElement('div'); name.className = 'name'; name.textContent = ex.label; box.appendChild(name);
      for (const st of ex.sets){
        const row = document.createElement('div'); row.className = 'row' + (st.done ? ' done' : '') + (editing === st.id ? ' editing' : '');
        const nums = document.createElement('span'); nums.className = 'nums' + (st.edited ? ' edited' : '');
        const w = st.done ? st.actual_weight : st.planned_weight, r = st.done ? st.actual_reps : st.planned_reps;
        nums.textContent = fmt(w) + ' × ' + fmt(r);
        const circle = document.createElement('span'); circle.className = 'circle';
        row.appendChild(nums); row.appendChild(circle);
        if (st.pr && st.done){ const p = document.createElement('div'); p.className = 'pr'; p.textContent = st.pr; row.appendChild(p); }
        if (editing === st.id){
          const ed = document.createElement('div'); ed.className = 'edit';
          const iw = document.createElement('input'); iw.type = 'number'; iw.inputMode = 'decimal'; iw.value = fmt(st.actual_weight ?? st.planned_weight); iw.setAttribute('aria-label','weight');
          const ir = document.createElement('input'); ir.type = 'number'; ir.inputMode = 'numeric'; ir.value = fmt(st.actual_reps ?? st.planned_reps); ir.setAttribute('aria-label','reps');
          const ok = document.createElement('button'); ok.type = 'button'; ok.textContent = 'done';
          ok.addEventListener('click', (e) => { e.stopPropagation(); post(st.id, { done: true, actual_weight: iw.value, actual_reps: ir.value }); editing = null; });
          ed.appendChild(iw); ed.appendChild(ir); ed.appendChild(ok); row.appendChild(ed);
          setTimeout(() => iw.focus(), 0);
        }
        // tap = toggle at planned (or at the edited values); long-press or tapping the numbers = edit
        let timer = null, longed = false;
        row.addEventListener('touchstart', () => { longed = false; timer = setTimeout(() => { longed = true; editing = st.id; render(); }, 500); }, { passive: true });
        row.addEventListener('touchend', () => { clearTimeout(timer); });
        row.addEventListener('touchmove', () => { clearTimeout(timer); }, { passive: true });
        nums.addEventListener('click', (e) => { e.stopPropagation(); if (editing === st.id) return; editing = st.id; render(); });
        row.addEventListener('click', (e) => {
          if (longed || editing === st.id || e.target.closest('.edit')) return;
          post(st.id, { done: !st.done });
        });
        box.appendChild(row);
      }
      list.appendChild(box);
    }
    const any = state.done_count > 0;
    $('vol').textContent = any ? (state.volume_lb.toLocaleString() + ' lb so far') : '';
    const fin = $('finish');
    if (s.status === 'done'){ fin.classList.remove('show'); $('vol').textContent = state.volume_lb.toLocaleString() + ' lb · done'; }
    else fin.classList.toggle('show', any);
  }

  async function post(setId, body){
    try {
      const res = await fetch('/card/api/set/' + setId, { method: 'POST', headers: API.headers, body: JSON.stringify(body) });
      const data = await res.json();
      if (data && data.ok){ state = data; render(); }
    } catch (e) { /* offline: the next poll reconciles */ }
  }

  async function poll(){
    if (document.visibilityState !== 'visible' || editing !== null) return;
    try {
      const res = await fetch('/card/api/session', { headers: API.headers });
      const data = await res.json();
      if (data && data.ok && data.updated_at !== state.updated_at){ state = data; render(); }
    } catch (e) {}
  }

  $('finish').addEventListener('click', async () => {
    $('finish').disabled = true;
    try {
      const res = await fetch('/card/api/finish', { method: 'POST', headers: API.headers, body: '{}' });
      const data = await res.json();
      if (data && data.ok){ state = data; render(); }
    } catch (e) {} finally { $('finish').disabled = false; }
  });

  render();
  setInterval(poll, 5000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') poll(); });
})();
</script>
</body></html>"""
