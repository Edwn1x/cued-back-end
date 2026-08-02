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
        "transient chatter. A durable detail you read off an IMAGE counts the same "
        "as one they typed (a package weight, a label's macros, food on hand not "
        "yet eaten): save it this turn — the image is gone next turn, and a detail "
        "you only spoke back is not saved. add: a new fact. update: supersede an "
        "existing fact (pass the new text; the old value is preserved in history). "
        "invalidate: close a fact that's no longer true (e.g. an injury healed, "
        "on-hand food now eaten and logged) — pass its entry_id. Do NOT log eaten "
        "meals or completed workouts here (separate tools)."
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
        "list — one call is cheaper than several and less likely to truncate."
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
                notes=(f"saw_similar={saw}" if saw else None), eaten_at=_naive_utcnow(),
            )
            session.add(meal)
            session.flush()
            logged.append((meal.id, meal.description, it.get("calories") or 0,
                           it.get("protein_g") or 0, saw))
        session.commit()
    finally:
        session.close()

    recompute_daily_totals(user_id)  # once, after all inserts
    for mid, desc, _cal, _pro, saw in logged:
        if saw:
            logger.info("LOG_MEAL_SAW_SIMILAR user=%s meal_id=%s saw=%s (model logged as distinct serving)",
                        user_id, mid, saw)
        logger.info("LOG_MEAL user=%s meal_id=%s desc=%r", user_id, mid, desc[:40])

    if len(logged) == 1:
        mid, _desc, cal, pro, saw = logged[0]
        return f"ok: logged meal id={mid} ({cal}cal/{pro}g)" + (f" [saw_similar={saw}]" if saw else "")
    ids = [m for m, _d, _c, _p, _s in logged]
    total_cal = sum(c for _m, _d, c, _p, _s in logged)
    return f"ok: logged {len(logged)} items (ids {ids}, {total_cal}cal total)"


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


def _resolve_local_date(tz: ZoneInfo, date_str, *, strict: bool = False) -> date:
    """Resolve 'today' (default) / 'tomorrow' / 'YYYY-MM-DD' to a date in tz.
    Non-strict (log_event): unparseable input silently falls back to today, the
    existing behavior. Strict (manage_log date edit): unparseable input raises
    ValueError — a silent wrong-day edit is worse than a rejected one."""
    today_local = datetime.now(tz).date()
    ds = (date_str or "today").strip().lower()
    if ds == "tomorrow":
        return today_local + timedelta(days=1)
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
            if entity == "meal":
                recompute_daily_totals(user_id)
            logger.info("MANAGE_LOG user=%s delete %s id=%s", user_id, entity, entry_id)
            return f"ok: deleted {entity} id={entry_id}"

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


MATCH_MEAL_HISTORY_TOOL = {
    "name": "match_meal_history",
    "description": (
        "Check whether the user has logged this meal (or something like it) before, "
        "BEFORE estimating a meal that plausibly repeats — a repeat meal has a "
        "personal ground truth (their portions, their prep) that beats a generic "
        "estimate. A confident match: lean on their usual numbers and tell them "
        "you're doing so ('using your usual for this'). An ambiguous match or a "
        "different portion: ask one short question or estimate fresh — never "
        "silently assume the prior fits. Never claim history this tool didn't return."
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
    return "ok: their history for this:\n" + "\n".join(lines)


# name -> handler. The loop consults this after checking the tool is enabled.
_HANDLERS = {
    "remember": handle_remember,
    "log_workout": handle_log_workout,
    "manage_log": handle_manage_log,
    "log_meal": handle_log_meal,
    "log_event": handle_log_event,
    "get_dining_menu": handle_get_dining_menu,
    "match_meal_history": handle_match_meal_history,
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
