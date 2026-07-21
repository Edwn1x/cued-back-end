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

from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm.attributes import flag_modified

from models import (get_session, User, Workout, Meal, Event, DiningMenuItem, active,
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

LOG_MEAL_TOOL = {
    "name": "log_meal",
    "description": (
        "Log a meal the user reports eating. READ-BEFORE-WRITE: first check "
        "'TODAY'S LOGGED MEALS' in your context. If this is the SAME serving already "
        "logged, do NOT log it again — reference the existing entry instead. If it's "
        "a genuine SECOND serving of something similar, log it and set saw_similar to "
        "the id(s) of the similar entries you saw (so the choice is auditable). If "
        "you're unsure whether it's a repeat, ask the user one short question before "
        "logging — never guess in either direction. Include macros if you can estimate them."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string"},
            "calories": {"type": "integer"},
            "protein_g": {"type": "integer"},
            "carbs_g": {"type": "integer"},
            "fat_g": {"type": "integer"},
            "saw_similar": {"type": "array", "items": {"type": "integer"},
                            "description": "ids of similar already-logged meals you saw and judged to be a distinct serving"},
        },
        "required": ["description"],
    },
}


def handle_log_meal(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Create a Meal (the model already did the read-before-write judgment) and
    recompute today's totals. Records saw_similar so an intentional near-duplicate
    is auditable and correctable via manage_log."""
    description = (tool_input.get("description") or "").strip()
    if not description:
        return "error: description required"
    saw_similar = tool_input.get("saw_similar") or []

    session = get_session()
    try:
        note = f"saw_similar={saw_similar}" if saw_similar else None
        meal = Meal(
            user_id=user_id, description=description,
            calories=tool_input.get("calories"), protein_g=tool_input.get("protein_g"),
            carbs_g=tool_input.get("carbs_g"), fat_g=tool_input.get("fat_g"),
            source="text", log_type="user_reported", notes=note,
            eaten_at=_naive_utcnow(),
        )
        session.add(meal)
        session.commit()
        mid = meal.id
    finally:
        session.close()

    recompute_daily_totals(user_id)
    if saw_similar:
        logger.info("LOG_MEAL_SAW_SIMILAR user=%s meal_id=%s saw=%s (model logged as distinct serving)",
                    user_id, mid, saw_similar)
    logger.info("LOG_MEAL user=%s meal_id=%s desc=%r", user_id, mid, description[:40])
    return (f"ok: logged meal id={mid} ({tool_input.get('calories') or 0}cal/"
            f"{tool_input.get('protein_g') or 0}g)"
            + (f" [saw_similar={saw_similar}]" if saw_similar else ""))


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
        "be wrong (it never expires) and gets crowded out."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "what it is, e.g. 'founder summit'"},
            "starts_at": {"type": "string", "description": "local start time 'HH:MM' (24h, optional)"},
            "ends_at": {"type": "string", "description": "local end time 'HH:MM' (24h, optional)"},
            "date": {"type": "string", "description": "'today' (default), 'tomorrow', or 'YYYY-MM-DD'"},
        },
        "required": ["description"],
    },
}


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
    today_local = datetime.now(tz).date()
    ds = (date_str or "today").strip().lower()
    if ds == "tomorrow":
        d = today_local + timedelta(days=1)
    elif ds in ("", "today"):
        d = today_local
    else:
        try:
            d = date.fromisoformat(ds)
        except ValueError:
            d = today_local
    local_dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def handle_log_event(user_id: int, tool_input: dict, *, message_id=None) -> str:
    """Persist a dated schedule item as an Event (source='model'), so it auto-expires
    with its day and surfaces in TODAY'S EVENTS for the reactive loop AND the heartbeat.
    Dated items belong here, not in the `schedule` memory category — that's the
    burn-in fix (schedule facts were landing in memory and getting evicted)."""
    from events import record_event
    desc = (tool_input.get("description") or "").strip()
    if not desc:
        return "error: description required"

    session = get_session()
    try:
        user = session.get(User, user_id)
        tz_str = (user.user_timezone if user else None) or "America/Los_Angeles"
    finally:
        session.close()

    starts = _parse_local_dt(tz_str, tool_input.get("date"), tool_input.get("starts_at"))
    ends = _parse_local_dt(tz_str, tool_input.get("date"), tool_input.get("ends_at"))
    eid = record_event(user_id, "scheduled", ends_at=ends, source="model",
                       raw_text=desc, occurred_at=starts)
    logger.info("LOG_EVENT user=%s event_id=%s desc=%r start=%s end=%s",
                user_id, eid, desc[:40], starts, ends)
    when = f" at {tool_input.get('starts_at')}" if tool_input.get("starts_at") else ""
    return f"ok: noted '{desc}'{when} (event id={eid})"


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


# name -> handler. The loop consults this after checking the tool is enabled.
_HANDLERS = {
    "remember": handle_remember,
    "log_workout": handle_log_workout,
    "manage_log": handle_manage_log,
    "log_meal": handle_log_meal,
    "log_event": handle_log_event,
    "get_dining_menu": handle_get_dining_menu,
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
