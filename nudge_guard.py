"""Anti-nagging: recent-nudge awareness.

Live 2026-09-28 (founder, user 31): the coach delivered essentially the SAME
standing nudge ~5× in one day across separate interactions — "eat some protein /
you got beef and eggs" (reactive replies AND the morning brief). It re-derives the
same standing exhortation every turn with no awareness it already said it, so it
reads as nagging. This is DIFFERENT from the per-flush send-dedup (PR #134): this
is the SAME nudge TOPIC re-issued across separate turns over hours, on both the
reactive replies and the heartbeat.

The fix gives the coach visibility into which nudge TOPICS it has already raised
today by scanning its own recent OUTBOUND messages (every path — reactive AND
heartbeat — writes an outbound Message row, so one scan covers all of them). We
classify each outbound into coarse, precision-biased topics (protein, eating,
water, sleep, workout, weigh-in) and surface a compact "ALREADY NUDGED TODAY"
line. It is advisory, not a hard suppressor: a genuinely new fact (a real number
update) is fine — it's the repeated *same exhortation to fix* that nags.

Cheap + robust: ONE bounded query (local-day window, capped count), keyword
classification, flag-gated (NUDGE_REPETITION_GUARD_ENABLED, default ON), and
fail-open (any query/classification error → today's behavior, i.e. no block).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger("cued.nudge_guard")


# Coarse nudge topics → precision-biased keyword patterns. Every pattern is
# word-boundary anchored: a bare substring classifier once turned "egg whites"
# (⊃ "hit") into workout_log (see MEMORY: legacy keyword substring false
# positives), so we never match inside a word. Precision over recall — a missed
# nudge just means today's behavior; a false positive would nag-suppress a real
# reply, which is worse.
_TOPIC_PATTERNS: dict[str, list[str]] = {
    "protein": [r"\bprotein\b"],
    "eat": [
        r"\beat (?:more|something|some(?:thing)?|a meal|real food|some food)\b",
        r"\bget (?:some )?food\b",
        r"\bget (?:a )?meal in\b",
        r"\bneed to eat\b",
        r"\bhaven'?t eaten\b",
        r"\bgrab (?:a|some) (?:meal|food|bite)\b",
        r"\bfuel up\b",
    ],
    "water": [r"\b(?:water|hydrate|hydration|hydrated)\b"],
    "sleep": [
        r"\b(?:sleep|asleep|bedtime)\b",
        r"\bgo to bed\b",
        r"\bhead to bed\b",
        r"\bget (?:some )?rest\b",
        r"\bwind down\b",
    ],
    "workout": [
        r"\b(?:workout|gym|training|lifting)\b",
        r"\bwork out\b",
        r"\bget a (?:lift|session|workout) in\b",
        r"\bhit the gym\b",
    ],
    "weigh_in": [
        r"\bweigh[- ]?in\b",
        r"\bweigh (?:yourself|in)\b",
        r"\bstep on the scale\b",
        r"\blog your weight\b",
    ],
}

_COMPILED: dict[str, list[re.Pattern]] = {
    topic: [re.compile(p, re.IGNORECASE) for p in pats]
    for topic, pats in _TOPIC_PATTERNS.items()
}

# Human-readable labels for the context line.
_LABELS = {
    "protein": "protein",
    "eat": "eating/food",
    "water": "water",
    "sleep": "sleep",
    "workout": "workout/gym",
    "weigh_in": "weigh-in",
}


def classify_nudge_topics(text: str | None) -> set[str]:
    """Coarse topic set for one outbound body. A message can touch several topics
    ("get some sleep, and eat something first"); it contributes to each it matches.
    Precision-biased word-boundary matching only."""
    if not text:
        return set()
    topics: set[str] = set()
    for topic, regexes in _COMPILED.items():
        for rx in regexes:
            if rx.search(text):
                topics.add(topic)
                break
    return topics


def _window_start_utc(user) -> datetime:
    """Naive-UTC start of the lookback window: TODAY (local midnight), further
    clamped to the last N hours so a very long day stays bounded. Matches the
    naive-UTC convention Message.created_at is stored in."""
    try:
        tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("America/Los_Angeles")
    now_local = datetime.now(tz)
    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    lookback = now_local - timedelta(hours=config.NUDGE_GUARD_LOOKBACK_HOURS)
    start_local = max(day_start, lookback)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)


def recent_nudge_topics(user, session) -> dict[str, int]:
    """{topic: count} for nudge topics already raised in this user's recent
    OUTBOUND messages. ONE bounded query (window + capped scan). Fail-open: any
    error → {} (today's behavior). Reactions/tapbacks are excluded (they carry no
    nudge and closing a loop with 👍 must not count)."""
    from models import Message
    from engagement_tracker import _not_reaction

    counts: dict[str, int] = {}
    try:
        start = _window_start_utc(user)
        rows = (
            session.query(Message.body)
            .filter(
                Message.user_id == user.id,
                Message.direction == "out",
                Message.created_at >= start,
                _not_reaction(),
            )
            .order_by(Message.created_at.desc())
            .limit(config.NUDGE_GUARD_MAX_SCAN)
            .all()
        )
    except Exception as e:  # noqa: BLE001 — never break a turn over a hint
        logger.warning("NUDGE_GUARD_QUERY_FAILED user=%s err=%s",
                       getattr(user, "id", None), e)
        return {}

    for (body,) in rows:
        for topic in classify_nudge_topics(body):
            counts[topic] = counts.get(topic, 0) + 1
    return counts


def nudge_guard_block(user, session) -> str | None:
    """Compact context line naming the nudge topics already raised today, or None
    when the guard is off / nothing has been nudged. Fed into build_loop_context
    (reactive replies) and, through it, into the heartbeat's _proactive_context —
    one scan covers every proactive path."""
    if not config.NUDGE_REPETITION_GUARD_ENABLED:
        return None
    counts = recent_nudge_topics(user, session)
    if not counts:
        return None
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    listed = ", ".join(f"{_LABELS.get(t, t)} ({n}x)" for t, n in ordered)
    return (
        "## ALREADY NUDGED TODAY\n"
        f"{listed} — you have ALREADY raised these with them today (across your replies "
        "AND any proactive check-ins). Do NOT restate the same nudge or the same line. If "
        "a gap is still unmet, either change the angle / escalate meaningfully or let it "
        "REST — after ~1–2 mentions of a standing gap, drop it unless they bring it up. A "
        "genuinely new fact (a real number update, new info) is fine; it's the repeated "
        "SAME exhortation to fix that reads as nagging."
    )
