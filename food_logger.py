"""
Logger bridge — a user who still logs food in another app (MyNetDiary, MyFitnessPal,
Cronometer, Lose It) while trying cued. Spec: rewrite/logger-bridge/CHANGESPEC.md.

Why this exists (user 32, 2026-09-19): she sent a MyNetDiary screenshot of a breakfast
the coach had already photo-estimated; the model judged "turkey ≠ chicken" and logged it
as a SECOND meal, then filled protein with its own guess beside the app's printed
calories. She caught the double-count four turns later. None of the three real APIs
exist (MFP partner program closed; MyNetDiary/Cronometer have none), so the bridge is
state + rules, not OAuth:

  - users.food_logger / food_logger_status / food_logger_since: which app, whether
    they're still coexisting or have switched, and since when.
  - A context block that tells the coach an EMPTY day is not an unlogged day.
  - Diary screenshots (agent_tools, PR #91): printed numbers are the log; a screenshot
    for a meal slot that already holds a row is refused in code and edited instead.
  - Parity: the edit computes the delta in code (agent_tools); three close matches in a
    week earn ONE "you can drop the app" line (this module).
  - Graduation: 14 days with no app-sourced meals and 10+ cued days → switched.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger("cued.food_logger")

# Canonical app ids. Onboarding text and the coach both normalise through this.
APPS = ("myfitnesspal", "mynetdiary", "cronometer", "loseit", "macrofactor", "other")
_ALIASES = {
    "myfitnesspal": ("myfitnesspal", "my fitness pal", "mfp", "fitnesspal", "myfitness pal"),
    "mynetdiary": ("mynetdiary", "my net diary", "net diary", "net dairy", "netdiary", "mynet diary", "mnd"),
    "cronometer": ("cronometer", "chronometer", "crono"),
    "loseit": ("loseit", "lose it", "lose-it"),
    "macrofactor": ("macrofactor", "macro factor"),
}
STATUS_COEXIST = "coexist"
STATUS_SWITCHED = "switched"

APP_SOURCE = "app"

PARITY_CLOSE_PCT = 15
PARITY_MISS_PCT = 20
PARITY_CLOSE_NEEDED = 3
GRADUATE_DAYS = 14
GRADUATE_MIN_CUED_DAYS = 10


def normalize_app(text) -> str | None:
    """'net dairy' → 'mynetdiary'; unknown non-empty → 'other'; empty/none → None."""
    if not text:
        return None
    t = str(text).strip().lower()
    if not t or t in ("none", "no", "nah", "null"):
        return None
    for app, names in _ALIASES.items():
        for n in names:
            if n in t:
                return app
    return "other"


def app_label(app: str | None) -> str:
    return {"myfitnesspal": "MyFitnessPal", "mynetdiary": "MyNetDiary", "cronometer": "Cronometer",
            "loseit": "Lose It", "macrofactor": "MacroFactor"}.get(app or "", "their app")


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── state ─────────────────────────────────────────────────────────────────

def set_food_logger(user_id: int, app, status: str, *, source: str = "tool", session=None) -> dict:
    """Write the coexist/switched state. `switched` also writes ONE dated memory fact
    (the one thing about it worth remembering); `coexist` stamps `since` when it's a
    new coexist spell. Returns {"app", "status", "changed"}."""
    from models import get_session, User
    own = session is None
    session = session or get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return {"error": "user not found"}
        app_id = normalize_app(app) or user.food_logger
        if status not in (STATUS_COEXIST, STATUS_SWITCHED):
            return {"error": f"status must be {STATUS_COEXIST} or {STATUS_SWITCHED}"}
        if status == STATUS_COEXIST and not app_id:
            return {"error": "need the app name to record a coexist"}
        before = (user.food_logger, user.food_logger_status)
        if status == STATUS_COEXIST:
            if user.food_logger_status != STATUS_COEXIST or user.food_logger != app_id:
                user.food_logger_since = _naive_utcnow()
            user.food_logger = app_id
            user.food_logger_status = STATUS_COEXIST
        else:
            user.food_logger = app_id
            user.food_logger_status = STATUS_SWITCHED
        changed = before != (user.food_logger, user.food_logger_status)
        if own:
            session.commit()
        else:
            session.flush()
        if changed:
            logger.info("FOOD_LOGGER user=%s %s -> %s/%s source=%s", user_id, before, app_id, status, source)
        return {"app": app_id, "status": status, "changed": changed}
    finally:
        if own:
            session.close()


def _remember_switch(user_id: int, app_id: str | None) -> None:
    """The dated fact: 'switched to cued from <app> on <date>'. Best-effort."""
    try:
        from agent_tools import handle_remember
        when = _naive_utcnow().strftime("%Y-%m-%d")
        handle_remember(user_id, {"action": "add", "category": "identity",
                                  "text": f"switched to cued from {app_label(app_id)} on {when}"})
    except Exception as e:  # noqa: BLE001
        logger.warning("FOOD_LOGGER_REMEMBER_FAILED user=%s err=%s", user_id, e)


# ─── the replace rule + parity WRITE live in agent_tools (PR #91): log_meal refuses a
# from_app write into an occupied slot (LOG_MEAL_SLOT_REFUSED) and manage_log edit with
# from_app flips provenance and records Signal(kind="parity", payload={meal_id, cued_cal,
# app_cal, delta_pct}). This module READS those signals (parity_week) and owns the state.


def app_write_side_effects(user_id: int, *, app: str | None) -> None:
    """A screenshot-sourced write on a user with no food_logger recorded is the strongest
    evidence they still use it — record the coexist."""
    from models import get_session, User
    s = get_session()
    try:
        u = s.get(User, user_id)
        if u and not u.food_logger_status:
            set_food_logger(user_id, app or "other", STATUS_COEXIST, source="screenshot", session=s)
            s.commit()
    finally:
        s.close()


# ─── context ───────────────────────────────────────────────────────────────

def _week_counts(session, user, now: datetime) -> tuple[int, int, datetime | None]:
    """(days with an app-sourced meal, days with only cued-sourced meals, last app day) over 7 days."""
    from models import Meal, active
    from timefmt import resolve_tz
    tz = resolve_tz(user)
    since = now - timedelta(days=7)
    rows = (active(session, Meal, user_id=user.id)
            .filter(Meal.eaten_at >= since).all())
    app_days, cued_days = set(), set()
    last_app = None
    for m in rows:
        d = m.eaten_at.replace(tzinfo=timezone.utc).astimezone(tz).date()
        if m.source == APP_SOURCE:
            app_days.add(d)
            if last_app is None or m.eaten_at > last_app:
                last_app = m.eaten_at
        else:
            cued_days.add(d)
    return len(app_days), len(cued_days - app_days), last_app


def parity_week(session, user, now: datetime) -> tuple[int, int]:
    """(close comparisons, misses) in the last 7 days."""
    from models import Signal
    since = now - timedelta(days=7)
    rows = (session.query(Signal)
            .filter(Signal.user_id == user.id, Signal.kind == "parity", Signal.ts >= since).all())
    close = sum(1 for r in rows if abs((r.payload or {}).get("delta_pct", 999)) <= PARITY_CLOSE_PCT)
    miss = sum(1 for r in rows if abs((r.payload or {}).get("delta_pct", 0)) > PARITY_MISS_PCT)
    return close, miss


def context_block(user, session, *, now: datetime | None = None) -> str | None:
    """## OTHER FOOD LOGGER — only for a coexisting user. Everything the model would
    otherwise have to guess (days since, counts, whether today has a screenshot) is
    computed here."""
    if not config.FOOD_LOGGER_BRIDGE_ENABLED:
        return None
    if getattr(user, "food_logger_status", None) != STATUS_COEXIST:
        return None
    now = now or _naive_utcnow()
    from timefmt import resolve_tz
    tz = resolve_tz(user)
    label = app_label(user.food_logger)
    since = user.food_logger_since
    since_txt = ""
    if since:
        days = max(0, (now - since).days)
        since_txt = f" (since {since.replace(tzinfo=timezone.utc).astimezone(tz):%b %d}, {days} days)"
    app_days, cued_only_days, last_app = _week_counts(session, user, now)
    last_txt = (f"Last screenshot-sourced day: {last_app.replace(tzinfo=timezone.utc).astimezone(tz):%b %d}."
                if last_app else "No screenshot-sourced day yet.")
    lines = [
        f"## OTHER FOOD LOGGER (code-computed)",
        f"They still log food in {label} alongside you{since_txt}. An EMPTY day here is NOT an "
        f"unlogged day — it's probably in their app. Never ask them to re-type what they already "
        f"logged there: ask for a screenshot of the day (one tap, you read the numbers off it) or an "
        f"end-of-day total. If they ask to 'connect' the app: there's no connection, the screenshot IS "
        f"the bridge — say that plainly. {last_txt} Screenshot days in the last 7: {app_days}. "
        f"cued-only days in the last 7: {cued_only_days}.",
    ]
    close, miss = parity_week(session, user, now)
    if close >= PARITY_CLOSE_NEEDED and miss == 0 and not getattr(user, "parity_suggested_at", None):
        lines.append(f"Parity: {close} of {close} close this week and no misses. Suggest ONCE, casually, that "
                     f"they can drop {label} — then never again unless they ask. (Code marks it said.)")
    return "\n".join(lines)


def mark_parity_suggested(user_id: int) -> None:
    from models import get_session, User
    s = get_session()
    try:
        u = s.get(User, user_id)
        if u and not u.parity_suggested_at:
            u.parity_suggested_at = _naive_utcnow()
            s.commit()
    finally:
        s.close()


def known_gap_line(user) -> str | None:
    if not config.FOOD_LOGGER_BRIDGE_ENABLED:
        return None
    if getattr(user, "food_logger_status", None) == STATUS_COEXIST:
        return "today's food is probably in their other app — not a gap to ask about"
    return None


# ─── graduation (nightly) ──────────────────────────────────────────────────

def graduate(user_id: int, *, now: datetime | None = None) -> dict:
    """coexist → switched after GRADUATE_DAYS with zero app-sourced meals and at least
    GRADUATE_MIN_CUED_DAYS cued-logged days. One human-readable log line per change."""
    from models import get_session, User, Meal, active
    from timefmt import resolve_tz
    now = now or _naive_utcnow()
    s = get_session()
    try:
        u = s.get(User, user_id)
        if not u or u.food_logger_status != STATUS_COEXIST:
            return {"status": "noop"}
        since = now - timedelta(days=GRADUATE_DAYS)
        if u.food_logger_since and u.food_logger_since > since:
            return {"status": "too_recent"}
        tz = resolve_tz(u)
        rows = active(s, Meal, user_id=user_id).filter(Meal.eaten_at >= since).all()
        app_meals = sum(1 for m in rows if m.source == APP_SOURCE)
        cued_days = len({m.eaten_at.replace(tzinfo=timezone.utc).astimezone(tz).date()
                         for m in rows if m.source != APP_SOURCE})
        if app_meals > 0 or cued_days < GRADUATE_MIN_CUED_DAYS:
            return {"status": "holding", "app_meals": app_meals, "cued_days": cued_days}
        app_id = u.food_logger
        set_food_logger(user_id, app_id, STATUS_SWITCHED, source="graduation", session=s)
        s.commit()
    finally:
        s.close()
    _remember_switch(user_id, app_id)
    logger.info("FOOD_LOGGER_GRADUATED user=%s app=%s days=%d cued_days=%d — no app-sourced meals; now switched",
                user_id, app_id, GRADUATE_DAYS, cued_days)
    return {"status": "graduated", "app": app_id, "cued_days": cued_days}


def graduate_all() -> int:
    from models import get_session, User
    s = get_session()
    try:
        ids = [u.id for u in s.query(User).filter(User.active.is_(True),
                                                  User.food_logger_status == STATUS_COEXIST).all()]
    finally:
        s.close()
    n = 0
    for uid in ids:
        try:
            if graduate(uid).get("status") == "graduated":
                n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("FOOD_LOGGER_GRADUATE_FAILED user=%s err=%s", uid, e)
    return n
