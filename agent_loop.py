"""
Phase 2 — the single agent loop (inbound only).

One Sonnet call per inbound, one voice, full context. Replaces
classifier→specialists→merge behind SINGLE_AGENT_LOOP_ENABLED; the webhook falls
back to the legacy pipeline (orchestrator.route_message) on any exception.

Context is UNIFIED — all memory categories rendered (not the per-agent slice), so
a fact told in one domain is available in another (fixes failure 1). Safety
constraints stay universal via render_categories(include_safety_universal=True).
Model: claude-sonnet-5 (config.AGENT_LOOP_MODEL), no sampling params, adaptive
thinking + low effort held constant (Sonnet 5 rejects temperature/budget_tokens;
switching thinking modes would break the messages cache — see INVESTIGATION §5).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import anthropic
import config
from cost_tracking import track
from memory import render_categories, CATEGORIES, render_body_line, render_dietary_line
from models import get_session, User, Message, Workout
from events import todays_events
from split_pointer import get_split_pointer

logger = logging.getLogger("cued.agent_loop")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

_VOICE_PATH = os.path.join(os.path.dirname(__file__), "prompts", "voice.md")
_voice_cache: str | None = None


def _voice_prompt() -> str:
    global _voice_cache
    if _voice_cache is None:
        with open(_VOICE_PATH, "r", encoding="utf-8") as f:
            _voice_cache = f.read()
    return _voice_cache


def _known_gaps(user) -> list[str]:
    """What the coach does NOT know — models ask good follow-ups when they see gaps."""
    gaps = []
    if not (user.confirmed_workout_time or user.workout_time):
        gaps.append("workout time is unconfirmed")
    if not get_split_pointer(user.id):
        gaps.append("no known split day yet — hasn't logged a workout")
    return gaps


def build_loop_context(user, session) -> str:
    """The VOLATILE per-user block, injected AFTER the cached voice prefix.

    UNIFIED memory render (all categories + universal safety) is the failure-1 fix:
    the single loop sees every domain's facts, not a per-agent slice.
    """
    profile = user.user_profile_memory or {}
    parts: list[str] = []

    # 1. Unified memory (ALL categories, safety appended universally).
    mem_text, _ids = render_categories(profile, CATEGORIES, include_safety_universal=True)
    if mem_text:
        parts.append(f"## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n{mem_text}")

    # 2. Typed-column profile (source of truth for body/diet/targets).
    if user.profile_summary:
        parts.append(f"## PROFILE\n{user.profile_summary}")
    body = render_body_line(user)
    diet = render_dietary_line(user)
    if body:
        parts.append(body)
    if diet:
        parts.append(diet)
    if (user.food_context or "").strip():
        parts.append(f"Food context: {user.food_context.strip()}")

    # 3. Today's events (local-day; from the Phase 1 Event floor).
    evs = todays_events(user.id)
    if evs:
        ev_txt = "; ".join(
            e.event_type + (f" until {e.ends_at:%H:%M}Z" if e.ends_at else "")
            for e in evs
        )
        parts.append(f"## TODAY'S EVENTS\n{ev_txt}")

    # 4. Split pointer WITH provenance — the model hedges on inferred days.
    p = get_split_pointer(user.id)
    if p:
        parts.append(
            f"## SPLIT POINTER\nlast completed: {p['day']} ({p['source']}). "
            f"Derive today's likely day from this and the split; if the source is "
            f"'inferred', hedge (ask/confirm) rather than assert."
        )

    # 5. Coaching summary + delivered points.
    if (user.coaching_summary or "").strip():
        parts.append(f"## COACHING SUMMARY\n{user.coaching_summary.strip()}")
    if (user.delivered_coaching_points or "").strip():
        parts.append(f"## ALREADY TOLD THEM (don't repeat)\n{user.delivered_coaching_points.strip()}")

    # 6. Recent conversation window (reuse the watermark boundary — no overlap with summary).
    watermark = user.last_compressed_message_id or 0
    msgs = (session.query(Message)
            .filter(Message.user_id == user.id, Message.id > watermark)
            .order_by(Message.created_at.desc())
            .limit(config.CONVERSATION_HISTORY_LIMIT)
            .all())
    msgs.reverse()
    if msgs:
        window = "\n".join(
            f"{'Coach' if m.direction == 'out' else user.name}: {m.body}" for m in msgs
        )
        parts.append(f"## RECENT CONVERSATION\n{window}")

    # 7. Recent training log.
    workouts = (session.query(Workout)
                .filter(Workout.user_id == user.id)
                .order_by(Workout.date.desc()).limit(5).all())
    if workouts:
        wl = "\n".join(f"{w.date:%m-%d} ({w.workout_type})" for w in workouts)
        parts.append(f"## RECENT WORKOUTS\n{wl}")

    # 8. Known gaps + follow-up permission.
    gaps = _known_gaps(user)
    if gaps:
        parts.append(
            "## KNOWN GAPS\nYou don't currently know: " + "; ".join(gaps) +
            ". If one matters to this reply, you may ask at most ONE follow-up."
        )

    now = datetime.now(timezone.utc)
    parts.append(f"## NOW\n{now:%A %Y-%m-%d %H:%M}Z (resolve times against the user's timezone: "
                 f"{user.user_timezone or 'America/Los_Angeles'})")

    return "\n\n".join(parts)


def run_agent_loop(user, combined_body: str, message_type: str, image_data: dict = None) -> str:
    """One model call → the reply text. Raises on failure (caller falls back to legacy)."""
    session = get_session()
    try:
        context = build_loop_context(user, session)
    finally:
        session.close()

    voice = _voice_prompt()
    if config.PROMPT_CACHING_ENABLED:
        system = [
            {"type": "text", "text": voice, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": context},
        ]
    else:
        system = f"{voice}\n\n{context}"

    if image_data:
        user_content = [image_data, {"type": "text", "text": combined_body or "(image)"}]
    else:
        user_content = combined_body

    resp = client.messages.create(
        model=config.AGENT_LOOP_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )

    try:
        track(user.id, "agent_loop.run", config.AGENT_LOOP_MODEL, resp.usage)
    except Exception as e:  # cost telemetry must never break the reply
        logger.warning("AGENT_LOOP_COST_TRACK_FAILED user=%s err=%s", user.id, e)

    # Adaptive thinking can put (empty) thinking blocks first — take the text block.
    text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
    if not text.strip():
        raise RuntimeError("agent loop returned no text block")
    return text
