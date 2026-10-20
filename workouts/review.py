"""
The coach's reasoning around a session — the part a lookup table can't do.

Founder, 2026-10-18: the program builder gave the structure, "but it's still missing the
knowledge and responsiveness of a real coach." Two moments, both model-written over
code-assembled facts, with code keeping the numbers safe:

  post_session_review(session_id)  — after the code summary: what moved, what to change
      next time, one cue. The model also returns per-exercise NEXT targets with reasons;
      code bounds them (≤ 2 plate steps / ±20%) and stores them on users.training_decisions,
      which plan.next_targets honours ONCE for the next session of each exercise — so "start
      leg press at 155 next time" is the number on the next card, not a sentence.
  pre_session_plan(session_id)     — when a card goes out (not the onboarding setup card):
      the targets with their reasons (history / a review decision / readiness), one cue for
      a movement they've never done, a demo link when one exists, and a readiness note from
      the wearable. Replaces the generic intro line when it succeeds; the intro line is the
      fallback.

Both read prompts/training_knowledge.md (cached) with identity + voice. Mid-session
reactions on card events are deliberately NOT here (founder: hold off).
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

import config
from models import get_session, User, WorkoutSession, SetLog

logger = logging.getLogger("cued.review")

REVIEW_TYPE = "workout_review"
PLAN_TYPE = "workout_intro"
_KNOWLEDGE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts", "training_knowledge.md")
_knowledge_cache: str | None = None
MAX_JUMP_PCT = 0.20
MAX_STEPS_UP = 2


def training_knowledge() -> str:
    global _knowledge_cache
    if _knowledge_cache is None:
        with open(_KNOWLEDGE_PATH, "r", encoding="utf-8") as f:
            _knowledge_cache = f.read()
    return _knowledge_cache


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt(w) -> str:
    return f"{float(w):g}"


# ─── facts ───────────────────────────────────────────────────────────────────

def exercise_history(session, user_id: int, slug: str, *, exclude_session_id=None, limit: int = 3) -> list[dict]:
    """Last `limit` sessions with done sets of this exercise: [{date, sets: [(w, r)], best}]."""
    q = (session.query(WorkoutSession.id, WorkoutSession.date, WorkoutSession.finished_at)
         .join(SetLog, SetLog.session_id == WorkoutSession.id)
         .filter(WorkoutSession.user_id == user_id, SetLog.exercise == slug, SetLog.done.is_(True)))
    if exclude_session_id:
        q = q.filter(WorkoutSession.id != exclude_session_id)
    rows = q.distinct().order_by(WorkoutSession.date.desc(), WorkoutSession.id.desc()).limit(limit).all()
    out = []
    for sid, date, finished_at in rows:
        sets = [(float(s.actual_weight or 0), int(s.actual_reps or 0)) for s in
                session.query(SetLog).filter(SetLog.session_id == sid, SetLog.exercise == slug, SetLog.done.is_(True))
                .order_by(SetLog.set_index).all() if s.actual_reps]
        if sets:
            out.append({"date": (finished_at or date).strftime("%b %-d"), "sets": sets, "best": max(sets)})
    return out


def session_facts(session_id: int) -> dict | None:
    """Everything the review needs, from the DB: per-exercise plan vs done, history, PRs,
    readiness, the program, first-session flag."""
    from workouts.summary import summarize
    from workouts.program import program_context
    session = get_session()
    try:
        ws = session.get(WorkoutSession, session_id)
        if not ws:
            return None
        user = session.get(User, ws.user_id)
        s = summarize(session, ws)
        rows = session.query(SetLog).filter(SetLog.session_id == ws.id).order_by(SetLog.id).all()
        order, by_ex = [], {}
        for r in rows:
            if r.exercise not in by_ex:
                by_ex[r.exercise] = {"slug": r.exercise, "label": r.exercise_label or r.exercise, "planned": [], "done": [], "sources": set()}
                order.append(r.exercise)
            e = by_ex[r.exercise]
            e["planned"].append((float(r.planned_weight or 0), int(r.planned_reps or 0)))
            if r.done and r.actual_reps:
                e["done"].append((float(r.actual_weight or 0), int(r.actual_reps)))
                e["sources"].add(r.source or "")
        exercises = []
        for slug in order:
            e = by_ex[slug]
            planned_w = max((w for w, _ in e["planned"]), default=0.0)
            planned_r = max((r for _, r in e["planned"]), default=0)
            best = max(e["done"], default=None)
            hit_all = bool(e["done"]) and len(e["done"]) >= len(e["planned"]) and all(r >= planned_r for _, r in e["done"])
            exercises.append({
                "slug": slug, "label": e["label"], "planned_weight": planned_w, "planned_reps": planned_r,
                "sets_planned": len(e["planned"]), "done": e["done"], "best": best,
                "skipped": not e["done"], "hit_all": hit_all,
                "missed_reps": any(r < planned_r for _, r in e["done"]) if e["done"] else False,
                "jumped": bool(best) and best[0] > planned_w > 0,
                "history": exercise_history(session, ws.user_id, slug, exclude_session_id=ws.id),
                "is_new": not exercise_history(session, ws.user_id, slug, exclude_session_id=ws.id, limit=1),
                "sources": sorted(x for x in e["sources"] if x),
            })
        n_done = session.query(WorkoutSession.id).filter(WorkoutSession.user_id == ws.user_id, WorkoutSession.status == "done").count()
        readiness = _readiness(user, session)
        return {
            "session_id": ws.id, "user_id": ws.user_id, "name": user.name if user else "",
            "key": ws.template_key, "status": ws.status, "minutes": s["minutes"], "volume_lb": s["volume_lb"],
            "prs": s["prs"], "sets_done": s["sets_done"], "sets_planned": s["sets_planned"],
            "n_done_sessions": n_done, "first": n_done <= 1 and ws.status == "done",
            "exercises": exercises, "readiness": readiness,
            "program": program_context(user) if user else "",
            "level": (getattr(user, "experience", None) or "beginner"), "goal": getattr(user, "goal", None) or "",
            "injuries": getattr(user, "injuries", None) or "", "equipment": getattr(user, "equipment", None) or "",
            "decisions": dict(getattr(user, "training_decisions", None) or {}),
        }
    finally:
        session.close()


def _readiness(user, session) -> dict:
    try:
        from wearable_read import recovery_read
        r = recovery_read(user, session)
    except Exception:  # noqa: BLE001
        r = None
    if not r or not r.fresh:
        return {}
    out = {}
    if r.sleep_minutes is not None:
        out["sleep_last_night_h"] = round(r.sleep_minutes / 60, 1)
        out["poor_night"] = r.poor_night
    if r.rhr_worse or r.hrv_worse:
        out["poor_recovery"] = True
    return out


def facts_text(f: dict, *, for_plan: bool = False) -> str:
    lines = [f"## SESSION {'PLAN' if for_plan else 'FACTS'} (code-assembled — every number here is real)",
             f"- day: {f['key']} · level: {f['level']} · goal: {f['goal'] or 'n/a'} · equipment: {f['equipment'] or 'gym'}"
             + (f" · injuries: {f['injuries'][:80]}" if f['injuries'] else "")]
    if not for_plan:
        lines.append(f"- done: {f['sets_done']}/{f['sets_planned']} sets · {f['minutes']} min · {f['volume_lb']:,} lb"
                     + (" · FIRST EVER SESSION" if f["first"] else f" · session #{f['n_done_sessions']}"))
        if f["prs"]:
            lines.append("- PRs: " + "; ".join(f["prs"]))
    for e in f["exercises"]:
        plan = f"{_fmt(e['planned_weight'])}×{e['planned_reps']}×{e['sets_planned']}" if e["planned_weight"] else f"{e['planned_reps']}×{e['sets_planned']}"
        if for_plan:
            src = ", ".join(e["sources"]) if e["sources"] else ""
            hist = "; ".join(f"{h['date']}: " + " · ".join(f"{_fmt(w)}×{r}" for w, r in h["sets"]) for h in e["history"][:2]) or "never done it"
            why = ""
            d = (f.get("decisions") or {}).get(e["slug"]) or {}
            if d and not d.get("consumed"):
                why = f" · from the last review: {d.get('why', '')}"
            lines.append(f"- {e['label']}: plan {plan}{why} · history: {hist}" + (" · NEW TO THEM" if e["is_new"] else ""))
        else:
            done = " · ".join(f"{_fmt(w)}×{r}" if w else f"{r}" for w, r in e["done"]) or "skipped"
            flags = [k for k in ("hit_all", "missed_reps", "jumped", "skipped") if e[k]]
            hist = "; ".join(f"{h['date']}: best {_fmt(h['best'][0])}×{h['best'][1]}" for h in e["history"][:3]) or "first time"
            lines.append(f"- {e['label']}: plan {plan} → did {done}" + (f" [{', '.join(flags)}]" if flags else "")
                         + f" · before: {hist}" + (" · NEW TO THEM" if e["is_new"] else ""))
    rd = f.get("readiness") or {}
    if rd:
        lines.append("- readiness: " + ", ".join(f"{k}={v}" for k, v in rd.items()))
    if f.get("program"):
        lines.append(f["program"])
    return "\n".join(lines)


# ─── the model call ──────────────────────────────────────────────────────────

def _system(user_name: str, tz_name: str | None) -> list:
    from agent_loop import identity_prompt, _voice_prompt
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name or "America/Los_Angeles")
    now_local = datetime.now(tz).strftime("%A %-I:%M%p").replace("AM", "am").replace("PM", "pm")
    return [
        {"type": "text", "text": identity_prompt() + "\n\n---\n\n" + _voice_prompt(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": training_knowledge(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"Their name: {user_name}. It's {now_local} their time."},
    ]


def _call(user_id: int, site: str, system: list, instruction: str, *, max_tokens: int = 900):
    from agent_loop import _join_text
    from cost_tracking import track
    from llm_client import make_client
    client = make_client()
    resp = client.messages.create(model=config.AGENT_LOOP_MODEL, max_tokens=max_tokens, system=system,
                                  messages=[{"role": "user", "content": instruction}])
    try:
        track(user_id, site, config.AGENT_LOOP_MODEL, resp)
    except Exception as e:  # noqa: BLE001
        logger.warning("REVIEW_COST_TRACK_FAILED user=%s err=%s", user_id, e)
    if getattr(resp, "stop_reason", None) == "max_tokens":
        logger.warning("REVIEW_TRUNCATED user=%s site=%s", user_id, site)
        return None
    return (_join_text(resp.content) or "").strip()


def _parse_json(text: str) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
        return d if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


# ─── post-session review ─────────────────────────────────────────────────────

REVIEW_INSTRUCTION = """They just closed the session below; the code summary (every set, the total) already went out — do not repeat it.
Write the REVIEW a real coach would text right after: what actually moved (name the lift and the numbers), ONE thing to change next time and why, and at most one cue for a movement that's new to them. Honest about a starting weight that was clearly too light (they hit every rep easily → a bigger jump next time) or too heavy (missed reps → hold or drop). Readiness (short night) explains a flat session; say so once. Nothing about hydration, nothing generic, no "great job".
Return ONLY JSON:
{"bubbles": ["...", "..."],                                   // 1–3 bubbles in your voice, each under 220 chars, lowercase, no emoji
 "next": {"<exercise slug>": {"weight": <number>, "reps": <number>, "why": "<under 90 chars>"}},   // only lifts whose NEXT target should differ from the default +1 step; weight in lb (stack/plate loaded), omit to leave the default
 "note": "<one line for your own records, or empty>"}
Slugs are the keys shown in brackets below. Never set a weight more than ~20% / 2 plate steps above what they actually did; never below 50% of it."""


def post_session_review(session_id: int) -> dict | None:
    """Model-written review after the code summary; decisions bounded + stored. Returns
    {"bubbles": [...], "decisions": {...}} or None (nothing sent — the caller's fallbacks stand)."""
    if not getattr(config, "SESSION_REVIEW_ENABLED", True):
        return None
    try:
        f = session_facts(session_id)
        if not f or f["status"] != "done" or f["sets_done"] == 0:
            return None
        session = get_session()
        try:
            u = session.get(User, f["user_id"])
            phone, tz_name = u.phone, u.user_timezone
        finally:
            session.close()
        slugs = ", ".join(f"[{e['slug']}]" for e in f["exercises"])
        instruction = f"{REVIEW_INSTRUCTION}\n\nslugs: {slugs}\n\n{facts_text(f)}"
        raw = _call(f["user_id"], "workout.review", _system(f["name"], tz_name), instruction)
        d = _parse_json(raw or "")
        if not d or not isinstance(d.get("bubbles"), list):
            logger.warning("REVIEW_UNPARSEABLE user=%s session=%s raw=%r", f["user_id"], session_id, (raw or "")[:160])
            return None
        bubbles = [str(b).strip() for b in d["bubbles"] if str(b).strip()][:3]
        if not bubbles:
            return None
        decisions = _bound_decisions(f, d.get("next") or {})
        if decisions:
            store_decisions(f["user_id"], decisions, session_id=session_id)
        from sms import send_sms
        send_sms(phone, "\n---\n".join(bubbles), user_id=f["user_id"], message_type=REVIEW_TYPE)
        logger.info("REVIEW_SENT user=%s session=%s bubbles=%d decisions=%s note=%r", f["user_id"], session_id,
                    len(bubbles), {k: (v["weight"], v["reps"]) for k, v in decisions.items()}, str(d.get("note") or "")[:80])
        return {"bubbles": bubbles, "decisions": decisions, "note": d.get("note")}
    except Exception as e:  # noqa: BLE001 — the summary already went out; the review is extra
        logger.warning("REVIEW_FAILED session=%s err=%s", session_id, e, exc_info=True)
        return None


def _bound_decisions(f: dict, nxt: dict) -> dict:
    """Keep the model's next targets inside what the data supports."""
    from workouts.templates import plate_step_for_slug
    by_slug = {e["slug"]: e for e in f["exercises"]}
    out = {}
    for slug, v in (nxt or {}).items():
        e = by_slug.get(slug)
        if not e or not isinstance(v, dict):
            continue
        try:
            w = float(v.get("weight")) if v.get("weight") is not None else None
            r = int(v.get("reps")) if v.get("reps") is not None else e["planned_reps"]
        except (TypeError, ValueError):
            continue
        base = e["best"][0] if e["best"] else e["planned_weight"]
        if w is None or base is None:
            continue
        step = plate_step_for_slug(slug) or 5.0
        if base:
            # snap to THEIR grid (base ± k steps): at most +2 steps and +20%, never under half
            k = int(round((w - base) / step))
            k = min(k, MAX_STEPS_UP)
            # one plate step is always allowed (a 20-lb cable fly can't move 20% without it);
            # the percentage cap governs the second step
            while k > 1 and base + k * step > base * (1 + MAX_JUMP_PCT) + 1e-9:
                k -= 1
            w = base + k * step
            if w < base * 0.5:
                import math
                w = base + math.ceil((base * 0.5 - base) / step) * step
        r = max(3, min(20, r))
        out[slug] = {"weight": float(w), "reps": int(r), "why": str(v.get("why") or "")[:120]}
    return out


def store_decisions(user_id: int, decisions: dict, *, session_id: int | None = None) -> None:
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        u = session.query(User).filter(User.id == user_id).with_for_update().one_or_none()
        if not u:
            return
        cur = dict(u.training_decisions or {})
        for slug, v in decisions.items():
            cur[slug] = {**v, "session_id": session_id, "set_at": _utcnow().isoformat(timespec="seconds"), "consumed": False}
        u.training_decisions = cur
        flag_modified(u, "training_decisions")
        session.commit()
        logger.info("TRAINING_DECISIONS_STORED user=%s %s", user_id, {k: (v["weight"], v["reps"]) for k, v in decisions.items()})
    finally:
        session.close()


def pending_decision(user, slug: str) -> dict | None:
    d = (getattr(user, "training_decisions", None) or {}).get(slug)
    return d if d and not d.get("consumed") else None


def consume_decisions(user_id: int, slugs) -> None:
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        u = session.query(User).filter(User.id == user_id).with_for_update().one_or_none()
        if not u or not u.training_decisions:
            return
        cur = dict(u.training_decisions)
        changed = False
        for s in slugs:
            if s in cur and not cur[s].get("consumed"):
                cur[s] = {**cur[s], "consumed": True}
                changed = True
        if changed:
            u.training_decisions = cur
            flag_modified(u, "training_decisions")
            session.commit()
    finally:
        session.close()


# ─── pre-session plan ────────────────────────────────────────────────────────

PLAN_INSTRUCTION = """Their card for today is going out right after this text — the numbers on it are set (code: their history, the last review's decisions, calibration). Write the 1–2 bubbles a coach texts with the card: the plan in their words — which lifts are moving up and why (name the number), what to hold, ONE cue for a movement that's NEW TO THEM (never for one they've done), and the readiness call if there's a short night (lighter today, the plan's the plan next time). If a demo link is listed, include it on its own for that movement. No greeting, no hype, no list of every exercise, no hydration. Under 2 bubbles, each under 240 chars, lowercase, no emoji.
Return ONLY JSON: {"bubbles": ["...", "..."]}"""


def pre_session_plan(session_id: int, *, first: bool = False, estimated: bool = False) -> str | None:
    """The intro text for a card going out, model-written over the plan facts. None → the
    caller uses the generic intro line."""
    if not getattr(config, "PRE_SESSION_PLAN_ENABLED", True):
        return None
    try:
        f = session_facts(session_id)
        if not f:
            return None
        session = get_session()
        try:
            u = session.get(User, f["user_id"])
            tz_name = u.user_timezone
            demos = {}
            try:
                from exercise_demos import unseen_demos_for, mark_demos_seen, _resolve
                new_names = [e["label"] for e in f["exercises"] if e["is_new"]]
                demos = unseen_demos_for(u, new_names, limit=1)
                if demos:
                    mark_demos_seen(u.id, [_resolve(n) for n in demos])
            except Exception:  # noqa: BLE001
                demos = {}
        finally:
            session.close()
        extra = ""
        if first:
            extra += "\n- this is their FIRST card" + (" and the weights are calibrated guesses from their stats, not history — say the numbers are a starting guess and the point is to move them" if estimated else "")
        if demos:
            extra += "\n- demo link for a new movement: " + "; ".join(f"{n}: {url}" for n, url in demos.items())
        instruction = f"{PLAN_INSTRUCTION}\n\n{facts_text(f, for_plan=True)}{extra}"
        raw = _call(f["user_id"], "workout.plan", _system(f["name"], tz_name), instruction, max_tokens=600)
        d = _parse_json(raw or "")
        bubbles = [str(b).strip() for b in (d or {}).get("bubbles", []) if str(b).strip()][:2] if d else []
        if not bubbles:
            logger.warning("PLAN_UNPARSEABLE user=%s session=%s raw=%r", f["user_id"], session_id, (raw or "")[:160])
            return None
        logger.info("PLAN_TEXT user=%s session=%s bubbles=%d demos=%s", f["user_id"], session_id, len(bubbles), list(demos))
        return "\n---\n".join(bubbles)
    except Exception as e:  # noqa: BLE001
        logger.warning("PLAN_FAILED session=%s err=%s", session_id, e, exc_info=True)
        return None
