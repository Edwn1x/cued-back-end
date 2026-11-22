"""
Bail turn (bail.py): the moment they back out of today's session, the push rule arrives in the
prompt — at that moment, not buried on line 71 of voice.md.

The rule has been in voice.md since PR #245 (founder, 2026-10-18: "the whole purpose of the coach is
to motivate them to their goals"), with the founder's own lines as the template. The coach still
folds on the first word of pushback: "Ughhh I don't want toooo" → "lol ok don't then" (u40, the
founder's three pushes then got him to the gym); "I'll prolly hit back instead ngl" → "alr, back
monday then" (u51, legs skipped for a month); "i didnt end up goign to work out sorry keep it next
time" → "i cleared the legs card you didn't use" (u56, day one). A prompt line competes with the
model's prior toward agreeing; a block that fires on the turn itself, naming the session they're
backing out of, how many times they've pushed back, whether a real reason was named, and the
scoreboard's LEAD, is what a coach with a stake would be holding in their head.

Code detects (looks_like_bail + a session live today); the model writes the push. A real reason
(pain, sick, exam tonight, work / class / dance now, past bedtime) ends it in one line and re-arms
tomorrow. After two real pushes and a firm no, step aside — the founder's rule, not a nag loop.

Flag BAIL_TURN_BLOCK_ENABLED (default on). Fail-open.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import config

logger = logging.getLogger("cued.bail")

BAIL_RE = re.compile(
    r"\b(don'?t (?:really )?(?:wanna|want to|feel like)|dont (?:really )?(?:wanna|want to|feel like)|not (?:really )?feeling it"
    r"|not today|not tonight|not (?:gonna|going to) (?:make it|go|happen)|not happening|can'?t (?:go|make it|do it) (?:today|tonight|rn)"
    r"|(?:gonna|going to|think i'?ll|i'?ll|imma|ima) (?:skip|pass|sit this one out|rain ?check)|skip(?:ping)? (?:today|tonight|it|the gym|legs|push|pull)"
    r"|too tired|exhausted|no energy|rain ?check|next time|maybe (?:tmrw|tomorrow|mon|tue|wed|thu|fri|sat|sun)\w*"
    r"|(?:prolly|probably|think i'?ll|gonna) (?:hit|do) \w+ instead|didn'?t (?:end up )?(?:go|going|make it|work ?out)|ended up not going"
    r"|keep it (?:for )?next time|push (?:it|legs|today) to|move (?:it|legs|today) to|got plans|ughh+"
    r"|can'?t (?:go|make it|do it) (?:till|until|before|again until)|(?:thats|that'?s) my workout (?:for )?(?:the day|today)"
    r"|(?:will|gonna|imma|ima) just (?:go to|do) \w+ (?:instead|tonight|today))\b", re.I)
# A reason a coach accepts in one line. Named in THEIR text.
REAL_REASON_RE = re.compile(
    r"\b(hurt|hurts|pain|sore (?:af|as)|sick|ill|fever|cough|throwing up|migraine|injur\w*|sprain|doctor|urgent care"
    r"|exam (?:tonight|tmrw|tomorrow|in the morning)|midterm|final(?:s)? (?:tonight|tmrw|tomorrow|week)|due (?:tonight|at midnight|tmrw)"
    r"|work(?:ing)? (?:till|until|late|a shift|tonight)|shift|class (?:till|until)|rehearsal|practice (?:till|tonight)|dance|game|match"
    r"|flight|driving|out of town|funeral|family|too late|past (?:my )?bedtime|it'?s (?:1|2|3)\s?am)\b", re.I)
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def enabled() -> bool:
    return bool(getattr(config, "BAIL_TURN_BLOCK_ENABLED", True))


def looks_like_bail(text: str | None) -> bool:
    return bool(text) and bool(BAIL_RE.search(text))


def real_reason(text: str | None) -> str | None:
    m = REAL_REASON_RE.search(text or "")
    return m.group(0) if m else None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def session_live_today(user, session, now: datetime | None = None) -> str | None:
    """Why there is a session to defend today, or None: a planned / active card, a program or
    training day today with no completed session yet, or a gym ping / poll about today."""
    now = now or _now()
    from models import WorkoutSession, Message
    from timefmt import local_day_bounds, to_local
    day0, day1 = local_day_bounds(user, now=now)
    done = (session.query(WorkoutSession.id)
            .filter(WorkoutSession.user_id == user.id, WorkoutSession.status == "done",
                    WorkoutSession.date >= day0, WorkoutSession.date < day1).first())
    if done:
        return None
    open_card = (session.query(WorkoutSession)
                 .filter(WorkoutSession.user_id == user.id, WorkoutSession.status.in_(("planned", "active")))
                 .order_by(WorkoutSession.id.desc()).first())
    if open_card:
        return f"their {open_card.template_key or 'workout'} card is already on their phone ({open_card.status})"
    today = _DAYS[to_local(now, user).weekday()]
    prog = getattr(user, "program", None)
    if isinstance(prog, dict) and prog.get("days", {}).get(today):
        t = (prog.get("times") or {}).get(today)
        return f"today is {prog['days'][today]} day on their program" + (f" at {t}" if t else "")
    raw = (getattr(user, "confirmed_training_days", None) or getattr(user, "workout_days", None) or "").lower()
    named = {t[:3] for t in re.split(r"[\s,/&+]+", raw) if t} & set(_DAYS)
    if today in named:
        return "today is one of their training days"
    ping = (session.query(Message.body)
            .filter(Message.user_id == user.id, Message.direction == "out", Message.created_at >= day0,
                    Message.message_type.in_(("reminder", "heartbeat", "gym_dead", "gym_optin_ask", "poll")))
            .order_by(Message.id.desc()).limit(12).all())
    for (body,) in ping:
        if body and re.search(r"\b(gym time|legs|push|pull|upper|lower|workout|lift|session|card)\b", body, re.I):
            return "the coach already brought up today's session"
    return None


def pushbacks_recent(user_id: int, session, now: datetime | None = None, hours: int = 2) -> int:
    from models import Message
    now = now or _now()
    rows = (session.query(Message.body)
            .filter(Message.user_id == user_id, Message.direction == "in", Message.created_at >= now - timedelta(hours=hours))
            .all())
    return sum(1 for (b,) in rows if looks_like_bail(b))


def context_block(user, text: str, session, *, now: datetime | None = None) -> str | None:
    if not enabled() or not looks_like_bail(text):
        return None
    if (getattr(user, "onboarding_step", 0) or 0) < 3:
        return None
    try:
        why = session_live_today(user, session, now)
        if not why:
            return None
        n = pushbacks_recent(user.id, session, now)      # includes this text once stored; may be 0 pre-store
        reason = real_reason(text)
        lead = None
        try:
            from scoreboard import compute
            lead = compute(user, session, now).get("lead")
        except Exception:  # noqa: BLE001
            lead = None
        name = (getattr(user, "name", None) or "they").split()[0]
        lines = [f"## BAIL TURN — {name} is backing out of today's session ({why}). This is the whole job."]
        if reason:
            lines.append(f"- They NAMED a real reason (\"{reason}\"): one line that takes it seriously, no push. "
                         f"Then re-arm: set_reminder for tomorrow (or the day they named) at their usual time and SAY you did. "
                         f"Never argue with pain, sickness, an exam, work or a class.")
        else:
            lines.append("- No real reason named. PUSH, warmly and for real, in THIS reply: no 'ok don't then', no 'alr, tomorrow "
                         "then', no 'no worries' / 'fair'. Ask what's in the way AND make it small and concrete (20 min, 3 "
                         "machines, just the first exercise then bail if u want). Make it about the habit and the day ('day one "
                         "or one day'), tie it to the goal THEY said. Never 'it's late anyway' at their normal slot. Vary the angle.")
            if n >= 2:
                lines.append(f"- This is pushback #{n} in the last two hours. One more real push; if the next reply is a firm no, "
                             f"step aside: 'alr, tomorrow then', set_reminder tomorrow same time, say you did.")
            else:
                lines.append("- First pushback. Do not step aside yet.")
        lines.append("- A session they MOVE ('can't till wednesday') is the same deal: the reminder moves with it (update_reminder / "
                     "set_reminder) and you say so. Never claim 'it's on the books' unless a reminder exists.")
        if lead:
            lines.append(f"- SCOREBOARD LEAD: {lead}")
        logger.info("BAIL_TURN user=%s why=%r reason=%r pushbacks=%s", user.id, why, reason, n)
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — never break a turn over this block
        logger.warning("BAIL_BLOCK_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return None
