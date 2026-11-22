"""
Self-knowledge (self_model.py): what the coach IS for this person, right now.

Across eleven threads (audit 2026-11-21) the coach denied things it can do and claimed things
it didn't do: "google health isn't ready yet" (live a month, user 80), "i can't make a ping
wait until the RSF is under 85%" (the gym_quiet beat does exactly that, user 86), "i can't
move us to iMessage" (user 100), "i can't send anything to the founders" (user 96), "i can't
do animations, only texts" (every water ping is an animated sticker, user 56), "idk who edwin
is" (user 33, whose thread the team reads), an apology for a message the founder typed as the
coach (user 42), and a lesson written into the founder's own summary about gif floods that
were his test sends. Domain knowledge was fine in all of them; what was missing was knowing
itself: what it can do for THIS person on THIS channel, what its outputs look like on their
phone, who else writes in the thread, and which store a stated fact has to land in before
anything downstream (targets, pings, briefings) sees it.

One block, rendered into every prompt that speaks as the coach (the reply loop, the heartbeat
through build_loop_context, the intake turn, the old setup agent in compact form):

  - WHAT YOU CAN DO FOR THEM RIGHT NOW — every enabled capability's `what`, from the
    registry (capabilities.py), plus what is NOT on for them (channel / flag), stated.
  - WHAT YOUR OUTPUTS LOOK LIKE ON THEIR PHONE — per channel, in the words the history uses.
  - WHO ELSE WRITES HERE — team-typed lines are labelled in RECENT CONVERSATION and are not
    the coach's to apologize for or disown; the team reads the thread.
  - WHERE A FACT HAS TO LIVE — profile columns drive targets / pings / briefings / cards;
    `remember` is a note. The update_profile tool (agent_tools) is the pen for those columns.
  - SENT TO THEM TODAY BY CODE — counts, so the coach knows what already landed.

Flag SELF_MODEL_ENABLED (default on). Fail-open: any error → no block, never a broken turn.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config

logger = logging.getLogger("cued.self_model")

# The label RECENT CONVERSATION gives an outbound row the team typed (message_type "admin").
TEAM_LABEL = "Coach (typed by the cued team)"

# message_type → how it reads in a one-line "sent today" summary. Everything else is the
# coach's own voice and is already visible as Coach: lines.
_CODE_SENT_TYPES = {
    "water_reminder": "water sticker",
    "reminder": "reminder text",
    "heartbeat": "check-in from the heartbeat",
    "card_setup": "card setup line",
    "workout_intro": "workout intro",
    "workout_card": "workout card",
    "connect_offer": "connect offer",
    "connect_link": "connect link",
    "program_brief": "program brief",
    "onboarding_nudge": "setup nudge",
    "water_offer": "water offer",
    "gym_beat": "gym ping",
    "gym_line": "gym line ping",
    "stat_card": "stat card",
}

_PROFILE_FACTS = ("steps a day", "wake / sleep times", "workout time and days", "weight", "how active their day is",
                  "equipment", "experience", "anything that hurts", "what they do (job / school)", "timezone")


def enabled() -> bool:
    return bool(getattr(config, "SELF_MODEL_ENABLED", True))


def _channel(user) -> str:
    try:
        from sms import _resolve_channel
        return _resolve_channel(user.id) or "sms"
    except Exception:  # noqa: BLE001 — a channel read must never break a prompt
        return (getattr(user, "preferred_channel", None) or "sms")


def capability_lines(user) -> tuple[list[str], list[str]]:
    """([what, …] for enabled capabilities, [what, …] for the ones NOT on for this user)."""
    from capabilities import CAPABILITIES, available
    on = available(user)
    on_ids = {c.id for c in on}
    off = [c for c in CAPABILITIES if c.id not in on_ids]
    return [c.what for c in on], [c.what for c in off]


def outputs_lines(channel: str) -> list[str]:
    if channel == "imessage":
        return [
            "a workout card = a tappable card inside Messages (it needs the small iMessage extension, one tap to "
            "add, nothing on their home screen); in RECENT CONVERSATION it reads '[workout card: …]'.",
            "a water ping = an ANIMATED water sticker; in RECENT CONVERSATION it reads '[water-break gif]'. Every "
            "water ping is one. Never say you can't do animations or only send texts.",
            "a stat card / day card = a picture of their numbers. a poll = tappable options. a reaction = a tapback "
            "on their bubble (code sends the one they asked for).",
            "a reminder = a plain text at the time it's set for; a 'remind me to drink water' is the sticker above.",
        ]
    return [
        "they're on SMS: everything is plain text. a workout card comes as a link; a water ping comes as a picture "
        "(MMS); no stickers, polls or tapbacks reach them.",
        "a reminder = a plain text at the time it's set for.",
    ]


def team_lines() -> list[str]:
    return [
        f"Lines in RECENT CONVERSATION labelled '{TEAM_LABEL}' were typed by the people who build cued, in your "
        "voice. They are not yours: never apologize for them, never claim them, never argue with them as if "
        "you'd said them. Treat them as true about what cued can do.",
        "The team reads this thread when something matters (a bug, safety). You can't DM them separately — "
        "saying it here reaches them; that is the honest answer to 'tell the founders'.",
    ]


def fact_store_lines(*, tools_available: bool) -> list[str]:
    pen = ("write it with update_profile (weight → log_weight, the city they're in → set_weather_location) "
           "in the same turn, THEN remember it if there's colour worth keeping"
           if tools_available else
           "code stores it from your conversation; say it back so they know it landed")
    return [
        "Targets, water pings, the morning brief and the workout cards read the PROFILE columns: "
        + ", ".join(_PROFILE_FACTS) + ".",
        f"When they state one of those ('i get like 10k steps', 'i'm up at 8 now', 'i train at 6 not 2'), {pen}. "
        "`remember` alone is a note — it changes none of their numbers.",
    ]


def sent_today(user, session=None) -> list[tuple[str, int, str | None]]:
    """[(label, count, last local HH:MM)] of code-sent outbound today, most recent first."""
    from models import get_session, Message
    from timefmt import local_day_bounds, to_local
    own = session is None
    s = session or get_session()
    try:
        day0, day1 = local_day_bounds(user)
        rows = (s.query(Message.message_type, Message.created_at)
                .filter(Message.user_id == user.id, Message.direction == "out",
                        Message.created_at >= day0, Message.created_at < day1,
                        Message.message_type.in_(tuple(_CODE_SENT_TYPES)))
                .all())
    finally:
        if own:
            s.close()
    agg: dict[str, list] = {}
    for mt, at in rows:
        a = agg.setdefault(mt, [0, None])
        a[0] += 1
        if at and (a[1] is None or at > a[1]):
            a[1] = at
    out = []
    for mt, (n, last) in agg.items():
        hhmm = to_local(last, user).strftime("%H:%M") if last else None
        out.append((_CODE_SENT_TYPES[mt], n, hhmm))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def context_block(user, session=None, *, tools_available: bool = True) -> str | None:
    """The loop / heartbeat / intake block. None when off or on any failure."""
    if not enabled():
        return None
    try:
        channel = _channel(user)
        on, off = capability_lines(user)
        parts = ["## WHAT YOU ARE FOR " + (getattr(user, "name", None) or "them").upper() + " RIGHT NOW"]
        parts.append("WHAT YOU CAN DO (all of it, today — never say you can't do one of these):")
        parts.extend(f"- {w}" for w in on)
        if off:
            parts.append("NOT on for them (say so plainly if asked; don't offer it): " + "; ".join(off))
        parts.append("WHAT YOUR OUTPUTS LOOK LIKE ON THEIR PHONE:")
        parts.extend(f"- {line}" for line in outputs_lines(channel))
        parts.append("WHO ELSE WRITES HERE:")
        parts.extend(f"- {line}" for line in team_lines())
        parts.append("WHERE A FACT HAS TO LIVE:")
        parts.extend(f"- {line}" for line in fact_store_lines(tools_available=tools_available))
        sent = sent_today(user, session)
        if sent:
            parts.append("SENT TO THEM TODAY BY CODE, NOT BY YOU (they already got these; don't re-send, don't deny):")
            parts.extend(f"- {n}× {label}" + (f" (last {hhmm})" if hhmm else "") for label, n, hhmm in sent)
        return "\n".join(parts)
    except Exception as e:  # noqa: BLE001 — never break a turn over self-knowledge
        logger.warning("SELF_MODEL_BLOCK_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return None


def onboarding_block(user) -> str | None:
    """Compact form for the OLD setup agent (no tools): what cued does, what it looks like, who
    else writes — so setup never says 'idk if i can do that yet' about a thing cued does."""
    if not enabled():
        return None
    try:
        channel = _channel(user)
        on, _off = capability_lines(user)
        lines = ["## WHAT CUED DOES (you are cued; during setup you can't trigger these yourself, code does them "
                 "once the basics are in — so NEVER say cued can't or doesn't do one of these. 'yep, that's a "
                 "thing — once we're set up' is the honest line):"]
        lines.extend(f"- {w}" for w in on)
        lines.append("WHAT IT LOOKS LIKE ON THEIR PHONE: " + " ".join(outputs_lines(channel)))
        lines.append("WHO ELSE WRITES HERE: " + " ".join(team_lines()))
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        logger.warning("SELF_MODEL_ONBOARDING_BLOCK_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return None


def history_label(message) -> str:
    """The speaker label for an outbound row in RECENT CONVERSATION."""
    if enabled() and getattr(message, "message_type", None) == "admin":
        return TEAM_LABEL
    return "Coach"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
