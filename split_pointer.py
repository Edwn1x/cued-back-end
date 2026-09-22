"""
Split pointer — two facts + provenance, advanced only by code.

Stores the last COMPLETED split day, when, and whether that day was confirmed by
the user or inferred by code (an unnamed "already went"). The model derives
"today is probably legs" from this at reasoning time (Phase 2); code never answers
that question.

DESIGN CONSTRAINT: SPLIT_CYCLES / _next_day are WRITE-TIME ONLY — used solely to
compute the inferred advance. There is deliberately no read-time "what's today's
workout" function. The moment code answers that from the cycle, the cycle rule
engine we killed is back. get_split_pointer() returns the raw pointer only.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from models import get_session, User

logger = logging.getLogger("cued.split")
DEFAULT_TZ = "America/Los_Angeles"

# Write-time-only cycle mapping (see DESIGN CONSTRAINT above).
SPLIT_CYCLES = {
    "ppl": ["push", "pull", "legs"],
    "push_pull_legs": ["push", "pull", "legs"],
    "upper_lower": ["upper", "lower"],
    "ul": ["upper", "lower"],
    "full_body": ["full_body"],
    "arnold": ["chest_back", "shoulders_arms", "legs"],
    # The classic five-day bro split. A user's OWN grouping (users.split_days —
    # "chest and biceps, back and triceps, legs and shoulders") overrides this; see
    # cycle_for(). Live 2026-09-22 (user 43): "bro_split" had no cycle → full_body card.
    "bro_split": ["chest", "back", "shoulders", "arms", "legs"],
    "bro": ["chest", "back", "shoulders", "arms", "legs"],
}


def cycle_for(user) -> list[str]:
    """The user's split as an ordered day cycle: their stated days first
    (users.split_days), then their pasted routine's days, then the named system's
    cycle. [] when the split is unknown or unmapped — callers must not guess."""
    days = getattr(user, "split_days", None)
    if isinstance(days, list) and len(days) >= 2 and all(isinstance(d, str) and d for d in days):
        return list(days)
    system = _normalize_system(getattr(user, "current_split", None)
                               or getattr(user, "confirmed_training_split", None) or "")
    custom_keys: list[str] = []
    custom = getattr(user, "custom_templates", None)
    if isinstance(custom, dict) and len(custom) >= 2:
        from workouts.templates import normalize_template_key
        custom_keys = [k for k in (normalize_template_key(x) for x in custom) if k]
    # A pasted routine defines the cycle when the label is loose ("custom", a bro
    # split); a named system (ppl / upper_lower) keeps its canonical order even if
    # only some of its days were pasted.
    if len(custom_keys) >= 2 and system in ("", "custom", "bro_split", "bro"):
        return custom_keys
    return list(SPLIT_CYCLES.get(system) or (custom_keys if len(custom_keys) >= 2 else []))

# Precision-biased: only unambiguous canonical day names (a bare "chest"/"arms"
# maps differently across systems, so we don't guess — those fall through to an
# inferred advance, or to an explicit named_day passed by a caller).
_DAY_RE = re.compile(r"\b(push|pull|legs?|upper|lower|full\s*body)\b", re.IGNORECASE)
_DAY_CANON = {"push": "push", "pull": "pull", "leg": "legs", "legs": "legs",
              "upper": "upper", "lower": "lower", "fullbody": "full_body"}


def _normalize_system(system: str) -> str:
    return (system or "").strip().lower().replace("-", "_").replace(" ", "_")


def _next_day(system_or_user, last_day: str):
    """WRITE-TIME ONLY. Next day in the cycle after last_day, or None. Takes a User
    (their own days win) or, for callers that only have the label, a system string."""
    if isinstance(system_or_user, str) or system_or_user is None:
        cycle = SPLIT_CYCLES.get(_normalize_system(system_or_user))
    else:
        cycle = cycle_for(system_or_user)
    if not cycle or last_day not in cycle:
        return None
    return cycle[(cycle.index(last_day) + 1) % len(cycle)]


def parse_named_split_day(message: str):
    """Canonical split day explicitly named in a message, or None (precision-biased)."""
    m = _DAY_RE.search(message or "")
    if not m:
        return None
    return _DAY_CANON.get(re.sub(r"\s+", "", m.group(1).lower()))


def _pointer_dict(user):
    if not user or not user.split_pointer_day:
        return None
    return {"day": user.split_pointer_day,
            "at": user.split_pointer_at,
            "source": user.split_pointer_source}


def _local_date(dt_naive_utc, tz):
    return dt_naive_utc.replace(tzinfo=timezone.utc).astimezone(tz).date()


def _advanced_today(user, now_utc_naive) -> bool:
    if not user.split_pointer_at:
        return False
    try:
        tz = ZoneInfo(user.user_timezone or DEFAULT_TZ)
    except Exception:
        tz = ZoneInfo(DEFAULT_TZ)
    return _local_date(user.split_pointer_at, tz) == _local_date(now_utc_naive, tz)


def advance_split_pointer(user_id: int, *, named_day: str = None, at=None) -> dict | None:
    """
    Code-mediated advancement (the only writer). Policy:
      - named_day given  -> set that day, source='confirmed'. Overrides, including
        overwriting an inferred same-day value (the correction seam — a later
        "actually I did arms" wins; Phase 3 manage_log edits through here too).
      - named_day None (unnamed "already went") -> advance to the rule-expected
        next day from the current pointer + split system, source='inferred'. Once
        per local day (a second "already went" the same day is a no-op). If the
        next day can't be computed (no current day / unknown system), the pointer
        is left unchanged — never guess.
      - A missed day is simply the absence of a completion, so the pointer doesn't
        advance and the slot isn't skipped.
    """
    session = get_session()
    try:
        user = (session.query(User).filter(User.id == user_id)
                .with_for_update().one_or_none())
        if not user:
            return None
        now = at or datetime.now(timezone.utc).replace(tzinfo=None)

        if named_day:
            user.split_pointer_day = named_day
            user.split_pointer_at = now
            user.split_pointer_source = "confirmed"
            session.commit()
            logger.info("SPLIT_POINTER user=%s day=%s source=confirmed", user_id, named_day)
            return _pointer_dict(user)

        if _advanced_today(user, now):
            logger.info("SPLIT_POINTER_SKIP user=%s reason=already_advanced_today", user_id)
            return _pointer_dict(user)

        nxt = _next_day(user, user.split_pointer_day)
        if nxt:
            user.split_pointer_day = nxt
            user.split_pointer_at = now
            user.split_pointer_source = "inferred"
            session.commit()
            logger.info("SPLIT_POINTER user=%s day=%s source=inferred", user_id, nxt)
        else:
            logger.info("SPLIT_POINTER_NOINFER user=%s (no current day / unknown cycle)", user_id)
        return _pointer_dict(user)
    finally:
        session.close()


def restore_split_pointer(user_id: int, prev: dict | None) -> dict | None:
    """Rollback seam for manage_log delete: put the pointer back to a serialized
    prior value (None clears it). Code-mediated like advance; never inferred."""
    session = get_session()
    try:
        user = (session.query(User).filter(User.id == user_id)
                .with_for_update().one_or_none())
        if not user:
            return None
        if prev:
            at = prev.get("at")
            user.split_pointer_day = prev.get("day")
            user.split_pointer_at = datetime.fromisoformat(at) if isinstance(at, str) else at
            user.split_pointer_source = prev.get("source")
        else:
            user.split_pointer_day = None
            user.split_pointer_at = None
            user.split_pointer_source = None
        session.commit()
        logger.info("SPLIT_POINTER user=%s day=%s source=restored", user_id, user.split_pointer_day)
        return _pointer_dict(user)
    finally:
        session.close()


def get_split_pointer(user_id: int) -> dict | None:
    """Read the raw pointer (day / at / source). Does NOT compute today's workout."""
    session = get_session()
    try:
        return _pointer_dict(session.get(User, user_id))
    finally:
        session.close()
