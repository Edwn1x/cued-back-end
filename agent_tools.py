"""
Phase 3 — agent tools. The model REQUESTS; code VALIDATES and WRITES (state
writes stay code-mediated). Each tool is gated by its own flag in config; the
loop assembles the enabled set and dispatches tool_use blocks here.

Tool 1 — remember: the agent's memory write path, wrapping the Phase-1 primitives
(apply_facts add/update, invalidate_entry). Runs in parallel with the legacy
per-turn extraction until the recall eval shows parity; only then is extraction
retired.
"""

from __future__ import annotations

import logging
import re

from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm.attributes import flag_modified

import config

from models import (Message, get_session, User, Workout, Meal, Event, DiningMenuItem, active,
                    recompute_daily_totals, confirm_workout_today)
from memory import apply_facts, invalidate_entry, CATEGORIES

logger = logging.getLogger("cued.agent_tools")


REMEMBER_TOOL = {
    "name": "remember",
    "description": (
        "Save, update, or invalidate a DURABLE fact about the user (preferences, "
        "goals, schedule, constraints, life context). Use it when you learn "
        "something worth remembering across future conversations — not for "
        "transient chatter. A durable detail you read off an IMAGE counts the same "
        "as one they typed (a package weight, a label's macros, food on hand not "
        "yet eaten): save it this turn — the image is gone next turn, and a detail "
        "you only spoke back is not saved. CATEGORY ROUTING: groceries / food on "
        "hand not yet eaten go to 'food_on_hand' (transient inventory — it ages out "
        "automatically as it's eaten), NEVER to 'constraints'; 'constraints' is for "
        "injuries, medical issues, and hard limits only. When they say an "
        "injury/illness is over, save the recovered state — storage closes the "
        "superseded active states it replaces (audited). add: a new fact. update: "
        "supersede an existing fact (pass the new text; the old value is preserved "
        "in history). invalidate: close a fact that's no longer true (e.g. an "
        "injury healed, on-hand food now eaten and logged) — pass its entry_id. "
        "Write fact text with resolved absolute dates — 'tomorrow' → the actual "
        "date — because the fact will be read on later days when relative words "
        "mislead. Do NOT log eaten meals or completed workouts here (separate tools)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "update", "invalidate"]},
            "category": {"type": "string", "enum": list(CATEGORIES),
                         "description": "required for add/update"},
            "text": {"type": "string", "description": "the fact text (add/update)"},
            "replaces_text": {"type": "string",
                              "description": "for update: a distinctive fragment of the fact being superseded"},
            "entry_id": {"type": "string", "description": "required for invalidate"},
            "safety_critical": {"type": "boolean",
                                "description": "true for allergies/injuries/medical flags"},
        },
        "required": ["action"],
    },
}


def handle_remember(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Execute a remember tool call under a row lock. Returns a short result
    string for the tool_result block."""
    action = (tool_input.get("action") or "").lower()
    session = get_session()
    try:
        user = (session.query(User).filter(User.id == user_id)
                .with_for_update().one_or_none())
        if not user:
            return "error: user not found"
        profile = dict(user.user_profile_memory or {})

        if action in ("add", "update"):
            category = tool_input.get("category")
            text = (tool_input.get("text") or "").strip()
            if category not in CATEGORIES:
                return f"error: unknown category {category!r}"
            if not text:
                return "error: no text provided"
            # De-deixis floor: a memory fact is timeless text — a bare "tomorrow"
            # in it gets re-resolved against NOW every future read. Pin the date.
            from timefmt import resolve_deixis
            text = resolve_deixis(text, user)
            new_profile, stats = apply_facts(profile, [{
                "action": action, "category": category, "text": text,
                "replaces_text": tool_input.get("replaces_text"),
                "safety_critical": bool(tool_input.get("safety_critical")),
            }], user_id=user_id)
            user.user_profile_memory = new_profile
            flag_modified(user, "user_profile_memory")
            session.commit()
            logger.info("REMEMBER_TOOL user=%s action=%s category=%s stats=%s",
                        user_id, action, category, stats)
            return f"ok: {action} '{text[:40]}' to {category} (stats={stats})"

        if action == "invalidate":
            entry_id = tool_input.get("entry_id")
            if not entry_id:
                return "error: invalidate requires entry_id"
            # trigger is the auditable justification; a safety entry is REJECTED
            # without one (see memory.invalidate_entry's guard).
            trigger = f"msg:{message_id}" if message_id else "remember_tool"
            ok = invalidate_entry(profile, entry_id, by="remember_tool", trigger=trigger)
            if not ok:
                return (f"error: could not invalidate {entry_id} "
                        f"(not found, or a safety entry without a recorded trigger)")
            user.user_profile_memory = profile
            flag_modified(user, "user_profile_memory")
            session.commit()
            logger.info("REMEMBER_TOOL user=%s invalidate=%s", user_id, entry_id)
            return f"ok: invalidated {entry_id}"

        return f"error: unknown action {action!r}"
    finally:
        session.close()


LOG_WORKOUT_TOOL = {
    "name": "log_workout",
    "description": (
        "Log a COMPLETED workout the user reports doing. Records the session and "
        "advances the split pointer (code-mediated). Pass split_day ONLY when the user "
        "named it in their own words ('did legs', 'hit pull') — that's a confirmed "
        "advance. NEVER guess it from their split, the pointer, or the day of the week: "
        "omit it and code infers the next day. (Live: 'went to the gym 9-11' was logged "
        "as pull before the user said pull.) Include exercises the user mentioned with "
        "any sets/reps/weight. If the session was NOT today ('yesterday', 'tuesday'), "
        "pass date as YYYY-MM-DD in the user's local calendar — otherwise it's logged today. "
        "CARDIO (a run, bike, swim, walk, hike, sport): pass cardio=true and NEVER a split_day — "
        "cardio is recorded as its own session and does not move the split pointer. Put "
        "distance in distance_miles and time in duration_min, never in reps. (Live: a 2-mile "
        "run was logged as split_day=full_body with reps=2 and knocked the pointer off push.) "
        "Logging a second time on the same day for the same split day ADDS to that session "
        "(e.g. 'i went' then 'bench 135x3x8') — you never need to delete and re-log."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "split_day": {"type": "string",
                          "description": "ONLY the day the user literally named: push/pull/legs/upper/lower/full_body, or one of their own body-part days from SPLIT in your context (chest_biceps, back_triceps, legs_shoulders, chest, arms …). Omit if they didn't. Never for cardio."},
            "cardio": {"type": "boolean",
                       "description": "true for a run/bike/swim/walk/hike/sport session. Recorded as cardio; the split pointer is untouched."},
            "date": {"type": "string",
                     "description": "YYYY-MM-DD (user's local day) when the session was not today, e.g. 'yesterday'"},
            "exercises": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "sets": {"type": "integer"},
                        "reps": {"type": "integer"},
                        "weight": {"type": "number"},
                        "distance_miles": {"type": "number", "description": "cardio distance"},
                        "duration_min": {"type": "number", "description": "cardio time"},
                    },
                    "required": ["name"],
                },
            },
            "notes": {"type": "string", "description": "anything the user said about the session"},
        },
        "required": [],
    },
}


def _local_day_bounds_utc(tz):
    """[start, end) of the user's current local day as naive UTC."""
    today = datetime.now(tz).date()
    start = datetime(today.year, today.month, today.day, tzinfo=tz)
    to_utc = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None)
    return to_utc(start), to_utc(start + timedelta(days=1))


LOG_WEIGHT_TOOL = {
    "name": "log_weight",
    "description": (
        "Log a body-weight reading the user reports ('weighed in at 141 this morning', a "
        "scale or fitness-app screenshot). Pass weight_lbs (convert kg → lb yourself and put "
        "their raw words in note). If the reading was NOT today, pass date as YYYY-MM-DD in "
        "their local calendar. The stored TREND (context: WEIGHT) is what you quote back, "
        "never the single reading. If they say they don't own a scale, call this with "
        "no_scale=true and nothing else — you'll never be asked to nudge them again."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "weight_lbs": {"type": "number"},
            "date": {"type": "string", "description": "YYYY-MM-DD (local) when not today"},
            "note": {"type": "string", "description": "their words, e.g. '64 kg after breakfast'"},
            "no_scale": {"type": "boolean", "description": "true = they don't own a scale; stop nudging"},
        },
        "required": [],
    },
}


def handle_log_weight(user_id: int, tool_input: dict, *, message_id=None) -> str:
    from models import WeightLog
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return "error: user not found"
        if tool_input.get("no_scale"):
            user.weigh_in_opt_out = True
            session.commit()
            logger.info("WEIGH_IN_OPT_OUT user=%s", user_id)
            return "ok: noted — no scale, weigh-in nudges are off"
        try:
            lbs = float(tool_input.get("weight_lbs"))
        except (TypeError, ValueError):
            return "error: weight_lbs must be a number"
        if not (60 <= lbs <= 600):
            return f"error: {lbs:g} lb is outside a plausible range — check the unit (kg → lb?)"
        tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
        when = _naive_utcnow()
        date_str = (tool_input.get("date") or "").strip() or None
        if date_str:
            try:
                d = _resolve_local_date(tz, date_str, strict=True)
                when = (datetime(d.year, d.month, d.day, 9, 0, tzinfo=tz)
                        .astimezone(timezone.utc).replace(tzinfo=None))
            except Exception:
                date_str = None
        row = WeightLog(user_id=user_id, weighed_at=when, weight_lbs=round(lbs, 1),
                        notes=(tool_input.get("note") or None))
        session.add(row)
        session.flush()
        # Is this their first reading ever? (autoflush means the new row is already
        # visible to the query below, so "no prior" must exclude it by id.)
        first_reading = (session.query(WeightLog.id)
                         .filter(WeightLog.user_id == user_id, WeightLog.id != row.id)
                         .first()) is None
        # users.weight_lbs follows the LATEST reading (protein is g/lb; profile shows it).
        latest = (session.query(WeightLog.weighed_at).filter(WeightLog.user_id == user_id)
                  .order_by(WeightLog.weighed_at.desc()).first())
        is_latest = latest is None or when >= latest[0]
        protein_note, anchor_note = "", ""
        # First reading ever → this weekday becomes the weigh-in anchor (unless they or
        # onboarding already picked one). "Same time each week" needs a day to mean it.
        if first_reading and not (user.weigh_in_day or "").strip():
            user.weigh_in_day = when.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%A").lower()
            anchor_note = f"; weigh-in day set to {user.weigh_in_day}"
        if is_latest:
            user.weight_lbs = round(lbs, 1)
            if getattr(user, "targets_source", None) != "user" and user.protein_target:
                from macro_calculator import calculate_targets
                new_p = calculate_targets(user)["protein"]
                if new_p != user.protein_target:
                    protein_note = f"; protein target {user.protein_target} → {new_p}g (follows weight)"
                    user.protein_target = new_p
                # the computed pair must follow too — set_targets' 15% band reads it
                if getattr(user, "protein_target_computed", None) != new_p:
                    user.protein_target_computed = new_p
        session.commit()
        wid = row.id
    finally:
        session.close()
    logger.info("LOG_WEIGHT user=%s lbs=%s dated=%s latest=%s", user_id, lbs, date_str, is_latest)
    return (f"ok: logged {lbs:g} lb (id={wid}" + (f", dated {date_str}" if date_str else "") + ")"
            + protein_note + anchor_note + " — quote the trend from context, not this reading")


START_WORKOUT_SESSION_TOOL = {
    "name": "start_workout_session",
    "description": (
        "Start today's session for the user: 'starting push', 'about to lift', 'gym time', "
        "'send me today's workout'. Code builds the plan from their history and sends it — "
        "on iMessage one short text plus a card they tap as they go; on SMS one message per "
        "exercise they 👍. Pass template_key only when THEY named the day — one of the day keys "
        "listed under SPLIT in your context (push/pull/legs/upper/lower/full_body, or a body-part "
        "day like chest_biceps / back_triceps / legs_shoulders / chest / arms); otherwise omit it "
        "and code picks the next day of their split. "
        "After 'ok', reply with exactly [silent] — the text and the card already went out; "
        "never add a per-set prompt or a second intro. On 'error' tell them plainly."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"template_key": {"type": "string", "description": "only if they named the day"}},
        "required": [],
    },
}


def handle_start_workout_session(user_id: int, tool_input: dict, *, message_id=None) -> str:
    from workouts.start import start_workout_session
    try:
        r = start_workout_session(user_id, (tool_input.get("template_key") or "").strip() or None)
    except ValueError as e:
        return f"error: {e}"
    except Exception as e:  # noqa: BLE001 — a send failure must not crash the turn
        logger.error("START_WORKOUT_SESSION_FAILED user=%s err=%s", user_id, e, exc_info=True)
        return f"error: couldn't send the session ({e})"
    how = "card" if r["surface"] == "card" else "one message per exercise"
    return (f"ok: {r['template_key']} session #{r['session_id']} sent as a {how} ({r['sets']} sets). "
            f"Reply with exactly [silent].")


SET_TARGETS_TOOL = {
    "name": "set_targets",
    "description": (
        "Set the user's daily calorie and/or protein target to a number THEY asked for. "
        "Code enforces the band: each value must be within 15% of the computed target, or it "
        "is rejected and the result tells you the nearest allowed values — offer those instead "
        "of inventing a number. Use it when they say things like 'can we do 2200' or 'bump "
        "protein to 150'; never to move a target on your own initiative. The stored target "
        "becomes their pick (logged as user-chosen); say so plainly and note what you'd have "
        "set. If they cite a MAINTENANCE number from their own tracking ('my app says i "
        "maintain at 2200', 'my tdee is like 2.1k'), pass it as `maintenance` — within reason "
        "of the computed estimate, code stores it and centres the calorie band on it, so a "
        "target that follows from THEIR maintenance is allowed even when it's outside the band "
        "around ours. Returns 'ok: …' or 'error: …' — only claim a change after 'ok'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "calories": {"type": "integer", "description": "requested daily calories"},
            "protein_g": {"type": "integer", "description": "requested daily protein in grams"},
            "maintenance": {"type": "integer",
                            "description": "the daily maintenance/TDEE they report from their app or prior tracking, when they cite one"},
            "reason": {"type": "string", "description": "their words for why, in brief"},
        },
        "required": [],
    },
}


def handle_set_targets(user_id: int, tool_input: dict, *, message_id=None) -> str:
    from macro_calculator import apply_target_override
    cal = tool_input.get("calories")
    pro = tool_input.get("protein_g")
    maint = tool_input.get("maintenance")
    if cal is None and pro is None and maint is None:
        return "error: give calories, protein_g, and/or maintenance"
    r = apply_target_override(user_id, calories=cal, protein=pro, note=tool_input.get("reason"),
                              maintenance=maint)
    if "error" in r:
        return f"error: {r['error']}"
    parts = []
    m = r.get("maintenance")
    if m:
        if m.get("accepted"):
            parts.append(f"ok: noted their maintenance {m['reported']} (computed estimate was "
                         f"{m['computed_tdee']}); calorie band now centres on {m['basis_calories']}")
        elif "min" in m:
            parts.append(f"error: maintenance {m['reported']} is too far from the computed estimate "
                         f"{m['computed_tdee']} (accept {m['min']}–{m['max']}); say the estimate is "
                         f"what you have to go on and don't store theirs")
        else:
            parts.append("error: maintenance must be a number")
    if r["accepted"]:
        got = ", ".join(f"{k} {v}" for k, v in r["accepted"].items())
        parts.append(f"ok: set {got} (their pick; computed was {r['computed']['calories']} cal / "
                     f"{r['computed']['protein']}g)")
    for field, rj in r["rejected"].items():
        if "min" in rj:
            parts.append(f"error: {field} {rj['asked']} is outside the 15% band — nearest allowed "
                         f"{rj['min']}–{rj['max']} (computed {rj['computed']}); offer one of those")
        else:
            parts.append(f"error: {field} {rj['asked']!r} {rj.get('reason', 'invalid')}")
    parts.append(f"current: {r['current']['calories']} cal / {r['current']['protein']}g")
    return "; ".join(parts)


def _log_into_open_session(user_id: int, exercises: list, notes: str | None) -> str | None:
    """If a card/session is open, the model's extracted sets land there (source
    'text'), matched by name to the session's exercises; unmatched names become
    new exercises. Returns the tool result, or None when no session is open."""
    from workouts.session_ops import active_session_id
    from workouts.templates import slug_for_name
    from card_page import apply_set_update
    from models import WorkoutSession, SetLog
    ws_id = active_session_id(user_id)
    if not ws_id:
        return None
    applied = 0
    session = get_session()
    try:
        ws = session.get(WorkoutSession, ws_id)
        rows = session.query(SetLog).filter(SetLog.session_id == ws_id).order_by(SetLog.id).all()
        labels = {r.exercise: (r.exercise_label or r.exercise) for r in rows}
        # Reconcile, don't append: when the coach re-logs an exercise (e.g. after the user
        # says the numbers are off), supersede this session's prior TEXT sets for it — the
        # fragile terse guesses — before the authoritative ones land. Card/tapback/coach
        # sets (real user actions) are kept. (Live 2026-09-15: a bad 135×3 lingered next to
        # three real 135×7 because this appended.)
        provided = set()
        for e in exercises or []:
            if isinstance(e, dict) and e.get("name"):
                nm = str(e["name"]).strip().lower()
                sl = next((s0 for s0, lb in labels.items() if lb.lower() in nm or nm in lb.lower()), None) or slug_for_name(nm)
                if sl:
                    provided.add(sl)
        for r in rows:
            if r.exercise in provided and r.source == "text" and r.done:
                session.delete(r)
        session.flush()
        rows = session.query(SetLog).filter(SetLog.session_id == ws_id).order_by(SetLog.id).all()
        for e in exercises or []:
            if not isinstance(e, dict):
                continue
            name = (e.get("name") or "").strip().lower()
            slug = next((sl for sl, lb in labels.items() if lb.lower() in name or name in lb.lower()), None) or slug_for_name(name)
            n_sets = int(e.get("sets") or 1)
            w, r = e.get("weight"), e.get("reps")
            mine = [x for x in rows if x.exercise == slug and not x.done]
            for i in range(n_sets):
                if i < len(mine):
                    apply_set_update(session, ws, mine[i], done=True,
                                     actual_weight=w if w is not None else mine[i].planned_weight,
                                     actual_reps=r if r is not None else mine[i].planned_reps, source="text")
                    applied += 1
                elif slug and w is not None and r is not None:
                    new = SetLog(session_id=ws_id, exercise=slug, exercise_label=labels.get(slug, name or slug),
                                 set_index=len([x for x in rows if x.exercise == slug]) + i, planned_weight=w, planned_reps=r)
                    session.add(new); session.flush()
                    apply_set_update(session, ws, new, done=True, actual_weight=w, actual_reps=r, source="text")
                    applied += 1
    finally:
        session.close()
    from workouts.card import refresh_card_async
    refresh_card_async(ws_id)
    logger.info("LOG_WORKOUT_INTO_SESSION user=%s session=%s sets=%s", user_id, ws_id, applied)
    return (f"ok: logged {applied} sets into today's open session (#{ws_id}); it's still open — "
            f"they finish on the card or by texting 'done'")


def handle_log_workout(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Create a Workout and advance the split pointer under the Phase-1 policy.

    Three live-driven rules (2026-09-12, user 27):
      - cardio=true → workout_type='cardio', pointer untouched (a run logged as
        split_day=full_body knocked the pointer off push).
      - a same-day log for the same split day APPENDS to that session instead of
        creating a second row ("i went" → "bench 135x3x8" was create+delete twice).
      - a pointer advance is recorded on the row (edits[] field='split_pointer') so
        manage_log delete can roll it back — a deleted session must not leave the
        pointer claiming it happened.
    """
    from split_pointer import advance_split_pointer, parse_named_split_day

    exercises = tool_input.get("exercises") or []
    split_day = (tool_input.get("split_day") or "").strip().lower() or None
    cardio = bool(tool_input.get("cardio"))
    notes = tool_input.get("notes")
    date_str = (tool_input.get("date") or "").strip() or None
    if cardio:
        split_day = None  # never a split day, whatever the model passed
    elif not date_str:
        # One truth: an open card/session absorbs a text log ("bench 190x4 done").
        routed = _log_into_open_session(user_id, exercises, notes)
        if routed:
            return routed

    session = get_session()
    try:
        user = (session.query(User).filter(User.id == user_id)
                .with_for_update().one_or_none())
        if not user:
            return "error: user not found"
        tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
        # A past-day session ("yesterday i went 9-11") is stamped on THAT local day
        # (noon local → UTC) so day-bucketed readers see it where it happened. Live
        # 2026-09-11: yesterday's pull was logged as today's. Bad date → today.
        when = None
        is_today = True
        if date_str:
            try:
                local_day = _resolve_local_date(tz, date_str, strict=True)
                is_today = local_day == datetime.now(tz).date()
                when = (datetime(local_day.year, local_day.month, local_day.day, 12, 0, tzinfo=tz)
                        .astimezone(timezone.utc).replace(tzinfo=None))
            except Exception:
                when, is_today = None, True

        # Same-day append: today's active non-cardio session for the same split day
        # (or an unlabeled 'logged' one) absorbs this call. Cardio is always its own row.
        existing = None
        if not cardio and when is None:
            lo, hi = _local_day_bounds_utc(tz)
            for w0 in (active(session, Workout, user_id=user_id)
                       .filter(Workout.date >= lo, Workout.date < hi)
                       .order_by(Workout.id.desc()).all()):
                if w0.workout_type == "cardio":
                    continue
                if split_day is None or w0.workout_type in (split_day, "logged"):
                    existing = w0
                    break
        if existing is not None:
            merged = list(existing.exercises or []) + list(exercises)
            existing.exercises = merged
            flag_modified(existing, "exercises")
            if notes:
                existing.user_notes = f"{existing.user_notes}; {notes}" if existing.user_notes else notes
            relabel = bool(split_day and existing.workout_type == "logged")
            if relabel:
                existing.workout_type = split_day
            existing.completed = True
            session.commit()
            wid, wtype = existing.id, existing.workout_type
            session.close()
            pointer = None
            if relabel:  # "i went" → "it was push": the unnamed row now has a name
                pointer = advance_split_pointer(user_id, named_day=split_day)
            logger.info("LOG_WORKOUT_TOOL user=%s appended workout_id=%s split_day=%s +%d exercises pointer=%s",
                        user_id, wid, split_day, len(exercises), pointer)
            return (f"ok: added {len(exercises)} exercises to today's {wtype} session (id={wid}, "
                    f"{len(merged)} total)"
                    + (f", split pointer now {pointer['day']} ({pointer['source']})" if pointer else ""))

        w = Workout(user_id=user_id,
                    workout_type=("cardio" if cardio else (split_day or "logged")),
                    exercises=exercises, user_notes=notes, completed=True,
                    date=(when if when is not None else _naive_utcnow()))
        session.add(w)
        session.commit()
        wid = w.id
    finally:
        session.close()

    if cardio:
        logger.info("LOG_WORKOUT_TOOL user=%s workout_id=%s cardio (pointer untouched)", user_id, wid)
        return (f"ok: logged cardio (id={wid}, {len(exercises)} activities"
                + (f", dated {date_str}" if when is not None else "") + "), split pointer untouched")

    if is_today:
        confirm_workout_today(user_id)
    # named day -> confirmed advance; else infer from the notes, then the cycle.
    day = split_day or parse_named_split_day(notes or "")
    from split_pointer import get_split_pointer
    before = get_split_pointer(user_id)
    pointer = advance_split_pointer(user_id, named_day=day)
    if pointer and pointer != before:
        _record_pointer_advance(wid, before, pointer)
    logger.info("LOG_WORKOUT_TOOL user=%s workout_id=%s split_day=%s pointer=%s",
                user_id, wid, split_day, pointer)
    return (f"ok: logged workout (id={wid}, {len(exercises)} exercises"
            + (f", dated {date_str}" if when is not None else "") + ")"
            + (f", split pointer now {pointer['day']} ({pointer['source']})" if pointer else ""))


def _pointer_ser(p):
    return {"day": p["day"], "at": _ser(p["at"]), "source": p["source"]} if p else None


def _record_pointer_advance(workout_id: int, before, after):
    """Audit the pointer move on the workout row that caused it (edits[] entry,
    field='split_pointer'), so deleting the row can undo exactly that move."""
    session = get_session()
    try:
        w = session.get(Workout, workout_id)
        if w is None:
            return
        w.edits = list(w.edits or []) + [{"at": _naive_utcnow().isoformat(), "field": "split_pointer",
                                          "old": _pointer_ser(before), "new": _pointer_ser(after)}]
        flag_modified(w, "edits")
        session.commit()
    finally:
        session.close()


def _rollback_pointer_for_deleted_workout(user_id: int, row) -> str | None:
    """If this workout's log moved the split pointer AND the pointer still sits where
    that move left it, put it back where it was. If it moved again since (a later
    log), leave it — that later log owns the pointer now."""
    from split_pointer import get_split_pointer, restore_split_pointer
    entry = next((e for e in reversed(list(row.edits or [])) if e.get("field") == "split_pointer"), None)
    if not entry:
        return None
    current = _pointer_ser(get_split_pointer(user_id))
    if current != entry.get("new"):
        return None
    restored = restore_split_pointer(user_id, entry.get("old"))
    logger.info("SPLIT_POINTER_ROLLBACK user=%s workout_id=%s restored=%s", user_id, row.id, restored)
    return (f"split pointer rolled back to {restored['day']} ({restored['source']})"
            if restored else "split pointer cleared (nothing logged before this)")


MANAGE_LOG_TOOL = {
    "name": "manage_log",
    "description": (
        "List, edit, or soft-delete the user's logged meals / workouts / events by "
        "their short id (shown in your context). Use delete for a duplicate or wrong "
        "entry, edit to fix macros or details. IMPORTANT: only confirm a change to "
        "the user AFTER this returns 'ok' — if it returns an 'error', tell them you "
        "couldn't make the change; never claim you did."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "delete", "edit"]},
            "entity": {"type": "string", "enum": ["meal", "workout", "event"]},
            "id": {"type": "integer", "description": "the short id of the entry (delete/edit)"},
            "fields": {"type": "object",
                       "description": "for edit: only the fields to change (others untouched). "
                       "meal: calories/protein_g/carbs_g/fat_g/description/notes. "
                       "workout: workout_type/notes. event: description/starts_at/ends_at/date "
                       "(times are local 'HH:MM', e.g. {\"starts_at\": \"13:00\"}; `date` moves "
                       "the event to a new day — 'today'/'tomorrow'/'YYYY-MM-DD' — keeping its "
                       "existing time unless starts_at/ends_at are also given in the same call)."},
        },
        "required": ["action"],
    },
}

LOG_MEAL_TOOL = {
    "name": "log_meal",
    "description": (
        "Log a meal the user reports eating. READ-BEFORE-WRITE: first check "
        "'TODAY'S LOGGED MEALS' in your context. If this is the SAME serving already "
        "logged, do NOT log it again — reference the existing entry instead. If it's "
        "a genuine SECOND serving of something similar, log it and set saw_similar to "
        "the id(s) of the similar entries you saw (so the choice is auditable). If "
        "you're unsure whether it's a repeat, ask the user one short question before "
        "logging — never guess in either direction. Include macros if you can estimate "
        "them. For a multi-item plate ('chicken, rice, and a coke'), pass an `items` "
        "list — one call is cheaper than several and less likely to truncate. If they're "
        "telling you about a meal from an EARLIER day ('last night's dinner', 'yesterday I "
        "had…'), pass `date` ('yesterday' or YYYY-MM-DD) so it lands on that day — never "
        "log a past meal as today (it would wrongly eat into today's remaining)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "date": {"type": "string", "description": "'yesterday' or 'YYYY-MM-DD' when the meal was NOT today (default today)"},
            "calories": {"type": "integer"},
            "protein_g": {"type": "integer"},
            "carbs_g": {"type": "integer"},
            "fat_g": {"type": "integer"},
            "saw_similar": {"type": "array", "items": {"type": "integer"},
                            "description": "ids of similar already-logged meals you saw and judged to be a distinct serving"},
            "items": {"type": "array",
                      "description": "OR log several items at once: a list of "
                                     "{description, calories?, protein_g?, carbs_g?, fat_g?, saw_similar?} objects.",
                      "items": {"type": "object", "properties": {
                          "description": {"type": "string"},
                          "calories": {"type": "integer"}, "protein_g": {"type": "integer"},
                          "carbs_g": {"type": "integer"}, "fat_g": {"type": "integer"},
                          "saw_similar": {"type": "array", "items": {"type": "integer"}},
                      }, "required": ["description"]}},
        },
    },
}


def _day_total_suffix(user_id: int) -> str:
    """The FRESH authoritative day total, read after a recompute, as a tool-result
    suffix. The context's TODAY'S TOTALS block was built BEFORE this turn's log/edit,
    so the coach must quote THIS number for the updated running total instead of adding
    the new meal to a stale block by hand (the live 2026-09-19 protein-drift bug)."""
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return ""
        cal = u.calories_today or 0
        pro = u.protein_today or 0
        tgt = f", {u.protein_target - pro}g protein left of {u.protein_target}" if u.protein_target else ""
        return f" | DAY TOTAL NOW: {cal} cal, {pro}g protein{tgt} — use this exact number"
    finally:
        session.close()


def handle_log_meal(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Create Meal(s) (the model already did the read-before-write judgment) and
    recompute today's totals ONCE. Accepts a single meal or an `items` list (a
    multi-item plate). Records saw_similar so an intentional near-duplicate is
    auditable and correctable via manage_log."""
    items = tool_input.get("items")
    if not isinstance(items, list):
        items = [tool_input]           # single-meal form (backward compatible)
    items = [it for it in items if (it.get("description") or "").strip()]
    if not items:
        return "error: description required"

    # A past-day meal ("last night's dinner", reported this morning) is stamped on
    # THAT local day (noon local → UTC) so today's totals don't absorb it. Live
    # 2026-09-12: Friday's SF pizza logged as Saturday → "why is it 1450 cal, i just
    # woke up" → deleted instead of re-dated. Bad date → today (never lose a meal).
    date_str = (tool_input.get("date") or "").strip() or None
    when, is_today = _naive_utcnow(), True
    if date_str:
        session = get_session()
        try:
            tz_str = (session.query(User.user_timezone).filter(User.id == user_id).first() or [None])[0]
        finally:
            session.close()
        tz = ZoneInfo(tz_str or "America/Los_Angeles")
        try:
            local_day = _resolve_local_date(tz, date_str, strict=True)
            is_today = local_day == datetime.now(tz).date()
            if not is_today:
                when = (datetime(local_day.year, local_day.month, local_day.day, 12, 0, tzinfo=tz)
                        .astimezone(timezone.utc).replace(tzinfo=None))
        except Exception:
            when, is_today = _naive_utcnow(), True

    logged = []
    session = get_session()
    try:
        for it in items:
            saw = it.get("saw_similar") or []
            meal = Meal(
                user_id=user_id, description=it["description"].strip(),
                calories=it.get("calories"), protein_g=it.get("protein_g"),
                carbs_g=it.get("carbs_g"), fat_g=it.get("fat_g"),
                source="text", log_type="user_reported",
                notes=(f"saw_similar={saw}" if saw else None), eaten_at=when,
            )
            session.add(meal)
            session.flush()
            logged.append((meal.id, meal.description, it.get("calories") or 0,
                           it.get("protein_g") or 0, saw))
        session.commit()
    finally:
        session.close()

    day = ""
    if is_today:
        recompute_daily_totals(user_id)  # once, after all inserts — a past-day meal leaves today alone
        day = _day_total_suffix(user_id)  # the FRESH post-log total, so the coach quotes it (not head math)
    for mid, desc, _cal, _pro, saw in logged:
        if saw:
            logger.info("LOG_MEAL_SAW_SIMILAR user=%s meal_id=%s saw=%s (model logged as distinct serving)",
                        user_id, mid, saw)
        logger.info("LOG_MEAL user=%s meal_id=%s desc=%r", user_id, mid, desc[:40])

    dated = f", dated {date_str}" if (date_str and not is_today) else ""
    if len(logged) == 1:
        mid, desc, cal, pro, saw = logged[0]
        # Include the description so the reply NAMES what was logged ("logged the chicken
        # wrap, ~650 cal 38g"), not just macros — the user asked to see what was recorded.
        return (f"ok: logged '{desc}' id={mid} ({cal}cal/{pro}g{dated})"
                + (f" [saw_similar={saw}]" if saw else "") + day)
    names = ", ".join(f"'{d}'" for _m, d, _c, _p, _s in logged)
    ids = [m for m, _d, _c, _p, _s in logged]
    total_cal = sum(c for _m, _d, c, _p, _s in logged)
    return f"ok: logged {len(logged)} items: {names} (ids {ids}, {total_cal}cal total)" + day


LOG_EVENT_TOOL = {
    "name": "log_event",
    "description": (
        "Save a DATED, day-scoped commitment on the user's calendar — a one-off thing "
        "happening today or on a specific day (from a calendar screenshot, or text like "
        "'lab till 2 today', 'founder summit noon to 2:30', 'orgo exam friday 9am'). "
        "Use this for time-bound events — NOT recurring habits or standing preferences "
        "(those go to remember with category 'schedule'). Times are the user's LOCAL "
        "time, 24-hour 'HH:MM'. This is how a dated commitment survives to the day it "
        "matters and stays visible for a timely check-in — a semantic memory fact would "
        "be wrong (it never expires) and gets crowded out. For a whole calendar / a day "
        "with several commitments, pass an `events` list — ONE call for the screenshot "
        "is cheaper and far less likely to truncate than five separate calls."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "single event: what it is, e.g. 'founder summit'"},
            "starts_at": {"type": "string", "description": "local start time 'HH:MM' (24h, optional)"},
            "ends_at": {"type": "string", "description": "local end time 'HH:MM' (24h, optional)"},
            "date": {"type": "string", "description": "'today' (default), 'tomorrow', or 'YYYY-MM-DD'"},
            "events": {"type": "array",
                       "description": "OR log several at once (a calendar screenshot): a list of "
                                      "{description, starts_at?, ends_at?, date?} objects.",
                       "items": {"type": "object", "properties": {
                           "description": {"type": "string"},
                           "starts_at": {"type": "string"}, "ends_at": {"type": "string"},
                           "date": {"type": "string"},
                       }, "required": ["description"]}},
        },
    },
}


SET_REMINDER_TOOL = {
    "name": "set_reminder",
    "description": (
        "Promise to text them at a specific LOCAL time — and keep it: code sends the "
        "reminder at that time, independent of anything else. Use this the moment they "
        "ask to be reminded / pinged / texted at or after something ('remind me to run "
        "after class', 'ping me at 7 to take creatine', 'text me when class is over'). "
        "Give `days` for a standing ask (Tue/Thu after class) or `date` for a one-off. "
        "Never say 'i'll ping u' without calling this — a reminder that isn't set won't "
        "happen. Time is 24h 'HH:MM' in their local time."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "what to remind them of, from their side: 'go run', 'take creatine'"},
            "time": {"type": "string", "description": "local time 'HH:MM' (24h)"},
            "days": {"type": "array", "items": {"type": "string"},
                     "description": "recurring weekdays, e.g. ['tue','thu']; omit for a one-off"},
            "date": {"type": "string", "description": "one-off: 'today' (default), 'tomorrow', or 'YYYY-MM-DD'"},
        },
        "required": ["text", "time"],
    },
}

SAVE_ROUTINE_TOOL = {
    "name": "save_routine",
    "description": (
        "They pasted or described THEIR OWN routine. Two forms: (1) a program with days AND "
        "exercises (\"Mon push: incline db press 3x10, shoulder press 3x10 …\") → pass "
        "routine_text as they gave it (all days at once) and their cards show THEIR exercises; "
        "(2) just how they group their days (\"chest and bis, back and tris, legs and "
        "shoulders\", \"push pull legs\", \"chest, back, shoulders, arms, legs\") → pass "
        "split_days, one entry per day IN THEIR ORDER, in their words. Code turns each into "
        "a day key and their cards follow that cycle. Do this the moment they state their "
        "split — a split that isn't saved gets them the wrong card. Days they didn't give "
        "exercises for use the default template for that body part. Card weights start as "
        "placeholders and update from their first logged sets — say so when you saved "
        "exercises. Not for a single session they just did (that's log_workout)."
    ),
    "input_schema": {"type": "object", "properties": {
        "routine_text": {"type": "string", "description": "the routine as written, all days (form 1)"},
        "split_days": {"type": "array", "items": {"type": "string"},
                       "description": "their days in order, each in their words: [\"chest and biceps\", \"back and triceps\", \"legs and shoulders\"] (form 2)"}},
        "required": []},
}

CANCEL_REMINDER_TOOL = {
    "name": "cancel_reminder",
    "description": "Cancel a reminder they no longer want (ids are in the REMINDERS block of your context).",
    "input_schema": {"type": "object", "properties": {"reminder_id": {"type": "integer"}},
                     "required": ["reminder_id"]},
}


def handle_set_reminder(user_id: int, tool_input: dict, *, message_id=None) -> str:
    from reminders import create_reminder, describe, _tz
    r = create_reminder(user_id, tool_input.get("text"), tool_input.get("time"),
                        days=tool_input.get("days"), date_str=tool_input.get("date"), source="model")
    if "error" in r:
        return f"error: {r['error']}"
    session = get_session()
    try:
        from models import Reminder
        row = session.get(Reminder, r["id"])
        user = session.get(User, user_id)
        return f"ok: reminder set — {describe(row, _tz(user.user_timezone if user else None))}"
    finally:
        session.close()


def handle_save_routine(user_id: int, tool_input: dict, *, message_id=None) -> str:
    from workouts.routine import save_routine, save_split_days
    from workouts.templates import day_label
    text = (tool_input.get("routine_text") or "").strip()
    days_in = tool_input.get("split_days")
    if isinstance(days_in, list) and days_in and not text:
        r = save_split_days(user_id, [str(d) for d in days_in], source="model")
        if "error" in r:
            return f"error: {r['error']}"
        return (f"ok: split saved — " + " → ".join(day_label(d) for d in r["days"])
                + f" (day keys: {', '.join(r['days'])}). start_workout_session now walks this order.")
    if len(text) < 20:
        return "error: pass routine_text (the whole routine as they gave it) or split_days (their days in order)"
    r = save_routine(user_id, text, source="model")
    if "error" in r:
        return f"error: {r['error']}"
    days = ", ".join(f"{k} ({n} exercises)" for k, n in r["days"].items())
    return (f"ok: routine saved to their cards — {days}; split={r['split']}. Weights are placeholders "
            f"until they log real sets — tell them that.")


def handle_cancel_reminder(user_id: int, tool_input: dict, *, message_id=None) -> str:
    from reminders import cancel_reminder
    try:
        rid = int(tool_input.get("reminder_id"))
    except (TypeError, ValueError):
        return "error: reminder_id required"
    return "ok: cancelled" if cancel_reminder(user_id, rid) else "error: no active reminder with that id"


def _resolve_local_date(tz: ZoneInfo, date_str, *, strict: bool = False) -> date:
    """Resolve 'today' (default) / 'tomorrow' / 'YYYY-MM-DD' to a date in tz.
    Non-strict (log_event): unparseable input silently falls back to today, the
    existing behavior. Strict (manage_log date edit): unparseable input raises
    ValueError — a silent wrong-day edit is worse than a rejected one."""
    today_local = datetime.now(tz).date()
    ds = (date_str or "today").strip().lower()
    if ds == "tomorrow":
        return today_local + timedelta(days=1)
    if ds in ("yesterday", "last night"):
        return today_local - timedelta(days=1)
    if ds in ("", "today"):
        return today_local
    try:
        return date.fromisoformat(ds)
    except ValueError:
        if strict:
            raise
        return today_local


def _parse_local_dt(tz_str: str, date_str, hhmm):
    """Combine a date (today/tomorrow/YYYY-MM-DD) + local 'HH:MM' -> naive UTC.
    Returns None if no time given (caller lets the Event default occurred_at=now)."""
    if not hhmm:
        return None
    try:
        parts = str(hhmm).split(":")
        hh, mm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return None
    try:
        tz = ZoneInfo(tz_str or "America/Los_Angeles")
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    d = _resolve_local_date(tz, date_str)
    local_dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def handle_log_event(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Persist a dated schedule item as an Event (source='model'), so it auto-expires
    with its day and surfaces in TODAY'S EVENTS for the reactive loop AND the heartbeat.
    Dated items belong here, not in the `schedule` memory category — that's the
    burn-in fix (schedule facts were landing in memory and getting evicted)."""
    from events import record_event
    items = tool_input.get("events")
    if not isinstance(items, list):
        items = [tool_input]           # single-event form (backward compatible)
    items = [it for it in items if (it.get("description") or "").strip()]
    if not items:
        return "error: description required"

    session = get_session()
    try:
        user = session.get(User, user_id)
        tz_str = (user.user_timezone if user else None) or "America/Los_Angeles"
    finally:
        session.close()

    logged = []
    for it in items:
        desc = it["description"].strip()
        starts = _parse_local_dt(tz_str, it.get("date"), it.get("starts_at"))
        ends = _parse_local_dt(tz_str, it.get("date"), it.get("ends_at"))
        if starts is None and ends is None:
            # Date-only item ("exam friday", no time): an all-day event on the
            # RESOLVED day. Without this, Event.occurred_at defaults to now and
            # the date is silently lost — "friday" lands today, and the lifecycle
            # readers would then render it as already passed.
            try:
                tz = ZoneInfo(tz_str)
            except Exception:
                tz = ZoneInfo("America/Los_Angeles")
            d = _resolve_local_date(tz, it.get("date"))
            starts = (datetime(d.year, d.month, d.day, tzinfo=tz)
                      .astimezone(timezone.utc).replace(tzinfo=None))
            ends = (datetime(d.year, d.month, d.day, 23, 59, tzinfo=tz)
                    .astimezone(timezone.utc).replace(tzinfo=None))
        eid = record_event(user_id, "scheduled", ends_at=ends, source="model",
                           raw_text=desc, occurred_at=starts)
        logged.append((eid, desc, it.get("starts_at")))
        logger.info("LOG_EVENT user=%s event_id=%s desc=%r start=%s end=%s",
                    user_id, eid, desc[:40], starts, ends)

    if len(logged) == 1:
        eid, desc, st = logged[0]
        return f"ok: noted '{desc}'{f' at {st}' if st else ''} (event id={eid})"
    ids = [e for e, _d, _s in logged]
    names = ", ".join(f"'{d}'" for _e, d, _s in logged)
    return f"ok: noted {len(logged)} events: {names} (ids {ids})"


_ENTITY_MODEL = {"meal": Meal, "workout": Workout, "event": Event}
# model-facing field -> (column, kind). kind: "str" | "int" | "event_time" (a local
# 'HH:MM' -> naive UTC on the row's CURRENT local day, so editing the time keeps the day)
# | "event_date" (moves occurred_at/ends_at to a new local day, keeping each's existing
# time-of-day — a day move, not a time move; see handle_manage_log's field ordering).
# Partial edits touch only supplied fields; never a free-text re-description of the row.
_EDIT_FIELDS = {
    "meal": {"calories": ("calories", "int"), "protein_g": ("protein_g", "int"),
             "carbs_g": ("carbs_g", "int"), "fat_g": ("fat_g", "int"),
             "description": ("description", "str"), "notes": ("notes", "str")},
    "workout": {"workout_type": ("workout_type", "str"),
                "user_notes": ("user_notes", "str"), "notes": ("user_notes", "str")},
    "event": {"description": ("raw_text", "str"),
              "starts_at": ("occurred_at", "event_time"),
              "ends_at": ("ends_at", "event_time"),
              "date": ("occurred_at", "event_date"),
              "event_type": ("event_type", "str")},
}


def _ser(x):
    """Audit/serialize a field value (datetimes -> iso) for the edits log + result string."""
    return x.isoformat() if hasattr(x, "isoformat") else x


def _naive_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def handle_manage_log(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """List / edit / soft-delete the user's records by short id. Deletes are soft;
    meal changes recompute today's totals. Returns 'ok:...' ONLY on real success —
    this is what makes the honesty invariant satisfiable."""
    action = (tool_input.get("action") or "").lower()

    if action == "list":
        session = get_session()
        try:
            meals = active(session, Meal, user_id=user_id).order_by(Meal.eaten_at.desc()).limit(15).all()
            workouts = active(session, Workout, user_id=user_id).order_by(Workout.date.desc()).limit(10).all()
            events = active(session, Event, user_id=user_id).order_by(Event.occurred_at.desc()).limit(10).all()
            lines = [f"meal [id {m.id}] {m.description} — {m.calories or 0}cal/{m.protein_g or 0}g"
                     for m in meals]
            lines += [f"workout [id {w.id}] {w.workout_type}" for w in workouts]
            lines += [f"event [id {e.id}] {(e.raw_text or e.event_type or '').strip()}"
                      + (f" ({e.occurred_at:%m-%d %H:%M}Z)" if e.occurred_at else "")
                      for e in events]
            return "ok:\n" + ("\n".join(lines) if lines else "(nothing logged)")
        finally:
            session.close()

    if action not in ("delete", "edit"):
        return f"error: unknown action {action!r}"

    entity = (tool_input.get("entity") or "").lower()
    Model = _ENTITY_MODEL.get(entity)
    if Model is None:
        return f"error: unknown entity {entity!r}"
    entry_id = tool_input.get("id")
    if not entry_id:
        return f"error: {action} requires an id"

    session = get_session()
    try:
        row = (active(session, Model, user_id=user_id)
               .filter(Model.id == entry_id).one_or_none())
        if row is None:
            return f"error: no active {entity} with id {entry_id} (already deleted or wrong id)"

        if action == "delete":
            row.deleted_at = _naive_utcnow()
            session.commit()
            day = ""
            if entity == "meal":
                recompute_daily_totals(user_id)
                _clear_pending_writeback(user_id, entry_id)
                day = _day_total_suffix(user_id)  # fresh total so the coach quotes it, not head math
            note = _rollback_pointer_for_deleted_workout(user_id, row) if entity == "workout" else None
            logger.info("MANAGE_LOG user=%s delete %s id=%s", user_id, entity, entry_id)
            return f"ok: deleted {entity} id={entry_id}" + (f"; {note}" if note else "") + day

        # edit — field-level, ID-targeted, AUDITED. Only supplied fields change; each
        # change captures its prior value into row.edits (an edited row otherwise silently
        # claims to have always held its new value). Recompute totals, never patch a delta.
        fields = tool_input.get("fields") or {}
        spec = _EDIT_FIELDS.get(entity, {})
        applied, audit, tz_str = {}, list(row.edits or []), None
        # A day move (event_date) must land BEFORE a time move (event_time) in the
        # same call — event_time reads its target day off the row's CURRENT
        # occurred_at, so a combined {"date": ..., "starts_at": ...} edit only lands
        # on the new day if the date move has already been applied to the row.
        ordered_fields = sorted(
            fields.items(), key=lambda kv: spec.get(kv[0], (None, ""))[1] != "event_date"
        )
        for mfield, value in ordered_fields:
            if mfield not in spec:
                continue
            column, kind = spec[mfield]
            if kind == "int":
                try:
                    newval = int(value)
                except (TypeError, ValueError):
                    return f"error: {mfield} must be a number"
            elif kind == "event_date":
                if tz_str is None:
                    u = session.get(User, user_id)
                    tz_str = (u.user_timezone if u else None) or "America/Los_Angeles"
                try:
                    tz = ZoneInfo(tz_str)
                except Exception:
                    tz = ZoneInfo("America/Los_Angeles")
                try:
                    new_day = _resolve_local_date(tz, str(value), strict=True)
                except ValueError:
                    return f"error: {mfield} must be a local date like 'YYYY-MM-DD', 'today', or 'tomorrow'"
                # Moves BOTH occurred_at and ends_at (whichever are set) to the new
                # day, each keeping its own local time-of-day — a day move, not a
                # time move. event_time (below/next) can still adjust the clock.
                for col in ("occurred_at", "ends_at"):
                    old = getattr(row, col, None)
                    if old is None:
                        continue
                    local_time = old.replace(tzinfo=timezone.utc).astimezone(tz).time()
                    newcol = (datetime.combine(new_day, local_time, tzinfo=tz)
                              .astimezone(timezone.utc).replace(tzinfo=None))
                    audit.append({"at": _naive_utcnow().isoformat(), "field": mfield,
                                  "old": _ser(old), "new": _ser(newcol)})
                    setattr(row, col, newcol)
                applied[mfield] = new_day.isoformat()
                continue
            elif kind == "event_time":
                if tz_str is None:
                    u = session.get(User, user_id)
                    tz_str = (u.user_timezone if u else None) or "America/Los_Angeles"
                try:
                    tz = ZoneInfo(tz_str)
                except Exception:
                    tz = ZoneInfo("America/Los_Angeles")
                base = getattr(row, "occurred_at", None) or _naive_utcnow()
                day = base.replace(tzinfo=timezone.utc).astimezone(tz).date().isoformat()
                newval = _parse_local_dt(tz_str, day, str(value))  # keep day, edit clock
                if newval is None:
                    return f"error: {mfield} must be a local time like '13:00'"
            else:
                newval = str(value).strip()
            old = getattr(row, column)
            audit.append({"at": _naive_utcnow().isoformat(), "field": mfield,
                          "old": _ser(old), "new": _ser(newval)})
            setattr(row, column, newval)
            applied[mfield] = _ser(newval)
        if not applied:
            return f"error: no editable fields in {list(fields)} for {entity}"
        row.edits = audit
        flag_modified(row, "edits")
        session.commit()
        day = ""
        if entity == "meal":
            recompute_daily_totals(user_id)
            _clear_pending_writeback(user_id, entry_id)
            day = _day_total_suffix(user_id)  # fresh total after the edit, so the coach quotes it
        logger.info("MANAGE_LOG user=%s edit %s id=%s fields=%s", user_id, entity, entry_id, applied)
        return f"ok: edited {entity} id={entry_id} ({applied})" + day
    finally:
        session.close()


GET_DINING_MENU_TOOL = {
    "name": "get_dining_menu",
    "description": (
        "Get today's UC Berkeley dining-hall menu with macros, on demand. Use it "
        "when the user asks what to eat at a hall (crossroads / foothill / clark_kerr "
        "/ cafe3). Optionally filter by meal period. Returns items with calories and "
        "protein so you can recommend a specific pick."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "hall": {"type": "string", "enum": ["crossroads", "foothill", "clark_kerr", "cafe3"]},
            "meal_period": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "brunch"]},
        },
        "required": ["hall"],
    },
}


def handle_get_dining_menu(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Read today's scraped menu for a hall (on-demand, replaces context injection)."""
    from zoneinfo import ZoneInfo
    from dining_scraper import _canonical_hall

    hall = _canonical_hall(tool_input.get("hall") or "")
    meal_period = (tool_input.get("meal_period") or "").strip().lower() or None
    today = datetime.now(ZoneInfo("America/Los_Angeles")).strftime("%Y-%m-%d")

    session = get_session()
    try:
        q = (session.query(DiningMenuItem)
             .filter(DiningMenuItem.scraped_date == today, DiningMenuItem.hall == hall))
        if meal_period:
            q = q.filter(DiningMenuItem.meal_period == meal_period)
        items = q.limit(80).all()
    finally:
        session.close()

    if not items:
        return (f"error: no menu for {hall} today ({today}) — it may be closed for "
                f"summer or not scraped yet")
    lines = [f"{i.item_name} ({i.meal_period}): {i.calories or '?'}cal, "
             f"{round(i.protein_g) if i.protein_g else '?'}g protein" for i in items]
    return f"ok: {hall} menu today:\n" + "\n".join(lines)


MATCH_MEAL_HISTORY_TOOL = {
    "name": "match_meal_history",
    "description": (
        "Check whether the user has logged this meal (or something like it) before, "
        "BEFORE estimating a meal that plausibly repeats — a repeat meal has a "
        "personal ground truth (their portions, their prep) that beats a generic "
        "estimate. A confident match: lean on their usual numbers and tell them "
        "you're doing so ('using your usual for this'). If they say they ALREADY ate "
        "it, call log_meal in this same turn — checking history is a step toward "
        "logging, never a reason to stop and ask permission. An ambiguous match or a "
        "different portion: ask one short question or estimate fresh — never "
        "silently assume the prior fits. Never claim history this tool didn't "
        "return, and never say 'logged' unless log_meal returned ok this turn — "
        "the tool call is the action."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string",
                            "description": "the meal as the user described it / as you'd log it"},
        },
        "required": ["description"],
    },
}


def handle_match_meal_history(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Read-only: surface the user's own repeat-meal groups (median macros, count,
    recency). No match is a real answer — the model falls through to estimating."""
    from meal_history import match_meal_history

    query = (tool_input.get("description") or "").strip()
    if not query:
        return "error: description required"

    matches = match_meal_history(user_id, query)
    if not matches:
        return f"no history match for '{query}' — estimate fresh"

    now = _naive_utcnow()
    lines = []
    for m in matches:
        days = max(0, (now - m["last_eaten_at"]).days) if m["last_eaten_at"] else None
        when = "today" if days == 0 else (f"{days}d ago" if days is not None else "unknown")
        macros = []
        if m["calories"] is not None:
            macros.append(f"~{m['calories']}cal")
        if m["protein_g"] is not None:
            macros.append(f"{m['protein_g']}g protein")
        if m["carbs_g"] is not None:
            macros.append(f"{m['carbs_g']}g carbs")
        if m["fat_g"] is not None:
            macros.append(f"{m['fat_g']}g fat")
        macro_txt = ", usually " + "/".join(macros) if macros else ", no macros recorded"
        lines.append(f"'{m['description']}' — logged {m['count']}x, last {when}{macro_txt}")
    # The affordance rides the RESULT, at the decision point: live runs showed the
    # model intermittently saying "logged it" after only this read (the history line
    # reads like a completed log). The description-level rule alone didn't hold.
    return ("ok: their history for this — NOT logged yet for today; if they ate it, "
            "call log_meal now with these numbers:\n" + "\n".join(lines))


MATCH_DINING_ITEM_TOOL = {
    "name": "match_dining_item",
    "description": (
        "Look up a meal in today's UC Berkeley dining-hall menu when it's plausibly "
        "dining-hall food (they name a hall, or context implies campus dining). "
        "Campus food gets looked up, not estimated: a match returns the menu item's "
        "real macros + serving size — log from those, scaled by how much they ate. "
        "No match or no menu data → just estimate normally. Say the menu is your "
        "source only when this tool returned the item you used."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string",
                            "description": "the food as the user described it, e.g. 'halal chicken bowl'"},
            "hall": {"type": "string", "enum": ["crossroads", "foothill", "clark_kerr", "cafe3"]},
            "meal_period": {"type": "string", "enum": ["breakfast", "lunch", "dinner", "brunch"]},
        },
        "required": ["description"],
    },
}


def handle_match_dining_item(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Read-only: today's menu candidates for a described meal. Both empty branches
    (no data scraped / nothing matched) are clean fallthrough answers."""
    from dining_scraper import match_dining_items

    query = (tool_input.get("description") or "").strip()
    if not query:
        return "error: description required"

    matches, had_data = match_dining_items(
        query, hall=tool_input.get("hall"), meal_period=tool_input.get("meal_period"))
    if not had_data:
        return ("no menu data for today (hall may be closed or not scraped yet) — "
                "estimate normally")
    if not matches:
        return f"no menu match for '{query}' — estimate normally"

    lines = []
    for it in matches:
        macros = [f"{it.calories or '?'}cal"]
        if it.protein_g is not None:
            macros.append(f"{round(it.protein_g)}g protein")
        if it.carbs_g is not None:
            macros.append(f"{round(it.carbs_g)}g carbs")
        if it.fat_g is not None:
            macros.append(f"{round(it.fat_g)}g fat")
        serving = f", per {it.serving_size}" if it.serving_size else ""
        lines.append(f"{it.item_name} ({it.hall} {it.meal_period}): "
                     f"{'/'.join(macros)}{serving}")
    return "ok: menu matches:\n" + "\n".join(lines)


USDA_FOOD_LOOKUP_TOOL = {
    "name": "usda_food_lookup",
    "description": (
        "Look up reference macros (per 100g, USDA FoodData Central) for an "
        "identifiable but GENERIC food — plain chicken breast, white rice, oatmeal, "
        "an apple — when there's no label, no history match, and it's not dining-hall "
        "food. Scale the per-100g numbers by your portion estimate (reference "
        "objects). NOT for branded or restaurant items — those aren't in this "
        "database. If it returns nothing or errors, just estimate normally. Cite "
        "USDA as your basis only when you used a returned entry."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "the generic food, e.g. 'grilled chicken breast'"},
        },
        "required": ["query"],
    },
}


_LOOKUP_STOPWORDS = {"raw", "cooked", "fresh", "plain", "the", "and", "with", "of", "deli",
                     "grilled", "boiled", "large", "small", "medium", "whole", "sliced"}


def _food_tokens(text: str) -> set:
    out = set()
    for w in re.split(r"[^a-z0-9]+", (text or "").lower()):
        if len(w) < 3 or w in _LOOKUP_STOPWORDS:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 3 else w)  # whites → white
    return out


def _logged_rows_overlapping(user_id: int, query: str) -> str:
    """Affordance-in-tool-result (macro-accuracy lesson): a lookup that lands on an
    ALREADY-LOGGED item is a correction waiting to be written. Live 2026-09-19 the coach
    re-estimated yesterday's muffin from two USDA hits, told the user the new number, and
    never edited the row. Lists today's + yesterday's active meals whose description shares
    a food word with the query, with ids + current numbers, and says what to do."""
    qt = _food_tokens(query)
    if not qt:
        return ""
    from timefmt import local_day_bounds
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return ""
        start, end = local_day_bounds(user)
        ystart = start - timedelta(days=1)
        rows = (active(session, Meal, user_id=user_id)
                .filter(Meal.eaten_at >= ystart, Meal.eaten_at < end)
                .order_by(Meal.eaten_at).all())
        hits, pending = [], {}
        for m in rows:
            if qt & _food_tokens(m.description):
                day = "today" if m.eaten_at >= start else "yesterday"
                hits.append(f"[id {m.id}] ({day}) {m.description} — currently {m.calories or 0}cal/"
                            f"{m.protein_g or 0}g protein")
                pending[int(m.id)] = f"{m.description} — {m.calories or 0}cal/{m.protein_g or 0}g"
    finally:
        session.close()
    if not hits:
        return ""
    # The loop's terminal check reads this: a reply that quotes a number for one of
    # these rows without an edit gets ONE code-forced follow-up (see agent_loop).
    _TURN_STATE.setdefault(user_id, {"reacted": False, "reply_to": None}) \
        .setdefault("pending_writeback", {}).update(pending)
    return ("\nalready logged (overlaps this lookup):\n" + "\n".join(hits) +
            "\nIf this lookup changes what one of those should be, call manage_log edit "
            "(entity meal, that id, fields calories/protein_g) BEFORE you quote the new "
            "number — a re-estimate that isn't written back leaves the day wrong. Then "
            "quote the total from context, never your own sum.")


def handle_usda_food_lookup(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Read-only external lookup. Every failure branch is a clean 'estimate
    normally' answer — the lookup adds information or gets out of the way."""
    from usda import search_usda, UsdaUnavailable

    query = (tool_input.get("query") or "").strip()
    if not query:
        return "error: query required"

    import config as _config
    if not _config.USDA_API_KEY:
        return "usda lookup not configured — estimate normally"

    try:
        results = search_usda(query)
    except UsdaUnavailable as e:
        logger.warning("USDA_LOOKUP_UNAVAILABLE user=%s q=%r reason=%s", user_id, query, e)
        return f"usda lookup unavailable ({e}) — estimate normally"

    if not results:
        return f"no usda match for '{query}' — estimate normally"

    lines = []
    for r in results:
        macros = [f"{r['calories']}cal" if r["calories"] is not None else "?cal"]
        for field, label in (("protein_g", "protein"), ("carbs_g", "carbs"), ("fat_g", "fat")):
            if r[field] is not None:
                macros.append(f"{r[field]}g {label}")
        lines.append(f"{r['description']} ({r['data_type']}, per 100g): {'/'.join(macros)}")
    return ("ok: usda entries (per 100g — scale by the portion you estimated):\n"
            + "\n".join(lines) + _logged_rows_overlapping(user_id, query))


# name -> handler. The loop consults this after checking the tool is enabled.
# ─── iMessage tapbacks + threaded replies (offered only on the iMessage channel) ─
# The WHEN is prompt (voice.md "Reactions and threaded replies"); this is the HOW.
# Message refs: the RECENT CONVERSATION block tags each of THEIR iMessages with
# [m<id>]; the tools take that ref and code maps it to the stored Photon id.
REACT_TOOL = {
    "name": "react_to_message",
    "description": (
        "Put an iMessage tapback on ONE of the user's messages, by its [m…] ref from "
        "RECENT CONVERSATION. emoji: love | like | dislike | laugh | emphasize | question, "
        "or a single raw emoji. A reaction REPLACES the text when the only honest reply is "
        "an acknowledgment (then end your turn with NO text). It never replaces an action "
        "(log the workout AND react) and never answers a question (that's a text). At most "
        "one per user turn, on the message that earned it. It never counts against the user."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "message_ref": {"type": "string", "description": "the [m…] ref of THEIR message, e.g. m3350"},
            "emoji": {"type": "string", "description": "love|like|dislike|laugh|emphasize|question, or one raw emoji"},
        },
        "required": ["message_ref", "emoji"],
    },
}

THREAD_REPLY_TOOL = {
    "name": "reply_in_thread",
    "description": (
        "Send THIS turn's text as a threaded iMessage reply quoting ONE of the user's "
        "messages (by [m…] ref). Use only when a plain reply would be ambiguous: a burst "
        "with two or more topics and you're answering one of them, or you're answering "
        "something from earlier than their latest message. Never on the latest message when "
        "it's the only topic; never for a coaching call-out. Call it, then write the text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "message_ref": {"type": "string", "description": "the [m…] ref of THEIR message to quote"},
        },
        "required": ["message_ref"],
    },
}

# The model cannot emit an empty message; this is how it says "the tapback was the
# reply". Code swallows it (and the natural-language variants a model drifts to).
REACTION_ONLY_SENTINEL = "[silent]"
_REACTION_ONLY_META = re.compile(
    r"^\W*(\[?silent\]?|no text( needed| required)?|turn ended[^.]*|nothing (else|more) to (add|say)|"
    r"\(?reaction only\)?|—|-)\W*$", re.IGNORECASE)


def is_reaction_only_text(text: str, reacted: bool) -> bool:
    """True when nothing should be sent: the exact sentinel ALWAYS (a tool result may
    ask for it — start_workout_session sends its own text + card; live 2026-09-14
    the literal '[silent]' was texted to the founder), and, after a reaction, an
    empty reply or the meta note a model drifts to."""
    t = (text or "").strip()
    if t.lower() == REACTION_ONLY_SENTINEL:
        return True
    if not reacted:
        return False
    return t == "" or bool(_REACTION_ONLY_META.match(t))


# A tool call written INTO the visible text ("react_to_message 👍") — a documented
# Opus failure mode at low effort. Never send it; execute what it meant.
_TOOL_CALL_IN_TEXT = re.compile(r"^\W*(react_to_message|reply_in_thread)\b[\s:(\[\"']*(.*?)[\s)\]\"']*$",
                                re.IGNORECASE | re.DOTALL)


def leaked_tool_call(text: str):
    """('react_to_message', '👍') / ('react_to_message', 'like') / ('reply_in_thread', 'm12')
    when the whole text is a tool invocation the model wrote as prose; else None."""
    m = _TOOL_CALL_IN_TEXT.match((text or "").strip())
    if not m:
        return None
    arg = re.sub(r"^(message_ref|emoji)\s*[=:]\s*", "", m.group(2).strip().split("\n")[0]).strip(" ,")
    # "m3350 like" / "like m3350" → keep the emoji-ish token for react
    if m.group(1).lower() == "react_to_message":
        toks = [re.sub(r"^(message_ref|emoji)\s*[=:]\s*", "", t.strip(" ,")) for t in arg.split() if t.strip(" ,")]
        emo = next((t for t in reversed(toks) if not re.match(r"^m?\d+$", t)), toks[-1] if toks else "like")
        return ("react_to_message", emo)
    return ("reply_in_thread", arg)


_EMOJI_ONLY = re.compile(r"^[\s\u200d\ufe0f\U0001F300-\U0001FAFF\u2600-\u27BF\u2B50\u2B55\u203C\u2049\u2764]+$")


def is_single_emoji_text(text: str) -> bool:
    """A text that is nothing but one emoji (a ❤️ sent as a message). On iMessage that
    is a tapback that lost its way — code turns it into one."""
    t = (text or "").strip()
    return 0 < len(t) <= 4 and bool(_EMOJI_ONLY.match(t))


def latest_inbound_imessage_sid(user_id: int):
    """The Photon id of their latest iMessage — None if that message is a question
    (a converted ❤️/leaked tool call must not tapback a question either)."""
    session = get_session()
    try:
        row = (session.query(Message.provider_sid, Message.body)
               .filter(Message.user_id == user_id, Message.direction == "in",
                       Message.channel == "imessage", Message.provider_sid.isnot(None))
               .order_by(Message.id.desc()).first())
        if not row or is_question_message(row[1]):
            return None
        return row[0]
    finally:
        session.close()


# Per-turn state: one turn per user at a time (the inbound buffer serializes them).
_TURN_STATE: dict = {}


def begin_turn(user_id: int) -> None:
    _TURN_STATE[user_id] = {"reacted": False, "reply_to": None}


def pop_turn_state(user_id: int) -> dict:
    return _TURN_STATE.pop(user_id, {"reacted": False, "reply_to": None})


def peek_turn_state(user_id: int) -> dict:
    return _TURN_STATE.get(user_id, {"reacted": False, "reply_to": None})


def _clear_pending_writeback(user_id: int, entry_id) -> None:
    """A meal row was edited/deleted this turn — it no longer needs a write-back."""
    pend = _TURN_STATE.get(user_id, {}).get("pending_writeback")
    if pend:
        try:
            pend.pop(int(entry_id), None)
        except (TypeError, ValueError):
            pass


def _resolve_message_ref(user_id: int, ref: str, *, with_body: bool = False):
    """[m3350] / m3350 / 3350 → (Message id, provider_sid[, body]) for one of THEIR iMessages."""
    import re as _re
    m = _re.search(r"(\d+)", ref or "")
    if not m:
        return (None, None, None) if with_body else (None, None)
    mid = int(m.group(1))
    session = get_session()
    try:
        row = (session.query(Message.id, Message.provider_sid, Message.body)
               .filter(Message.id == mid, Message.user_id == user_id,
                       Message.direction == "in", Message.channel == "imessage")
               .first())
    finally:
        session.close()
    if not (row and row[1]):
        return (None, None, None) if with_body else (None, None)
    return (row[0], row[1], row[2]) if with_body else (row[0], row[1])


def is_question_message(body: str) -> bool:
    """Founder's rule 3, made mechanical: a message that asks something never gets a
    tapback — it gets an answer. A '?' anywhere is enough (rhetorical counts: the
    live eval laugh-reacted to 'why does everyone think c104 means data science?')."""
    return "?" in (body or "")


def handle_react_to_message(user_id: int, tool_input: dict, *, message_id=None) -> str:
    from sms import react_to_message, TAPBACKS
    ref = (tool_input.get("message_ref") or "").strip()
    emoji = (tool_input.get("emoji") or "").strip()
    if not emoji:
        return "error: emoji required (love|like|dislike|laugh|emphasize|question or one emoji)"
    if emoji.lower() not in TAPBACKS and len(emoji) > 4:
        return "error: emoji must be a tapback name or a single emoji"
    st = peek_turn_state(user_id)
    if st.get("reacted"):
        return "error: already reacted this turn — at most one reaction per user turn"
    mid, sid, body = _resolve_message_ref(user_id, ref, with_body=True)
    if not sid:
        return f"error: no iMessage of theirs matches ref {ref!r} — use an [m…] ref from RECENT CONVERSATION"
    if is_question_message(body):
        return f"error: m{mid} is a question — questions get an answer in text, never a tapback (rule 3)"
    ok = react_to_message(user_id, sid, emoji)
    if ok:
        _TURN_STATE.setdefault(user_id, {"reacted": False, "reply_to": None})["reacted"] = True
        return (f"ok: reacted {TAPBACKS.get(emoji.lower(), emoji)} on m{mid}. If the reaction IS the whole "
                f"reply, respond with exactly {REACTION_ONLY_SENTINEL} and nothing else — never a note like "
                f"'no text needed'. Otherwise write the text.")
    return f"error: reaction on m{mid} failed (logged; not a strike) — reply with text instead"


def handle_reply_in_thread(user_id: int, tool_input: dict, *, message_id=None) -> str:
    ref = (tool_input.get("message_ref") or "").strip()
    mid, sid = _resolve_message_ref(user_id, ref)
    if not sid:
        return f"error: no iMessage of theirs matches ref {ref!r}"
    _TURN_STATE.setdefault(user_id, {"reacted": False, "reply_to": None})["reply_to"] = sid
    return f"ok: this turn's text will be sent as a threaded reply to m{mid}. Now write the text."


# ─── web_search (Anthropic SERVER-SIDE tool — no client handler) ─────────────
# Registered here with the other tools so every surface (coach loop, heartbeat,
# onboarding) offers the identical definition. Anthropic runs the search inline and
# returns results as content blocks; output/query hygiene are prompt rules in
# prompts/identity.md (one detail in your own words, no links, no user PII).
WEB_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": config.WEB_SEARCH_MAX_USES,
    # The beta is Berkeley: localize results (RSF hours, GBC, a 70 midterm thread)
    # without the model having to type "berkeley" into every query.
    "user_location": {"type": "approximate", "city": "Berkeley", "region": "California",
                      "country": "US", "timezone": "America/Los_Angeles"},
    # Hard-block known SEO-spam / content-farm hosts so they can't reach the model at
    # all. This is belt-and-suspenders — the general fix is the source-quality rule in
    # voice.md (prefer the OFFICIAL source, discount content farms). These are the
    # hijacked proxy/mirror hosts that fed a WRONG "RSF closes at 8pm" (2026-09-18);
    # add offenders here as they surface, extendable via WEB_SEARCH_BLOCKED_DOMAINS.
    "blocked_domains": config.WEB_SEARCH_BLOCKED_DOMAINS,
}


def web_search_queries(content) -> list[str]:
    """The search queries the model issued in one response: every server_tool_use
    (or tool_use) block named web_search, in order. Empty when it didn't search."""
    out = []
    for b in content or []:
        if (getattr(b, "type", None) in ("server_tool_use", "tool_use")
                and getattr(b, "name", None) == "web_search"):
            q = (getattr(b, "input", None) or {}).get("query")
            if q:
                out.append(q)
    return out


def log_web_search_queries(user_id, content, site: str) -> list[str]:
    """Log each web_search query with the user id (WEB_SEARCH_QUERY) so we can see
    what the coach reaches for, and how often. Returns the queries."""
    queries = web_search_queries(content)
    for q in queries:
        logger.info("WEB_SEARCH_QUERY user=%s site=%s query=%r", user_id, site, q)
    return queries


SET_DAY_RESET_TOOL = {
    "name": "set_day_reset",
    "description": (
        "Shift when THIS user's nutrition day rolls over, ONLY when they explicitly ask "
        "for it (\"count my after-midnight meals as the day before\", \"my day should "
        "start at 4am\", \"reset when I wake up around 10\"). Pass `hour` = the local "
        "hour (0–11) the new day begins; e.g. 4 means the day runs 4am→4am, so a 12:20am "
        "meal counts for the day that started the previous morning. Default is 0 "
        "(midnight) — pass 0 to put them back on the standard day. Never call this on "
        "your own initiative; the user has to state the preference."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"hour": {"type": "integer", "minimum": 0, "maximum": 11}},
        "required": ["hour"],
    },
}


def handle_set_day_reset(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Set the user's nutrition-day rollover hour (0–11 local; 0 = midnight). Recomputes
    today's totals against the new window so the change is reflected immediately."""
    raw = (tool_input or {}).get("hour")
    try:
        hour = int(raw)
    except (TypeError, ValueError):
        return f"error: hour must be an integer 0–11, got {raw!r}"
    if not (0 <= hour <= 11):
        return f"error: hour must be 0–11 (the early-morning rollover), got {hour}"
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return "error: user not found"
        user.day_reset_hour = hour
        session.commit()
    finally:
        session.close()
    recompute_daily_totals(user_id)   # re-window today's totals under the new reset
    logger.info("SET_DAY_RESET user=%s hour=%s", user_id, hour)
    if hour == 0:
        return "ok: nutrition day resets at midnight (standard)"
    return f"ok: nutrition day now resets at {hour}am local — meals before then count for the previous day"


_HANDLERS = {
    "react_to_message": handle_react_to_message,
    "set_day_reset": handle_set_day_reset,
    "reply_in_thread": handle_reply_in_thread,
    "remember": handle_remember,
    "log_workout": handle_log_workout,
    "manage_log": handle_manage_log,
    "log_meal": handle_log_meal,
    "set_targets": lambda user_id, tool_input, **kw: handle_set_targets(user_id, tool_input, **kw),
    "log_weight": lambda user_id, tool_input, **kw: handle_log_weight(user_id, tool_input, **kw),
    "start_workout_session": lambda user_id, tool_input, **kw: handle_start_workout_session(user_id, tool_input, **kw),
    "log_event": handle_log_event,
    "set_reminder": handle_set_reminder,
    "save_routine": handle_save_routine,
    "cancel_reminder": handle_cancel_reminder,
    "get_dining_menu": handle_get_dining_menu,
    "match_meal_history": handle_match_meal_history,
    "match_dining_item": handle_match_dining_item,
    "usda_food_lookup": handle_usda_food_lookup,
}


def dispatch_tool(name: str, tool_input: dict, user_id: int, *, message_id=None) -> str:
    handler = _HANDLERS.get(name)
    if handler is None:
        return f"error: unknown tool {name!r}"
    try:
        return handler(user_id, tool_input, message_id=message_id)
    except Exception as e:  # a tool failure must be reported, never claimed as success
        logger.exception("TOOL_FAILED name=%s user=%s", name, user_id)
        return f"error: {name} failed: {e}"
