"""
The program: a week the code builds for THIS person and the coach explains.

Founder, 2026-10-18: "make the overall coaching and program building actually better so it
actually comes up with a schedule, a training program that is actually good, that fits
each user, tailored, and that explains it to the user and talks to them like a real coach."
Live before this (user 40, 17, never trained, 4 nights, machines, wrist): split "none" →
the same five-machine full-body day every session; "4" days with no weekdays → the
briefing could only guess "rest day"; nothing ever said WHY.

build_program(user) is pure: a dict from the typed columns —
  split (upper_lower / ppl / full_body / rotation), cycle (day keys in order),
  days {weekday → day key}, times {weekday → HH:MM}, exercises {day key → [labels]},
  scheme (sets × reps band + why), progression (how loads move), cardio, constraints,
  rationale (voice lines), version/built_at.
apply_program(user_id) writes users.program and MIRRORS it into the columns the rest of the
system already reads (current_split, split_days, confirmed_training_days, workout_times), so
the card builder, the split pointer, the briefing and reminders all agree with the brief.
program_brief(user, program) is the coach's explanation, in voice, as 2–3 bubbles.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from workouts.templates import (day_template, day_label, normalize_template_key, BODYWEIGHT_EQUIPMENT,
                                MACHINE_EQUIPMENT)

logger = logging.getLogger("cued.program")

PROGRAM_VERSION = 1
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = {"mon": "monday", "tue": "tuesday", "wed": "wednesday", "thu": "thursday", "fri": "friday",
             "sat": "saturday", "sun": "sunday"}
# Default weekday patterns by days/week: a rest day between hard days where the count allows,
# the weekend free unless they need it. They confirm or swap in the brief.
DEFAULT_DAY_PATTERNS = {
    1: ["mon"], 2: ["mon", "thu"], 3: ["mon", "wed", "fri"], 4: ["mon", "tue", "thu", "fri"],
    5: ["mon", "tue", "wed", "fri", "sat"], 6: ["mon", "tue", "wed", "thu", "fri", "sat"], 7: list(DAYS),
}
NO_SPLIT = {None, "", "none"}


# ─── reading the person ──────────────────────────────────────────────────────

def level_of(user) -> str:
    lvl = (getattr(user, "experience", None) or "").strip().lower()
    if lvl in ("intermediate", "advanced"):
        return lvl
    return "beginner"          # "none", "beginner", unknown


def days_per_week(user) -> int:
    named = named_days(user)
    if named:
        return len(named)
    from macro_calculator import training_days_per_week
    return max(1, min(7, training_days_per_week(getattr(user, "workout_days", None), default=3)))


def named_days(user) -> list[str] | None:
    """Weekdays they actually named (confirmed_training_days / workout_days), in week order.
    A list SHORTER than the weekly count they stated is a mention, not a schedule — live
    2026-10-19 (user 40): the per-turn decisions extractor wrote confirmed_training_days='wed'
    off "I can't go to the gym till Wednesday" while workout_days was '4'; the builder would
    have made that a one-day program."""
    import re
    count = None
    m = re.fullmatch(r"\s*(\d)\s*", str(getattr(user, "workout_days", None) or ""))
    if m:
        count = int(m.group(1))
    for col in ("confirmed_training_days", "workout_days"):
        raw = (getattr(user, col, None) or "").lower()
        found = [t[:3] for t in re.split(r"[\s,/&+]+", raw) if t[:3] in DAYS]
        if found:
            seen = []
            for d in DAYS:
                if d in found and d not in seen:
                    seen.append(d)
            if count and len(seen) < count:
                logger.info("PROGRAM_NAMED_DAYS_IGNORED user=%s named=%s count=%s col=%s",
                            getattr(user, "id", None), seen, count, col)
                return None
            return seen
    return None


def goal_of(user) -> str:
    return (getattr(user, "goal", None) or "general_fitness").lower()


def equipment_of(user) -> str:
    eq = (getattr(user, "equipment", None) or "").strip().lower()
    if eq in BODYWEIGHT_EQUIPMENT:
        return "bodyweight"
    if eq in MACHINE_EQUIPMENT:
        return "machines"
    return "full_gym"


def constraints_of(user) -> list[str]:
    """Plain statements of what the program honours (and why), from typed columns."""
    out = []
    eq = equipment_of(user)
    inj = (getattr(user, "injuries", None) or "").strip()
    if eq == "machines":
        out.append("machines only — no barbell work" + (" while the wrist settles" if "wrist" in inj.lower() else ""))
    elif eq == "bodyweight":
        out.append("bodyweight only — no gym")
    if inj and inj.lower() not in ("none", "no", "n/a"):
        out.append(f"working around: {inj[:80]}")
    return out


# ─── choosing the split ──────────────────────────────────────────────────────

def _stated_cycle(user) -> tuple[str, list[str]] | None:
    """A split THEY run (stated, or a pasted routine) wins over the builder's default."""
    from split_pointer import cycle_for
    split = (getattr(user, "current_split", None) or "").strip().lower()
    cyc = [k for k in (normalize_template_key(d) for d in cycle_for(user)) if k]
    if split not in NO_SPLIT and cyc:
        return split, cyc
    return None


def choose_split(n: int, level: str, user=None, *, respect_stated: bool = True) -> tuple[str, list[str]]:
    """(split name, cycle of day keys). The cycle repeats across the week's training days.
    `respect_stated=False`: ignore current_split (it was the builder's own earlier pick,
    mirrored there — a day-count change must be free to pick again)."""
    stated = _stated_cycle(user) if (user is not None and respect_stated) else None
    if stated:
        return stated
    if n <= 2:
        return "full_body", ["full_body"]
    if n == 3:
        return ("full_body", ["full_body"]) if level == "beginner" else ("ppl", ["push", "pull", "legs"])
    if n == 4:
        return "upper_lower", ["upper", "lower"]
    if n == 5:
        return "rotation", ["push", "pull", "legs", "upper", "lower"]
    return "ppl", ["push", "pull", "legs"]


def assign_days(cycle: list[str], weekdays: list[str]) -> dict[str, str]:
    """{weekday → day key}: the cycle laid across the training days in week order."""
    out = {}
    for i, d in enumerate(weekdays):
        out[d] = cycle[i % len(cycle)]
    return out


def times_for(user, weekdays: list[str]) -> dict[str, str]:
    """{weekday → HH:MM}: by-day times when they gave them, else their one time (or '')."""
    from training_time import slot_for
    out = {}
    for d in weekdays:
        t = slot_for(user, d) or (getattr(user, "workout_time", None) or "")
        out[d] = t if _is_hhmm(t) else ""
    return out


def _is_hhmm(t: str) -> bool:
    import re
    return bool(re.fullmatch(r"\d{1,2}:\d{2}", (t or "").strip()))


# ─── the scheme, progression, cardio ─────────────────────────────────────────

def scheme_for(level: str, goal: str) -> dict:
    if level == "beginner":
        return {"sets": 3, "reps": "10-12", "why": "learning the movements — moderate reps, clean form, no grinding"}
    if level == "intermediate":
        return {"sets": 4, "reps": "6-10", "why": "compounds heavier for 6-8, accessories 10-12"}
    return {"sets": 4, "reps": "4-8", "why": "strength blocks on the compounds, volume on the rest"}


def progression_for(level: str) -> dict:
    return {"rule": "double progression",
            "text": ("hit every rep on every set → the weight goes up next time (the card already knows). "
                     "miss reps → same weight, get the reps first"),
            "review_weeks": 4 if level == "beginner" else 6}


def cardio_for(user, cycle: list[str]) -> dict | None:
    """A finisher on lower/legs days (or every day on full body) when they like one or the
    goal is fat loss. Reads stated preference from memory/training text when present."""
    prefs = _memory_text(user)
    what = None
    for name in ("stairmaster", "stair master", "treadmill", "incline walk", "bike", "rowing", "elliptical", "jump rope"):
        if name in prefs:
            what = "stairmaster" if "stair" in name else name
            break
    if not what and "fat_loss" in goal_of(user):
        what = "incline walk"
    if not what:
        return None
    on = [k for k in cycle if k in ("lower", "legs")] or [cycle[-1]]
    return {"what": what, "minutes": 15, "on": sorted(set(on), key=cycle.index)}


def _memory_text(user) -> str:
    prof = getattr(user, "user_profile_memory", None) or {}
    out = []
    for cat in ("training_preferences", "goals"):
        for e in prof.get(cat) or []:
            if isinstance(e, dict):
                out.append((e.get("text") or "").lower())
    return " ".join(out)


# ─── the program ─────────────────────────────────────────────────────────────

def _split_is_builders(user) -> bool:
    """True when current_split is just the builder's earlier pick mirrored back (so a
    rebuild may choose again); False when THEY stated it (onboarding / adjust_program)."""
    prev = getattr(user, "program", None) or {}
    cur = (getattr(user, "current_split", None) or "").strip().lower()
    return bool(prev) and prev.get("split_source") == "builder" and cur in (prev.get("split"), "custom")


def build_program(user, *, weekdays: list[str] | None = None, respect_stated: bool | None = None) -> dict:
    """Pure. `weekdays` overrides the day choice (the user's confirmation / swap)."""
    level = level_of(user)
    goal = goal_of(user)
    n = len(weekdays) if weekdays else days_per_week(user)
    if respect_stated is None:
        respect_stated = not _split_is_builders(user)
    split, cycle = choose_split(n, level, user, respect_stated=respect_stated)
    days = weekdays or named_days(user) or DEFAULT_DAY_PATTERNS[max(1, min(7, n))]
    days = [d for d in DAYS if d in days]
    assigned = assign_days(cycle, days)
    exercises = {}
    for key in cycle:
        try:
            exercises[key] = [t.label for t in day_template(user, key)]
        except KeyError:
            exercises[key] = []
    cardio = cardio_for(user, cycle)
    prog = {
        "version": PROGRAM_VERSION,
        "built_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds"),
        "level": level, "goal": goal, "equipment": equipment_of(user),
        "days_per_week": n, "split": split, "cycle": cycle,
        "days": assigned, "times": times_for(user, days),
        "exercises": exercises,
        "scheme": scheme_for(level, goal), "progression": progression_for(level),
        "cardio": cardio, "constraints": constraints_of(user),
        "days_source": "stated" if (weekdays or named_days(user)) else "default",
        "split_source": "user" if (respect_stated and _stated_cycle(user)) else "builder",
    }
    prog["rationale"] = rationale(prog, user)
    return prog


def rationale(prog: dict, user) -> list[str]:
    """Why this program, in voice — the lines the brief and the coach reuse."""
    n, split, level = prog["days_per_week"], prog["split"], prog["level"]
    lines = []
    if split == "upper_lower":
        lines.append(f"{n} days → upper/lower: every muscle gets hit twice a week, which is the sweet spot for {n} days")
    elif split == "full_body" and n <= 3:
        lines.append(f"{n} days → full body each time: as a {'beginner' if level == 'beginner' else 'lifter'} u grow fastest hitting everything {n}x a week, not splitting it thin")
    elif split == "ppl":
        lines.append(f"{n} days → push / pull / legs: each day owns a pattern, so nothing's half-baked")
    elif split == "rotation":
        lines.append("5 days → push, pull, legs, then upper and lower: everything twice, one real rest day")
    else:
        lines.append(f"ur own split ({day_label(split)}), kept as u run it")
    if prog["equipment"] == "machines":
        lines.append("all machines for now" + (" — nothing on the wrist til it's solid" if any("wrist" in c for c in prog["constraints"]) else " — u get strong on these first, the bar can wait"))
    elif prog["equipment"] == "bodyweight":
        lines.append("bodyweight versions of everything — progress is reps, not plates")
    lines.append(f"{prog['scheme']['sets']} sets of {prog['scheme']['reps']}: {prog['scheme']['why']}")
    if prog.get("cardio"):
        c = prog["cardio"]
        lines.append(f"{c['minutes']} min {c['what']} at the end of {'/'.join(day_label(k) for k in c['on'])} days")
    lines.append(f"first {prog['progression']['review_weeks']} weeks we run it as is, then we look at what moved")
    return lines


def describe_week(prog: dict) -> str:
    """'mon upper · tue lower · thu upper · fri lower' (+ ' at 10:30pm' when one time)."""
    parts = [f"{d} {day_label(prog['days'][d])}" for d in DAYS if d in prog["days"]]
    times = {t for t in prog.get("times", {}).values() if t}
    tail = ""
    if len(times) == 1:
        from workouts.start import _when
        tail = f" at {_when(next(iter(times)))}"
    return " · ".join(parts) + tail


def program_brief(user, prog: dict) -> list[str]:
    """The coach explaining the program: 2–3 bubbles, voice, ending on the one question
    they can answer in a word (the nights)."""
    name = getattr(user, "name", None) or "u"
    week = describe_week(prog)
    b1 = f"ok {name.lower()}, here's ur program. {week}. {prog['rationale'][0]}"
    day_lines = []
    for key in prog["cycle"]:
        ex = prog["exercises"].get(key) or []
        if ex:
            day_lines.append(f"{day_label(key)} = {', '.join(ex)}")
    extra = [ln for ln in prog["rationale"][1:] if ln.startswith(("all machines", "bodyweight", "15 min", "10 min"))]
    b2 = ". ".join(day_lines + extra)
    b3 = (f"how it moves: {prog['scheme']['sets']}x{prog['scheme']['reps']}. {prog['progression']['text']}. "
          f"{prog['rationale'][-1]}. "
          f"{'those days work, or wanna swap any?' if prog['days_source'] == 'default' else 'anything u want changed?'}")
    return [b1, b2, b3]


def program_context(user, prog: dict | None = None, *, now=None) -> str:
    """The loop/heartbeat block: the week, today's day (or rest), the next session."""
    prog = prog or getattr(user, "program", None)
    if not prog:
        return ""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(getattr(user, "user_timezone", None) or "America/Los_Angeles")
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = DAYS[local.weekday()]
    key = prog["days"].get(today)
    nxt = next((d for i in range(1, 8) for d in [DAYS[(local.weekday() + i) % 7]] if d in prog["days"]), None)
    lines = [f"## PROGRAM ({prog['split'].replace('_', '/')}, code-built — explain it, don't reinvent it)",
             f"- week: {describe_week(prog)}",
             f"- today ({today}): {day_label(key) + ' day' if key else 'rest day'}"
             + (f"; next: {nxt} {day_label(prog['days'][nxt])}" if nxt else "")]
    if prog.get("cardio"):
        c = prog["cardio"]
        lines.append(f"- cardio: {c['minutes']} min {c['what']} after {'/'.join(c['on'])}")
    if prog.get("constraints"):
        lines.append("- honouring: " + "; ".join(prog["constraints"]))
    lines.append(f"- progression: {prog['progression']['text']}")
    return "\n".join(lines)


# ─── writing it ──────────────────────────────────────────────────────────────

def apply_program(user_id: int, *, weekdays: list[str] | None = None, source: str = "builder",
                  stated_split: str | None = None) -> dict | None:
    """Build and store the program; mirror it into the columns the rest of the system
    reads. `stated_split`: a split THEY asked for (adjust_program) — written first and
    respected. Idempotent for the same inputs (version/built_at aside). Returns the program."""
    if not getattr(config, "PROGRAM_BUILDER_ENABLED", True):
        return None
    from models import get_session, User
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().one_or_none()
        if not user:
            return None
        prev = dict(user.program or {})
        if stated_split:
            user.current_split = stated_split
            user.split_days = None
            flag_modified(user, "split_days")
            session.flush()
        prog = build_program(user, weekdays=weekdays, respect_stated=True if stated_split else None)
        if prev.get("days_confirmed"):
            prog["days_confirmed"] = True
        user.program = prog
        flag_modified(user, "program")
        # mirrors — what cards / pointer / briefing / reminders already read
        if prog["split"] in ("upper_lower", "ppl", "full_body"):
            user.current_split = prog["split"]
            user.split_days = None
        else:
            user.current_split = "custom" if prog["split"] == "rotation" else (user.current_split or "custom")
            user.split_days = list(prog["cycle"])
            flag_modified(user, "split_days")
        user.confirmed_training_split = prog["split"]
        days = [d for d in DAYS if d in prog["days"]]
        user.confirmed_training_days = ",".join(days)
        if prog["days_source"] == "stated" or weekdays:
            user.workout_days = ",".join(days)
        times = {d: t for d, t in prog["times"].items() if t}
        if times and len(set(times.values())) > 1:
            user.workout_times = {"by_day": times}
            flag_modified(user, "workout_times")
        session.commit()
        logger.info("PROGRAM_APPLIED user=%s split=%s days=%s source=%s", user_id, prog["split"], days, source)
        return prog
    finally:
        session.close()


# ─── the brief, the days question, the standing reminder ─────────────────────

BRIEF_TYPE = "program_brief"
PENDING_HOURS = 48
_DAY_WORD_RE = None


def _day_words():
    global _DAY_WORD_RE
    import re
    if _DAY_WORD_RE is None:
        _DAY_WORD_RE = re.compile(r"\b(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)(?:[a-z]*)\b", re.I)
    return _DAY_WORD_RE


def _days_in(text: str) -> list[str]:
    seen = []
    for m in _day_words().finditer(text or ""):
        d = m.group(1).lower()[:3]
        if d in DAYS and d not in seen:
            seen.append(d)
    return seen


def send_program_brief(user_id: int) -> bool:
    """Build + store the program and text the brief (3 bubbles, type program_brief). The
    last bubble asks about the days; the answer is handled in code (handle_days_reply).
    Returns True when it went out."""
    if not getattr(config, "PROGRAM_BRIEF_ENABLED", True):
        return False
    prog = apply_program(user_id, source="brief")
    if not prog:
        return False
    from models import get_session, User
    from sms import send_sms
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u or not u.phone:
            return False
        phone = u.phone
        bubbles = program_brief(u, prog)
    finally:
        session.close()
    send_sms(phone, "\n---\n".join(bubbles), user_id=user_id, message_type=BRIEF_TYPE)
    logger.info("PROGRAM_BRIEF_SENT user=%s split=%s days=%s", user_id, prog["split"], list(prog["days"]))
    return True


def _brief_pending(session, user) -> bool:
    prog = getattr(user, "program", None) or {}
    if not prog or prog.get("days_confirmed"):
        return False
    from models import Message
    from datetime import timedelta
    row = (session.query(Message.created_at)
           .filter(Message.user_id == user.id, Message.direction == "out", Message.message_type == BRIEF_TYPE)
           .order_by(Message.id.desc()).first())
    if not row or not row[0]:
        return False
    return (datetime.now(timezone.utc).replace(tzinfo=None) - row[0]) < timedelta(hours=PENDING_HOURS)


def handle_days_reply(user_id: int, text: str) -> str | None:
    """Code-answer to the brief's "those days work, or wanna swap any?". A yes locks the
    days; a list of days (or "swap thu for fri" / "friday instead of thursday") rebuilds the
    week on THEIR days; a "no" asks which days. Anything else → None (a normal turn: the
    model has the PROGRAM block and adjust_program). Returns the one line to send."""
    if not getattr(config, "PROGRAM_BRIEF_ENABLED", True):
        return None
    import re
    from models import get_session, User
    from water_offer import YES_RE, NO_RE
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not _brief_pending(session, user):
            return None
        prog = dict(user.program or {})
        current = [d for d in DAYS if d in (prog.get("days") or {})]
    finally:
        session.close()
    body = (text or "").strip()
    low = body.lower()
    days = _days_in(low)
    swap = re.search(r"\b(?:swap|switch|move)\s+(\w+)\s+(?:for|to|with)\s+(\w+)|(\w+)\s+instead of\s+(\w+)|(\w+)\s+not\s+(\w+)", low)
    new_days = None
    if swap and len(days) == 2:
        a, b = (swap.group(1) or swap.group(4) or swap.group(6)), (swap.group(2) or swap.group(3) or swap.group(5))
        old_d, new_d = _days_in(a)[:1], _days_in(b)[:1]
        if swap.group(3):          # "<new> instead of <old>"
            old_d, new_d = _days_in(swap.group(4))[:1], _days_in(swap.group(3))[:1]
        if old_d and new_d and old_d[0] in current:
            new_days = [new_d[0] if d == old_d[0] else d for d in current]
    elif len(days) >= 2 and not NO_RE.match(body):
        new_days = days
    elif len(days) == 1 and len(current) == 1:
        new_days = days
    if new_days:
        prog = apply_program(user_id, weekdays=[d for d in DAYS if d in new_days], source="days_reply")
        _confirm(user_id, prog)
        logger.info("PROGRAM_DAYS_SWAPPED user=%s days=%s", user_id, list(prog["days"]))
        return f"locked: {describe_week(prog)}. " + _ping_line(prog)
    if YES_RE.match(body) and not days:
        from models import get_session as _gs
        s = _gs()
        try:
            prog = dict(s.get(User, user_id).program or {})
        finally:
            s.close()
        _confirm(user_id, prog)
        logger.info("PROGRAM_DAYS_CONFIRMED user=%s days=%s", user_id, current)
        return "locked in. " + _ping_line(prog)
    if NO_RE.match(body) and not days:
        return "which days then? just list em"
    return None


def _confirm(user_id: int, prog: dict) -> None:
    from models import get_session, User
    from sqlalchemy.orm.attributes import flag_modified
    session = get_session()
    try:
        u = session.query(User).filter(User.id == user_id).with_for_update().one()
        p = dict(u.program or prog or {})
        p["days_confirmed"] = True
        u.program = p
        flag_modified(u, "program")
        session.commit()
    finally:
        session.close()
    ensure_program_reminder(user_id)


def _ping_line(prog: dict) -> str:
    times = {t for t in (prog.get("times") or {}).values() if t}
    if len(times) == 1 and getattr(config, "PROGRAM_REMINDER_ENABLED", True):
        from workouts.start import _when
        return f"i'll ping u those days at {_when(next(iter(times)))}. say 'stop the gym reminders' if u don't want that"
    return "first card comes when u're heading in"


PROGRAM_REMINDER_TEXT = "gym"


def ensure_program_reminder(user_id: int) -> dict | None:
    """One standing reminder on the program's days at their time (source='program'),
    replaced on every change, cancelled when the program has no single time."""
    if not getattr(config, "PROGRAM_REMINDER_ENABLED", True):
        return None
    from models import get_session, User, Reminder
    from reminders import create_reminder
    session = get_session()
    try:
        u = session.get(User, user_id)
        prog = (u.program or {}) if u else {}
        existing = (session.query(Reminder).filter(Reminder.user_id == user_id, Reminder.source == "program",
                                                   Reminder.active.is_(True)).first())
        existing_id = existing.id if existing else None
    finally:
        session.close()
    days = [d for d in DAYS if d in (prog.get("days") or {})]
    times = {t for t in (prog.get("times") or {}).values() if t}
    if not days or len(times) != 1:
        if existing_id:
            from reminders import cancel_reminder
            cancel_reminder(user_id, existing_id)
        return None
    res = create_reminder(user_id, PROGRAM_REMINDER_TEXT, next(iter(times)), days=days, source="program",
                          replace_id=existing_id)
    if "error" not in res:
        logger.info("PROGRAM_REMINDER_SET user=%s days=%s at=%s id=%s", user_id, days, res["local_time"], res["id"])
    return res
