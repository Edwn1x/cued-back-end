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

from datetime import datetime, timezone

from sqlalchemy.orm.attributes import flag_modified

from models import (get_session, User, Workout, Meal, Event, active,
                    recompute_daily_totals, confirm_workout_today)
from memory import apply_facts, invalidate_entry, CATEGORIES

logger = logging.getLogger("cued.agent_tools")


REMEMBER_TOOL = {
    "name": "remember",
    "description": (
        "Save, update, or invalidate a DURABLE fact about the user (preferences, "
        "goals, schedule, constraints, life context). Use it when you learn "
        "something worth remembering across future conversations — not for "
        "transient chatter. add: a new fact. update: supersede an existing fact "
        "(pass the new text; the old value is preserved in history). invalidate: "
        "close a fact that's no longer true (e.g. an injury healed) — pass its "
        "entry_id. Do NOT log meals or workouts here (separate tools)."
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
        "advances the split pointer (code-mediated). Pass split_day when the user "
        "names it ('did legs') — that's a confirmed advance; omit it for a plain "
        "'just worked out' and code infers the next day. Include exercises the user "
        "mentioned with any sets/reps/weight."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "split_day": {"type": "string",
                          "description": "the named day if the user said it: push/pull/legs/upper/lower/full_body"},
            "exercises": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "sets": {"type": "integer"},
                        "reps": {"type": "integer"},
                        "weight": {"type": "number"},
                    },
                    "required": ["name"],
                },
            },
            "notes": {"type": "string", "description": "anything the user said about the session"},
        },
        "required": [],
    },
}


def handle_log_workout(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Create a Workout and advance the split pointer under the Phase-1 policy."""
    from split_pointer import advance_split_pointer, parse_named_split_day

    exercises = tool_input.get("exercises") or []
    split_day = (tool_input.get("split_day") or "").strip().lower() or None
    notes = tool_input.get("notes")

    session = get_session()
    try:
        user = (session.query(User).filter(User.id == user_id)
                .with_for_update().one_or_none())
        if not user:
            return "error: user not found"
        w = Workout(user_id=user_id, workout_type=(split_day or "logged"),
                    exercises=exercises, user_notes=notes, completed=True)
        session.add(w)
        session.commit()
        wid = w.id
    finally:
        session.close()

    confirm_workout_today(user_id)
    # named day -> confirmed advance; else infer from the notes, then the cycle.
    day = split_day or parse_named_split_day(notes or "")
    pointer = advance_split_pointer(user_id, named_day=day)
    logger.info("LOG_WORKOUT_TOOL user=%s workout_id=%s split_day=%s pointer=%s",
                user_id, wid, split_day, pointer)
    return (f"ok: logged workout (id={wid}, {len(exercises)} exercises)"
            + (f", split pointer now {pointer['day']} ({pointer['source']})" if pointer else ""))


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
                       "description": "for edit: fields to update, e.g. {\"calories\": 400, \"protein_g\": 30}"},
        },
        "required": ["action"],
    },
}

_ENTITY_MODEL = {"meal": Meal, "workout": Workout, "event": Event}
_EDITABLE = {
    "meal": {"calories", "protein_g", "carbs_g", "fat_g", "description", "notes"},
    "workout": {"workout_type", "user_notes"},
    "event": {"event_type", "ends_at"},
}


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
            lines = [f"meal [id {m.id}] {m.description} — {m.calories or 0}cal/{m.protein_g or 0}g"
                     for m in meals]
            lines += [f"workout [id {w.id}] {w.workout_type}" for w in workouts]
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
            if entity == "meal":
                recompute_daily_totals(user_id)
            logger.info("MANAGE_LOG user=%s delete %s id=%s", user_id, entity, entry_id)
            return f"ok: deleted {entity} id={entry_id}"

        # edit
        fields = tool_input.get("fields") or {}
        applied = {}
        for k, v in fields.items():
            if k in _EDITABLE.get(entity, set()):
                setattr(row, k, v)
                applied[k] = v
        if not applied:
            return f"error: no editable fields in {list(fields)} for {entity}"
        session.commit()
        if entity == "meal":
            recompute_daily_totals(user_id)
        logger.info("MANAGE_LOG user=%s edit %s id=%s fields=%s", user_id, entity, entry_id, applied)
        return f"ok: edited {entity} id={entry_id} ({applied})"
    finally:
        session.close()


# name -> handler. The loop consults this after checking the tool is enabled.
_HANDLERS = {
    "remember": handle_remember,
    "log_workout": handle_log_workout,
    "manage_log": handle_manage_log,
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
