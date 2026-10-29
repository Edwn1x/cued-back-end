"""
Reminders — an explicit "remind me" / "ping me after class" is a dated promise the
coach made, not a hope that a heartbeat tick lands in the right half hour.

Live 2026-09-22 (user 42, first conversation): "Wanna remind me to after class to
run?" → the coach said "yeah i can do that, i'll ping u after" and nothing existed
to do it. The onboarding surface has no tools, the regex Event floor logged an
EVENT_NEAR_MISS, and the memory fact ("wants a reminder after class ends to go run")
carried no time. The only path to the promised ping was the heartbeat model noticing
two memory lines during a 45-minute-cadence tick inside a 90-minute window.

Design:
  - A Reminder row: text, local time, optional recurrence days (a standing ask like
    "after class Tue/Thu"), or a date (one-off). fire_at is code-computed in the
    user's timezone; a recurring reminder re-arms itself after each send.
  - Creation paths: the agent loop's set_reminder tool (coaching), and during
    onboarding a small extraction that runs only when the inbound reads like a
    reminder request (or corrects one already set — "nah class ends 7:30").
  - Firing is a 60s scheduler sweep (fire_due): independent of the heartbeat's
    cadence, its model, and its guardrails. The only gates are the user being
    active and not opted out. Quiet hours do NOT apply — the user named the time.
  - The message is composed by the coach model in the shared voice (one short line
    that does exactly what was asked); on any model failure a plain deterministic
    line goes out instead. A promise is kept even when the model isn't available.
  - The heartbeat sees what code will send (REMINDERS block) so it doesn't pre-empt
    or duplicate it; a sent reminder counts as a proactive message for anti-stack.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from models import Reminder, User, get_session

logger = logging.getLogger("cued.reminders")

DAY_ABBR = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_ALIASES = {
    "monday": "mon", "tuesday": "tue", "tues": "tue", "wednesday": "wed", "weds": "wed",
    "thursday": "thu", "thurs": "thu", "friday": "fri", "saturday": "sat", "sunday": "sun",
    "weekdays": "mon,tue,wed,thu,fri", "weekends": "sat,sun", "everyday": "mon,tue,wed,thu,fri,sat,sun",
    "every day": "mon,tue,wed,thu,fri,sat,sun", "daily": "mon,tue,wed,thu,fri,sat,sun",
}

# Inbound that reads like a reminder request (onboarding has no tools; this is the
# floor that triggers the extraction). Precision over recall: "remind" / "ping me" /
# "text me at|after|when" / "hit me up at|after".
REMIND_REQUEST_RE = re.compile(
    r"\b(remind(?:er)?s?\b|ping me\b|(?:text|message|hit) me (?:up )?(?:at|after|when|before)\b|nudge me\b)",
    re.IGNORECASE,
)


def _tz(tz_str: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(tz_str or "America/Los_Angeles")
    except Exception:
        return ZoneInfo("America/Los_Angeles")


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_days(days) -> str | None:
    """'tue,thu' / ['tue','thu'] / 'Tuesdays and Thursdays' / 'weekdays' → canonical
    'tue,thu' (ordered mon..sun), or None if nothing parses."""
    if not days:
        return None
    if isinstance(days, (list, tuple)):
        raw = ",".join(str(d) for d in days)
    else:
        raw = str(days)
    raw = raw.lower()
    for alias, canon in _DAY_ALIASES.items():
        raw = raw.replace(alias, canon)
    found = {tok for tok in re.findall(r"[a-z]+", raw) if tok in DAY_ABBR}
    # "tues" → "tue" via alias; bare "tue"/"thu" already canonical; also accept "tuesdays"
    for tok in re.findall(r"[a-z]+", raw):
        for d in DAY_ABBR:
            if tok.startswith(d):
                found.add(d)
    if not found:
        return None
    return ",".join(d for d in DAY_ABBR if d in found)


def parse_local_time(hhmm) -> tuple[int, int] | None:
    """'19:30' / '7:30pm' / '7pm' / '19' → (19, 30). None if unparseable."""
    if hhmm is None:
        return None
    s = str(hhmm).strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", s)
    if not m:
        return None
    h, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        return None
    return h, mm


def _weekday_from(raw: str) -> int | None:
    """'wednesday' / 'wed' / 'next wed' / 'on thursday' → weekday index, else None."""
    import re as _re
    m = _re.search(r"\b(mon|tue|tues|wed|weds|thu|thur|thurs|fri|sat|sun)[a-z]*\b", raw or "")
    if not m:
        return None
    return DAY_ABBR.index(m.group(1)[:3])


def next_fire_at(tz: ZoneInfo, local_time: str, recur_days: str | None, *, after: datetime | None = None,
                 date_str: str | None = None) -> datetime | None:
    """Naive-UTC instant of the next occurrence strictly after `after` (default now).
    Recurring: the next listed weekday at local_time (today if still ahead). One-off
    with a date: that day. One-off without: today if ahead, else tomorrow."""
    parsed = parse_local_time(local_time)
    if not parsed:
        return None
    h, mm = parsed
    after_utc = (after or _naive_utcnow()).replace(tzinfo=timezone.utc)
    now_local = after_utc.astimezone(tz)
    if recur_days:
        days = [d for d in recur_days.split(",") if d in DAY_ABBR]
        if not days:
            return None
        for offset in range(0, 8):
            d = (now_local + timedelta(days=offset)).date()
            if DAY_ABBR[d.weekday()] not in days:
                continue
            cand = datetime(d.year, d.month, d.day, h, mm, tzinfo=tz)
            if cand > now_local:
                return cand.astimezone(timezone.utc).replace(tzinfo=None)
        return None
    if date_str and str(date_str).strip().lower() not in ("", "today", "tomorrow"):
        raw = str(date_str).strip().lower()
        wd = _weekday_from(raw)
        if wd is not None:
            # "wednesday" / "next wed" → the next such day (today only if the time is still ahead)
            for offset in range(0, 8):
                cand_d = (now_local + timedelta(days=offset)).date()
                if cand_d.weekday() != wd:
                    continue
                cand = datetime(cand_d.year, cand_d.month, cand_d.day, h, mm, tzinfo=tz)
                if cand > now_local and not (raw.startswith("next") and offset == 0):
                    return cand.astimezone(timezone.utc).replace(tzinfo=None)
            return None
        try:
            d = datetime.fromisoformat(raw).date()
        except ValueError:
            # Live 2026-10-18 (user 40): "wednesday" fell through to TODAY and armed a 10pm
            # "gym" ping for tonight. An unreadable date is an error, never a guess.
            logger.warning("REMINDER_DATE_UNREADABLE date_str=%r", date_str)
            return None
    elif str(date_str or "").strip().lower() == "tomorrow":
        d = (now_local + timedelta(days=1)).date()
    else:
        d = now_local.date()
    cand = datetime(d.year, d.month, d.day, h, mm, tzinfo=tz)
    if cand <= now_local:
        cand += timedelta(days=1)
    return cand.astimezone(timezone.utc).replace(tzinfo=None)


# ─── interval recurrence (daily rhythm: water) ───────────────────────────────
# One row = "every N hours between window_start and window_end, every day". Slots
# anchor at the window start (07:00, 09:00, ...); past the window end the next slot
# is tomorrow's start. A null window follows the user's wake/sleep at fire time.

INTERVAL_MAX_HOURS = 12
_DEFAULT_WINDOW = ("08:00", "22:00")   # when neither the row nor the profile gives a parseable time


def _norm_hhmm(value) -> str | None:
    p = parse_local_time(value)
    return f"{p[0]:02d}:{p[1]:02d}" if p else None


def interval_window(user, row=None) -> tuple[str, str]:
    """('HH:MM', 'HH:MM') for an interval row: the row's own window when set, else the
    user's wake/sleep when they parse, else 08:00–22:00."""
    ws = (_norm_hhmm(getattr(row, "window_start", None)) or _norm_hhmm(getattr(user, "wake_time", None))
          or _DEFAULT_WINDOW[0])
    we = (_norm_hhmm(getattr(row, "window_end", None)) or _norm_hhmm(getattr(user, "sleep_time", None))
          or _DEFAULT_WINDOW[1])
    return ws, we


def next_interval_fire_at(tz: ZoneInfo, every_hours, window_start: str, window_end: str, *,
                          after: datetime | None = None) -> datetime | None:
    """Naive-UTC instant of the next interval slot strictly after `after` (default now).
    Slots: window_start + k*every_hours while <= window_end (a window whose end is
    earlier than its start crosses midnight). Past the last slot → the next day's start."""
    try:
        n = int(every_hours)
    except (TypeError, ValueError):
        return None
    if not (1 <= n <= INTERVAL_MAX_HOURS):
        return None
    ps, pe = parse_local_time(window_start), parse_local_time(window_end)
    if not ps or not pe:
        return None
    after_utc = (after or _naive_utcnow()).replace(tzinfo=timezone.utc)
    now_local = after_utc.astimezone(tz)
    # A crossing window (10:00–01:00) may still be open from YESTERDAY's start at 00:30.
    for offset in (-1, 0, 1, 2):
        d = (now_local + timedelta(days=offset)).date()
        start = datetime(d.year, d.month, d.day, ps[0], ps[1], tzinfo=tz)
        end = datetime(d.year, d.month, d.day, pe[0], pe[1], tzinfo=tz)
        if end <= start:
            end += timedelta(days=1)
        t = start
        while t <= end:
            if t > now_local:
                return t.astimezone(timezone.utc).replace(tzinfo=None)
            t += timedelta(hours=n)
    return None


def _next_for_row(user, row: Reminder, tz: ZoneInfo, *, after: datetime | None = None) -> datetime | None:
    """Re-arm helper: the right next instant for any recurring row (interval or weekly)."""
    if row.every_hours:
        ws, we = interval_window(user, row)
        return next_interval_fire_at(tz, row.every_hours, ws, we, after=after)
    if row.recur_days:
        return next_fire_at(tz, row.local_time, row.recur_days, after=after)
    return None


def create_reminder(user_id: int, text: str, local_time: str | None = None, *, days=None,
                    date_str: str | None = None, source: str = "model", replace_id: int | None = None,
                    every_hours=None, window_start: str | None = None, window_end: str | None = None) -> dict:
    """Create (or, with replace_id, rewrite) a reminder. Returns {"id", "fire_at", "local_time",
    "recur_days", "every_hours"} or {"error": ...}. Never raises on bad input. With
    every_hours the row is an interval reminder: local_time is optional (it records the
    window start) and days/date are ignored."""
    text = (text or "").strip()
    if not text:
        return {"error": "text required"}
    interval = None
    if every_hours is not None and every_hours != "":
        try:
            interval = int(every_hours)
        except (TypeError, ValueError):
            return {"error": f"every_hours {every_hours!r} must be a whole number of hours (1–{INTERVAL_MAX_HOURS})"}
        if not (1 <= interval <= INTERVAL_MAX_HOURS):
            return {"error": f"every_hours must be 1–{INTERVAL_MAX_HOURS}, got {interval}"}
        for label, v in (("window_start", window_start), ("window_end", window_end)):
            if v not in (None, "") and parse_local_time(v) is None:
                return {"error": f"{label} {v!r} not understood — use local 'HH:MM' (24h)"}
    elif parse_local_time(local_time) is None:
        return {"error": f"time {local_time!r} not understood — use local 'HH:MM' (24h)"}
    recur = None if interval else parse_days(days)
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return {"error": "user not found"}
        tz = _tz(user.user_timezone)
        row = session.get(Reminder, replace_id) if replace_id else None
        if row is None or row.user_id != user_id:
            row = Reminder(user_id=user_id, source=source)
            session.add(row)
        if interval:
            row.every_hours = interval
            row.window_start = _norm_hhmm(window_start) if window_start else None
            row.window_end = _norm_hhmm(window_end) if window_end else None
            ws, we = interval_window(user, row)
            local_norm = ws
            fire = next_interval_fire_at(tz, interval, ws, we)
        else:
            row.every_hours = row.window_start = row.window_end = None
            h, mm = parse_local_time(local_time)
            local_norm = f"{h:02d}:{mm:02d}"
            fire = next_fire_at(tz, local_norm, recur, date_str=date_str)
        if fire is None:
            return {"error": "could not compute a next time"}
        row.text, row.local_time, row.recur_days, row.fire_at, row.active = text[:300], local_norm, recur, fire, True
        row.cancelled_at = None
        session.commit()
        logger.info("REMINDER_SET user=%s id=%s text=%r at=%s days=%s every_hours=%s fire_at=%s source=%s",
                    user_id, row.id, text[:60], local_norm, recur, interval, fire, source)
        return {"id": row.id, "fire_at": fire, "local_time": local_norm, "recur_days": recur,
                "every_hours": interval}
    finally:
        session.close()


def cancel_reminder(user_id: int, reminder_id: int) -> bool:
    session = get_session()
    try:
        row = session.get(Reminder, reminder_id)
        if not row or row.user_id != user_id or not row.active:
            return False
        row.active = False
        row.cancelled_at = _naive_utcnow()
        session.commit()
        logger.info("REMINDER_CANCELLED user=%s id=%s", user_id, reminder_id)
        return True
    finally:
        session.close()


def update_reminder_text(user_id: int, reminder_id: int, text: str) -> dict:
    """Rewrite what an active reminder SAYS; its time, days, next fire and sent count stay. Returns
    {"id", "text"} or {"error": ...}. Never raises on bad input. Live 2026-10-28 (user 77): a swapped
    stretch left both daily posture reminders prescribing the move he'd just been told to drop."""
    text = (text or "").strip()
    if not text:
        return {"error": "text required"}
    session = get_session()
    try:
        row = session.get(Reminder, reminder_id)
        if not row or row.user_id != user_id or not row.active:
            return {"error": "no active reminder with that id"}
        old = row.text
        row.text = text[:300]
        session.commit()
        logger.info("REMINDER_UPDATED user=%s id=%s old=%r new=%r", user_id, reminder_id, (old or "")[:60], text[:60])
        return {"id": row.id, "text": row.text}
    finally:
        session.close()


def active_reminders(user_id: int, session=None) -> list:
    own = session is None
    session = session or get_session()
    try:
        return (session.query(Reminder)
                .filter(Reminder.user_id == user_id, Reminder.active.is_(True))
                .order_by(Reminder.fire_at).all())
    finally:
        if own:
            session.close()


def _fmt_local(dt_naive_utc: datetime, tz: ZoneInfo) -> str:
    loc = dt_naive_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    return loc.strftime("%a %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def _fmt_time(local_time: str) -> str:
    h, mm = parse_local_time(local_time)
    d = datetime(2000, 1, 1, h, mm)
    return d.strftime("%-I:%M%p").replace("AM", "am").replace("PM", "pm")


def describe(row: Reminder, tz: ZoneInfo, user=None) -> str:
    if row.every_hours:
        if user is None:   # a null window follows the profile — resolve it the same way firing does
            s = get_session()
            try:
                user = s.get(User, row.user_id)
            finally:
                s.close()
        ws, we = interval_window(user, row)
        when = f"every {row.every_hours}h {_fmt_time(ws)}–{_fmt_time(we)} daily"
    elif row.recur_days:
        when = f"{row.recur_days.replace(',', '/')} {_fmt_time(row.local_time)}"
    else:
        when = _fmt_local(row.fire_at, tz)
    return f"'{row.text}' — {when} (next: {_fmt_local(row.fire_at, tz)}, id={row.id})"


def context_block(user, session) -> str | None:
    """What the heartbeat / loop sees: reminders code WILL send (don't pre-empt them)
    and any sent today (don't repeat them)."""
    if not config.REMINDERS_ENABLED:
        return None
    tz = _tz(user.user_timezone)
    rows = active_reminders(user.id, session)
    day_ago = _naive_utcnow() - timedelta(hours=24)
    sent = (session.query(Reminder)
            .filter(Reminder.user_id == user.id, Reminder.last_sent_at.isnot(None),
                    Reminder.last_sent_at >= day_ago).all())
    if not rows and not sent:
        return None
    lines = ["## REMINDERS (code sends these at the named time — do NOT pre-empt or duplicate them)"]
    for r in rows:
        lines.append(f"- pending: {describe(r, tz, user)}")
    for r in sent:
        lines.append(f"- sent {_fmt_local(r.last_sent_at, tz)}: '{r.text}'")
    return "\n".join(lines)


# ─── composing + firing ──────────────────────────────────────────────────────

def _compose(user, row: Reminder) -> str:
    """One short line in the coach's voice that does exactly what they asked. Any
    failure → the plain line; the promise is kept either way."""
    fallback = f"reminder: {row.text}"
    try:
        from agent_loop import _join_text, _voice_prompt, stable_cache_control
        from cost_tracking import track
        from llm_client import make_client
        tz = _tz(user.user_timezone)
        now_local = datetime.now(tz).strftime("%A %-I:%M%p").replace("AM", "am").replace("PM", "pm")
        # The SAME bytes as every other site (_voice_prompt already starts with identity)
        # so a water ping reads the warm voice-prompt entry instead of writing its own.
        system = [
            {"type": "text", "text": _voice_prompt(), "cache_control": stable_cache_control()},
            {"type": "text", "text": f"Their name: {user.name}. It's {now_local} their time."},
        ]
        instruction = (
            f"Earlier they asked you to remind them: \"{row.text}\". It's time. Send that reminder "
            f"now — ONE short line in your voice that does exactly that. No preamble, no "
            f"explanation of why you're texting, no greeting, no question unless it's the natural "
            f"way to say it. Under 20 words."
        )
        if row.every_hours:
            instruction += (
                f" This one is a standing ping they get every {row.every_hours} hours, not a one-off: "
                f"a few words, no question, no explanation, and not the same wording every time."
            )
        client = make_client()
        from cache_keepalive import note as keepalive_note
        keepalive_note("reminder.compose", model=config.AGENT_LOOP_MODEL, system=system,
                       thinking={"type": "adaptive"}, output_config={"effort": "low"})
        # Opus 5.5: thinking always on and counted in max_tokens — effort low keeps it
        # short; the cap has room for reasoning + a <20-word line.
        resp = client.messages.create(model=config.AGENT_LOOP_MODEL, max_tokens=800,
                                      thinking={"type": "adaptive"}, output_config={"effort": "low"},
                                      system=system, messages=[{"role": "user", "content": instruction}])
        try:
            track(user.id, "reminder.compose", config.AGENT_LOOP_MODEL, resp)
        except Exception as e:  # noqa: BLE001
            logger.warning("REMINDER_COST_TRACK_FAILED user=%s err=%s", user.id, e)
        if getattr(resp, "stop_reason", None) == "max_tokens":
            logger.warning("REMINDER_COMPOSE_TRUNCATED user=%s id=%s", user.id, row.id)
            return fallback
        text = (_join_text(resp.content) or "").strip()
        return text or fallback
    except Exception as e:  # noqa: BLE001 — never lose the promise to a model error
        logger.warning("REMINDER_COMPOSE_FAILED user=%s id=%s err=%s", user.id, row.id, e)
        return fallback


def fire_due(now: datetime | None = None) -> int:
    """Scheduler sweep: send every active reminder whose fire_at has passed. Recurring
    ones re-arm; one-offs deactivate. Returns the number sent."""
    if not config.REMINDERS_ENABLED:
        return 0
    now = now or _naive_utcnow()
    session = get_session()
    try:
        due = (session.query(Reminder)
               .filter(Reminder.active.is_(True), Reminder.fire_at <= now)
               .order_by(Reminder.fire_at).all())
        ids = [r.id for r in due]
    finally:
        session.close()
    sent = 0
    for rid in ids:
        try:
            if _fire_one(rid, now):
                sent += 1
        except Exception as e:  # noqa: BLE001 — one bad row must not block the rest
            logger.error("REMINDER_FIRE_FAILED id=%s err=%s", rid, e, exc_info=True)
    try:
        rearm_bailed_gym_reminders(now)
    except Exception as e:  # noqa: BLE001
        logger.error("REMINDER_GYM_REARM_FAILED err=%s", e, exc_info=True)
    return sent


_GYM_REMINDER_RE = re.compile(r"\b(gym|work ?out|lift(?:ing)?|train(?:ing)?|session|rsf|run)\b", re.I)


def rearm_bailed_gym_reminders(now: datetime | None = None) -> int:
    """A one-off "go to the gym" reminder that fired and was NOT followed by a workout
    session within REMINDER_GYM_REARM_AFTER_H hours re-arms once for the same local time
    tomorrow (source='rearm'), so the coach's "go tomorrow" is a row in REMINDERS, not a
    sentence. Live 2026-10-17 22:42 (user 40): "go tomorrow, it's on the books" — the
    reminder was one-off and inactive, nothing was on the books. Never re-arms a re-arm
    (one extra chance, not a nag), never when a session did start, never when an
    equivalent active reminder already exists. Returns the number created."""
    if not getattr(config, "REMINDER_GYM_REARM_ENABLED", True):
        return 0
    now = now or _naive_utcnow()
    after_h = float(getattr(config, "REMINDER_GYM_REARM_AFTER_H", 3))
    lo, hi = now - timedelta(hours=24), now - timedelta(hours=after_h)
    created = 0
    session = get_session()
    try:
        from models import WorkoutSession
        rows = (session.query(Reminder)
                .filter(Reminder.active.is_(False), Reminder.recur_days.is_(None), Reminder.every_hours.is_(None),
                        Reminder.cancelled_at.is_(None), Reminder.last_sent_at.isnot(None),
                        Reminder.last_sent_at >= lo, Reminder.last_sent_at <= hi,
                        Reminder.source != "rearm", Reminder.local_time.isnot(None)).all())
        todo = []
        for r in rows:
            if not _GYM_REMINDER_RE.search(r.text or ""):
                continue
            started = (session.query(WorkoutSession.id)
                       .filter(WorkoutSession.user_id == r.user_id, WorkoutSession.started_at.isnot(None),
                               WorkoutSession.started_at >= r.last_sent_at).first())
            if started:
                continue
            dup = (session.query(Reminder.id)
                   .filter(Reminder.user_id == r.user_id, Reminder.active.is_(True), Reminder.text == r.text).first())
            if dup:
                continue
            todo.append((r.id, r.user_id, r.text, r.local_time))
    finally:
        session.close()
    for rid, uid, text, local_time in todo:
        res = create_reminder(uid, text, local_time, date_str="tomorrow", source="rearm")
        if "error" in res:
            logger.info("REMINDER_GYM_REARM_SKIPPED user=%s from=%s err=%s", uid, rid, res["error"])
            continue
        created += 1
        logger.info("REMINDER_GYM_REARMED user=%s from=%s new=%s fire_at=%s text=%r", uid, rid, res["id"], res["fire_at"], text[:60])
    return created


_EXAM_TITLE_RE = re.compile(r"\b(?:exam|midterm|finals?|quiz|test)\b", re.I)


def _interval_gate(user, now: datetime, row: Reminder | None = None) -> dict:
    """Why an interval (water) ping should wait, and until when:
      {"reason": None}                              → fire
      {"reason": "quiet_hours", "until": None}      → overnight, re-arm to the grid
      {"reason": "exam"|"calendar_block", "until": block_end}
    `now` is naive UTC. Inert ({"reason": None}) unless WATER_REMINDER_GATES_ENABLED.

    v1 reused the heartbeat's gate: a block ONGOING OR STARTING WITHIN 90 MIN deferred the
    ping to the next grid slot, whatever the block was. Live 2026-10-27 (user 77): asked for
    water every 2h at 11:47; Phys 111A lab 12–7pm then a 7–8pm discussion → every slot from
    1pm slid, first ping at 9pm. v2 (INTERVAL_GATE_V2_ENABLED):
      - ongoing only — a one-line ping needs no runway;
      - a block of INTERVAL_GATE_LONG_BLOCK_MIN or more (a lab, a shift, a study block) does
        NOT gate — a water nudge during it is the point; shorter blocks (a lecture, a
        meeting) still do;
      - an exam/midterm/final/quiz title always gates, whatever the length;
      - a ping that has been deferred INTERVAL_GATE_MAX_DEFER_H hours (row.deferred_since)
        fires through a short block anyway — never through quiet hours or an exam.
    The caller re-arms a calendar gate to block_end + INTERVAL_GATE_CATCHUP_MIN (not the next
    grid slot) — see _fire_one."""
    none = {"reason": None, "until": None}
    if not config.WATER_REMINDER_GATES_ENABLED or user is None:
        return none
    try:
        from heartbeat import _in_standing_quiet_hours
        if _in_standing_quiet_hours(user, now=now):
            return {"reason": "quiet_hours", "until": None}
    except Exception as e:  # noqa: BLE001 — a gate import/eval failure must never drop the ping
        logger.warning("INTERVAL_GATE_QUIET_FAILED user=%s err=%s", getattr(user, "id", None), e)
    if not getattr(config, "INTERVAL_GATE_V2_ENABLED", True):
        try:
            from events import calendar_block_soon
            if calendar_block_soon(user.id, now=now):
                return {"reason": "calendar_block", "until": None}
        except Exception as e:  # noqa: BLE001
            logger.warning("INTERVAL_GATE_CALENDAR_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return none
    try:
        from events import current_blocks
        blocks = current_blocks(user.id, now=now)
    except Exception as e:  # noqa: BLE001
        logger.warning("INTERVAL_GATE_CALENDAR_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return none
    if not blocks:
        return none
    exam = next((b for b in blocks if _EXAM_TITLE_RE.search(b["title"] or "")), None)
    if exam:
        return {"reason": "exam", "until": exam["end"]}
    long_min = int(getattr(config, "INTERVAL_GATE_LONG_BLOCK_MIN", 120))
    short = [b for b in blocks if b["minutes"] < long_min]
    if not short:
        return none                                   # only long blocks → a nudge during it is the point
    since = getattr(row, "deferred_since", None) if row is not None else None
    max_h = float(getattr(config, "INTERVAL_GATE_MAX_DEFER_H", 4))
    if since and (now - since) >= timedelta(hours=max_h):
        logger.info("INTERVAL_GATE_MAX_DEFER_OVERRIDE user=%s id=%s deferred_since=%s block=%r",
                    getattr(user, "id", None), getattr(row, "id", None), since, short[0]["title"][:40])
        return none
    until = max(b["end"] for b in short)
    return {"reason": "calendar_block", "until": until}


def _interval_gate_reason(user, now: datetime, row: Reminder | None = None) -> str | None:
    """Back-compat: the reason only (defer) or None (fire)."""
    return _interval_gate(user, now, row)["reason"]


def _catchup_fire_at(user, row: Reminder, tz: ZoneInfo, now: datetime, until: datetime | None) -> datetime | None:
    """Where a calendar-gated ping lands: just after the block ends, if that comes before the
    next grid slot and isn't quiet hours; else the next grid slot."""
    grid = _next_for_row(user, row, tz, after=now)
    if not until:
        return grid
    cand = until + timedelta(minutes=int(getattr(config, "INTERVAL_GATE_CATCHUP_MIN", 5)))
    if cand <= now or (grid is not None and cand >= grid):
        return grid
    try:
        from heartbeat import _in_standing_quiet_hours
        if _in_standing_quiet_hours(user, now=cand):
            return grid
    except Exception:  # noqa: BLE001
        return grid
    return cand


def _send_demo_links_once(user, row: Reminder) -> bool:
    """The first time a reminder that names library movements fires, a second bubble carries
    the verified how-to clips (exercise_demos: PT channels, about a minute each); the demos
    are then marked seen so no later fire repeats them. Live 2026-10-27 (user 77): the 2pm
    posture break went out as a line, he asked for reference videos, the coach searched and
    said "couldn't pull links". Fail-open, flag-gated; never affects the reminder itself."""
    if not getattr(config, "REMINDER_DEMO_LINKS_ENABLED", True) or not config.EXERCISE_DEMOS_ENABLED:
        return False
    try:
        from exercise_demos import demo_links_for_reminder, mark_demos_seen, _resolve
        from sms import send_sms
        demos = demo_links_for_reminder(user, row.text)
        if not demos:
            return False
        lines = "\n".join(f"{name}\n{url}" for name, url in demos.items())
        send_sms(user.phone, f"how-to clips, about a minute each:\n{lines}", user_id=user.id, message_type="reminder")
        keys = [k for k in (_resolve(n) for n in demos) if k]
        mark_demos_seen(user.id, keys)
        logger.info("REMINDER_DEMO_LINKS_SENT user=%s id=%s keys=%s", user.id, row.id, keys)
        return True
    except Exception as e:  # noqa: BLE001 — the clips are extra; the reminder already went
        logger.warning("REMINDER_DEMO_LINKS_FAILED user=%s id=%s err=%s", getattr(user, "id", None), row.id, e)
        return False


def _first_slot_waits_for_briefing(user, row: Reminder, session, now: datetime) -> bool:
    """The day's FIRST water slot yields to a morning briefing that is still owed — the
    briefing carries "big glass of water first" and IS that ping (founder 2026-10-29). A
    7:00 waker's GIF lands at 7:15 and the briefing tick at ~7:30: without this the GIF
    goes first and the fold never has anything to fold. Only the first slot of the local
    day, only while _morning_open_signal still renders (nobody has talked since they woke
    and the window is open), only when the daily briefing is on. Fail-open → False."""
    if not (getattr(config, "BRIEFING_WATER_ENABLED", True)
            and getattr(config, "BRIEFING_WATER_FOLDS_FIRST_PING_ENABLED", True)
            and config.CALENDAR_ASSISTANT_ENABLED and config.CALENDAR_DAILY_BRIEFING_ENABLED):
        return False
    try:
        from water_gif import is_water_row
        if not is_water_row(row):
            return False
        from timefmt import local_day_bounds
        d0, _d1 = local_day_bounds(user, now=now.replace(tzinfo=timezone.utc))
        if row.last_sent_at is not None and row.last_sent_at >= d0:
            return False                                   # not the first slot of the day
        from heartbeat import _morning_open_signal
        return _morning_open_signal(user, session, now=now) is not None
    except Exception as e:  # noqa: BLE001
        logger.warning("WATER_BRIEFING_WAIT_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return False


def fold_first_water_slot(user_id: int, *, now: datetime | None = None) -> int:
    """The morning briefing just told them "big glass of water first" — that IS the day's
    first water ping (founder 2026-10-29). Every active water row (water_gif.is_water_row)
    whose pending slot is already due or lands within one interval of `now` moves to the
    grid slot after it; a slot further out (a catch-up already past the next slot) stays.
    Returns the number of rows moved. `now` naive UTC."""
    from water_gif import is_water_row
    now = now or _naive_utcnow()
    session = get_session()
    moved = 0
    try:
        rows = (session.query(Reminder)
                .filter(Reminder.user_id == user_id, Reminder.active.is_(True), Reminder.every_hours.isnot(None))
                .all())
        for row in rows:
            if not is_water_row(row) or row.fire_at is None:
                continue
            if row.fire_at > now + timedelta(hours=int(row.every_hours)):
                continue
            user = session.get(User, user_id)
            tz = _tz(user.user_timezone if user else None)
            nxt = _next_for_row(user, row, tz, after=max(now, row.fire_at))
            if not nxt:
                continue
            logger.info("WATER_FIRST_SLOT_FOLDED user=%s id=%s was=%s now=%s", user_id, row.id, row.fire_at, nxt)
            row.fire_at = nxt
            row.deferred_since = None
            moved += 1
        if moved:
            session.commit()
        return moved
    finally:
        session.close()


def _fire_one(rid: int, now: datetime) -> bool:
    from sms import send_sms
    session = get_session()
    try:
        row = session.get(Reminder, rid)
        if not row or not row.active or row.fire_at > now:
            return False
        user = session.get(User, row.user_id)
        tz = _tz(user.user_timezone if user else None)
        # Stale by more than a day (container down, row seeded in the past): re-arm
        # rather than send a reminder about a moment that's long gone.
        if now - row.fire_at > timedelta(hours=24):
            logger.warning("REMINDER_STALE_SKIPPED id=%s user=%s fire_at=%s", rid, row.user_id, row.fire_at)
            nxt = _next_for_row(user, row, tz, after=now)
            row.fire_at, row.active = (nxt, True) if nxt else (row.fire_at, False)
            session.commit()
            return False
        if not user or not user.active or getattr(user, "opted_out", False):
            logger.info("REMINDER_SKIPPED id=%s user=%s reason=%s", rid, row.user_id,
                        "opted_out" if user and getattr(user, "opted_out", False) else "inactive")
            row.active = False
            session.commit()
            return False
        # Interval (water) rhythm pings defer through quiet hours / calendar blocks —
        # the same gates the heartbeat honours. Re-arm to the next slot, don't send.
        if row.every_hours and _first_slot_waits_for_briefing(user, row, session, now):
            nxt = _next_for_row(user, row, tz, after=now)
            row.fire_at, row.active = (nxt, True) if nxt else (row.fire_at, False)
            session.commit()
            logger.info("REMINDER_GATED user=%s id=%s reason=briefing_owed next=%s", row.user_id, rid,
                        row.fire_at if row.active else None)
            return False
        if row.every_hours:
            gate = _interval_gate(user, now, row)
            if gate["reason"]:
                if gate["reason"] in ("calendar_block", "exam") and getattr(config, "INTERVAL_GATE_V2_ENABLED", True):
                    nxt = _catchup_fire_at(user, row, tz, now, gate.get("until"))
                else:
                    nxt = _next_for_row(user, row, tz, after=now)
                if getattr(row, "deferred_since", None) is None:
                    row.deferred_since = now
                row.fire_at, row.active = (nxt, True) if nxt else (row.fire_at, False)
                session.commit()
                logger.info("REMINDER_GATED user=%s id=%s reason=%s until=%s next=%s deferred_since=%s", row.user_id, rid,
                            gate["reason"], gate.get("until"), row.fire_at if row.active else None, row.deferred_since)
                return False
        # Water pings go out as the character GIF when the flag is on (water_gif.py,
        # founder 2026-10-26) — no model call, no line. The plain line below is the
        # fallback for every other case, including a GIF that didn't land.
        sent_as_gif = False
        if row.every_hours and config.WATER_GIF_ENABLED:
            from water_gif import MARKER, is_water_row, send_water_gif
            if is_water_row(row):
                sent_as_gif = send_water_gif(user, row)
        if sent_as_gif:
            text = MARKER
        else:
            text = _compose(user, row)
            # Interval pings carry their own type so the heartbeat's anti-stack gate and the
            # rhythm gates can tell "a water ping landed" from "the coach spoke" (engagement_
            # tracker.WATER_REMINDER_TYPE). One-offs stay "reminder".
            from engagement_tracker import WATER_REMINDER_TYPE
            send_sms(user.phone, text, user_id=user.id,
                     message_type=WATER_REMINDER_TYPE if row.every_hours else "reminder")
            if not row.every_hours:
                _send_demo_links_once(user, row)
        row.last_sent_at = now
        row.sent_count = (row.sent_count or 0) + 1
        row.deferred_since = None
        if row.every_hours or row.recur_days:
            row.fire_at = _next_for_row(user, row, tz, after=now) or row.fire_at
        else:
            row.active = False
        session.commit()
        logger.info("REMINDER_SENT user=%s id=%s text=%r next=%s", user.id, rid, text[:80],
                    row.fire_at if row.active else None)
        return True
    finally:
        session.close()


# ─── water ack ────────────────────────────────────────────────────────────────
# "drank" / "done" / 👍 right after an interval (water) ping is closure, not coaching
# input. app.py routes it through the existing closing-ack branch (no model call, a 👍
# tapback on iMessage) when this says so — never a second handler.

_WATER_ACK_RE = re.compile(
    r"^(?:(?:ok|okay|k|yes|yep|yup|ya|yea|yeah|bet|done|did|drank|drinking|drank it|drank some|"
    r"on it|just did|chugged|chugging|got it|gotchu|👍|💧|💪|🙏|✅)[\s.!,]*){1,4}$",
    re.IGNORECASE,
)
WATER_ACK_WINDOW_MINUTES = 90


def is_water_ack(user_id: int, body: str) -> bool:
    """True when the inbound is a short ack and the most recent outbound was a reminder
    sent within WATER_ACK_WINDOW_MINUTES for a user who has an active interval reminder."""
    b = (body or "").strip().lower()
    if not b or len(b) > 40 or not _WATER_ACK_RE.match(b):
        return False
    from models import Message
    session = get_session()
    try:
        has_interval = (session.query(Reminder.id)
                        .filter(Reminder.user_id == user_id, Reminder.active.is_(True),
                                Reminder.every_hours.isnot(None)).first()) is not None
        if not has_interval:
            return False
        last_out = (session.query(Message)
                    .filter(Message.user_id == user_id, Message.direction == "out")
                    .order_by(Message.created_at.desc()).first())
        from engagement_tracker import REMINDER_TYPES
        if not last_out or not last_out.created_at or last_out.message_type not in REMINDER_TYPES:
            return False
        age = _naive_utcnow() - last_out.created_at.replace(tzinfo=None)
        return age <= timedelta(minutes=WATER_ACK_WINDOW_MINUTES)
    finally:
        session.close()


# ─── onboarding capture (no tools on that surface) ───────────────────────────

_EXTRACT_PROMPT = """The user is in their first conversation with a fitness coach (text messages). Decide whether they have asked the coach to REMIND / PING / TEXT them at a specific time, and if so extract it.

Conversation so far (oldest first; "them:" is the user, "you:" is the coach):
{history}

Their latest message: "{incoming}"

Rules:
- Only a request TO BE REMINDED counts ("remind me to run after class", "ping me at 7", "text me when class is over"). A schedule fact alone ("class till 6") is NOT a reminder request unless a reminder was already asked for earlier in the conversation — then a corrected time updates it.
- The time is the user's LOCAL time. "after class" = the class END time stated anywhere in the conversation (use the LATEST correction). If no time can be resolved, return null.
- days: for a recurring ask, the weekdays it applies to ("Tuesdays and Thursdays" → ["tue","thu"]); a one-off → null. "every day"/"daily" → all seven.
- text: what to remind them of, in a few words, from their point of view ("go run", "take creatine").

Return ONLY JSON: {{"text": "...", "time": "HH:MM", "days": ["tue","thu"] or null, "date": "today"|"tomorrow"|"YYYY-MM-DD"|null}} or null."""


def maybe_capture_onboarding_reminder(user_id: int, incoming: str, history: str) -> dict | None:
    """Run only when the inbound reads like a reminder request, or when an onboarding
    reminder already exists and the inbound carries a number (a time correction —
    live: "class till 6" → "Nah class starts at 6 pm ends at 7:30 pm"). Small Sonnet
    JSON call; upserts the ONE onboarding-sourced reminder."""
    if not config.REMINDERS_ENABLED:
        return None
    existing = None
    session = get_session()
    try:
        existing = (session.query(Reminder)
                    .filter(Reminder.user_id == user_id, Reminder.active.is_(True),
                            Reminder.source == "onboarding").order_by(Reminder.id.desc()).first())
        existing_id = existing.id if existing else None
        user = session.get(User, user_id)
        name = user.name if user else "them"
    finally:
        session.close()
    asked = bool(REMIND_REQUEST_RE.search(incoming or ""))
    correcting = existing_id is not None and bool(re.search(r"\d", incoming or ""))
    if not (asked or correcting):
        return None
    try:
        from agent_loop import _join_text
        from cost_tracking import track
        from llm_client import make_client
        client = make_client()
        prompt = _EXTRACT_PROMPT.format(history=(history or "(nothing yet)")[-4000:], incoming=(incoming or "")[:600])
        resp = client.messages.create(model=config.ONBOARDING_EXTRACTOR_MODEL, max_tokens=400,
                                      messages=[{"role": "user", "content": prompt}])
        try:
            track(user_id, "reminder.onboarding_extract", config.ONBOARDING_EXTRACTOR_MODEL, resp)
        except Exception as e:  # noqa: BLE001
            logger.warning("REMINDER_COST_TRACK_FAILED user=%s err=%s", user_id, e)
        raw = (_join_text(resp.content) or "").replace("```json", "").replace("```", "").strip()
        data = json.loads(raw) if raw else None
    except Exception as e:  # noqa: BLE001
        logger.warning("REMINDER_ONBOARDING_EXTRACT_FAILED user=%s err=%s", user_id, e)
        return None
    if not isinstance(data, dict) or not data.get("text") or not data.get("time"):
        logger.info("REMINDER_ONBOARDING_NONE user=%s asked=%s correcting=%s", user_id, asked, correcting)
        return None
    r = create_reminder(user_id, data["text"], data["time"], days=data.get("days"),
                        date_str=data.get("date"), source="onboarding", replace_id=existing_id)
    if "error" in r:
        logger.info("REMINDER_ONBOARDING_REJECTED user=%s err=%s", user_id, r["error"])
        return None
    logger.info("REMINDER_ONBOARDING user=%s id=%s %s text=%r at=%s days=%s", user_id, r["id"],
                "updated" if existing_id else "created", data["text"], r["local_time"], r["recur_days"])
    return r
