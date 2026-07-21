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
from models import get_session, User, Message, Workout, Meal, active
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

    # 3. Today's events (local-day). Regex floor (went_to_gym / in_class) AND
    # model-logged dated schedule items (log_event) — the latter carry a description
    # in raw_text + a start/end window, so a timely check-in can reference them.
    evs = todays_events(user.id)
    if evs:
        def _fmt_event(e):
            if e.source == "model":
                label = (e.raw_text or e.event_type or "").strip()
                if e.occurred_at:
                    span = f" {e.occurred_at:%H:%M}Z" + (f"–{e.ends_at:%H:%M}Z" if e.ends_at else "")
                elif e.ends_at:
                    span = f" until {e.ends_at:%H:%M}Z"
                else:
                    span = ""
            else:
                label = e.event_type
                span = f" until {e.ends_at:%H:%M}Z" if e.ends_at else ""
            # [id N] so the model can reference/correct/delete an event via manage_log,
            # the same way it can for meals and workouts.
            return f"[id {e.id}] {label}{span}"
        ev_txt = "; ".join(_fmt_event(e) for e in evs)
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

    # 5b. Recent episodic notes (Phase 5) — non-fitness life context worth following
    # up on. The heartbeat's raw material ("how'd the midterm go"); the reactive loop
    # sees it too. Flag-gated; distinct ground from the coaching summary above.
    if config.EPISODIC_ENABLED:
        from episodic import recent_episodic
        notes = recent_episodic(user.id)
        if notes:
            nl = "\n".join(f"- {n.occurred_on:%m-%d}: {n.text}" for n in notes)
            parts.append(f"## RECENT LIFE CONTEXT (personal — follow up naturally)\n{nl}")

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

    # 7. Today's logged meals (ACTIVE only) — with short IDs + macros. This is the
    # "read" for read-before-write: the model sees what's already logged and decides
    # log vs skip vs a legitimate second serving, instead of a deterministic dedup
    # silently dropping real calories. It also lets the model reference/correct an
    # entry by id via manage_log.
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo as _ZI
    try:
        _tz = _ZI(user.user_timezone or "America/Los_Angeles")
    except Exception:
        _tz = _ZI("America/Los_Angeles")
    _mid = datetime.now(_tz).replace(hour=0, minute=0, second=0, microsecond=0)
    _start = _mid.astimezone(timezone.utc).replace(tzinfo=None)
    _end = (_mid + _td(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    meals = (active(session, Meal, user_id=user.id)
             .filter(Meal.eaten_at >= _start, Meal.eaten_at < _end)
             .order_by(Meal.eaten_at).all())
    if meals:
        ml = "\n".join(
            f"[id {m.id}] {m.eaten_at:%H:%M}Z {m.description} — {m.calories or 0}cal/{m.protein_g or 0}g protein"
            for m in meals)
        parts.append("## TODAY'S LOGGED MEALS (already recorded — reference by id; do not "
                     "double-log the same serving; a genuine second serving is fine)\n" + ml)

    # 8. Recent training log (active only).
    workouts = (active(session, Workout, user_id=user.id)
                .order_by(Workout.date.desc()).limit(5).all())
    if workouts:
        wl = "\n".join(f"[id {w.id}] {w.date:%m-%d} ({w.workout_type})" for w in workouts)
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


# Returned when the loop can't produce a clean reply but the turn's tool writes have
# already persisted — a neutral ack beats raising into legacy (which would re-answer and
# ignore the work done) or asking to resend (which would re-log). Rare with the raised cap.
_LOOP_DEGRADE_REPLY = "got it."


def _join_text(content) -> str:
    """The reply is EVERY text block in the response, concatenated in order — not just
    the first. A single Sonnet 5 response interleaves blocks: thinking, then text, then
    (for inline server-side web_search) server_tool_use + web_search_tool_result, then
    MORE text; citations likewise split the answer across multiple text blocks. Taking
    only the first text block sent a lead-in ("from what I can find:") and silently
    dropped the substance that came after the tool result (burn-in finding). Non-text
    blocks (thinking / tool_use / server_tool_use / *_tool_result) contribute nothing."""
    return "".join(
        b.text for b in content
        if getattr(b, "type", None) == "text" and getattr(b, "text", None)
    ).strip()


def run_agent_loop(user, combined_body: str, message_type: str, image_data: dict = None,
                   message_id: str = None) -> str:
    """One agentic turn → the reply text. Raises only on genuine anomalies (caller
    falls back to legacy); truncation and the iteration bound degrade gracefully."""
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

    if image_data and config.READ_IMAGE_ENABLED:
        # The model sees the image and routes it in-call (food/calendar/whiteboard/
        # other) — no pre-classifier. Log the receipt as corpus for schema tuning.
        logger.info("AGENT_LOOP_IMAGE user=%s — vision routing (read_image corpus marker)", user.id)
        user_content = [image_data, {"type": "text", "text": combined_body or "(the user sent an image)"}]
    elif image_data:
        # read_image off: don't send vision; note it so the coach can ask, never invent.
        user_content = ((combined_body or "")
                        + "\n[the user sent an image you can't see — ask them what it shows]")
    else:
        user_content = combined_body

    # Assemble the enabled tool set (each behind its own flag, added one at a time).
    tools = []
    if config.REMEMBER_TOOL_ENABLED:
        from agent_tools import REMEMBER_TOOL
        tools.append(REMEMBER_TOOL)
    if config.LOG_WORKOUT_TOOL_ENABLED:
        from agent_tools import LOG_WORKOUT_TOOL
        tools.append(LOG_WORKOUT_TOOL)
    if config.MANAGE_LOG_TOOL_ENABLED:
        from agent_tools import MANAGE_LOG_TOOL
        tools.append(MANAGE_LOG_TOOL)
    if config.LOG_MEAL_TOOL_ENABLED:
        from agent_tools import LOG_MEAL_TOOL
        tools.append(LOG_MEAL_TOOL)
    if config.LOG_EVENT_TOOL_ENABLED:
        from agent_tools import LOG_EVENT_TOOL
        tools.append(LOG_EVENT_TOOL)
    if config.GET_DINING_MENU_TOOL_ENABLED:
        from agent_tools import GET_DINING_MENU_TOOL
        tools.append(GET_DINING_MENU_TOOL)
    if config.WEB_SEARCH_TOOL_ENABLED:
        # Server-side tool: Anthropic runs the search inline and returns results as
        # content blocks; no client handler. Output/query hygiene are prompt rules
        # in voice.md (speak findings naturally, no links; no user PII in queries).
        tools.append({"type": "web_search_20260209", "name": "web_search",
                      "max_uses": config.WEB_SEARCH_MAX_USES})

    messages = [{"role": "user", "content": user_content}]
    last_text = ""
    for i in range(config.AGENT_LOOP_MAX_TOOL_ITERS):
        kwargs = dict(
            model=config.AGENT_LOOP_MODEL,
            max_tokens=config.AGENT_LOOP_MAX_TOKENS,  # room for thinking + tool JSON + reply
            thinking={"type": "adaptive"},  # held constant; switching modes breaks the messages cache
            output_config={"effort": "low"},
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        resp = client.messages.create(**kwargs)

        try:
            track(user.id, "agent_loop.run", config.AGENT_LOOP_MODEL, resp.usage)
        except Exception as e:  # cost telemetry must never break the reply
            logger.warning("AGENT_LOOP_COST_TRACK_FAILED user=%s err=%s", user.id, e)

        stop = getattr(resp, "stop_reason", None)
        block_types = [getattr(b, "type", None) for b in resp.content]

        # Server-side tool (web_search) hit its iteration limit — re-send to resume.
        if stop == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue

        # The model requested client tools: execute them (code-mediated), feed results back.
        if stop == "tool_use":
            from agent_tools import dispatch_tool
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    out = dispatch_tool(block.name, block.input, user.id, message_id=message_id)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
            messages.append({"role": "user", "content": results})
            continue

        # ALL text blocks, concatenated (adaptive thinking + inline web_search / citations
        # split the reply across several — see _join_text). This is the burn-in fix.
        text = _join_text(resp.content)
        if text:
            last_text = text

        # Truncation is its OWN branch: the output cap was hit (thinking + tools + reply
        # didn't fit). Log it BY NAME with the blocks we actually got — the observability
        # that "no text block" was missing — and degrade gracefully instead of raising
        # into fallback (which would re-answer via legacy and ignore any tool writes).
        if stop == "max_tokens":
            logger.warning("AGENT_LOOP_TRUNCATED user=%s iter=%d stop=max_tokens blocks=%s "
                           "max_tokens=%d — degrading, not raising",
                           user.id, i, block_types, config.AGENT_LOOP_MAX_TOKENS)
            return last_text or _LOOP_DEGRADE_REPLY

        # Safety refusal (Sonnet 5 classifiers, HTTP 200 + stop_reason=refusal): content
        # is empty pre-output or partial mid-stream. Don't raise into legacy — it would
        # re-answer the same declined request. Return what we have (or a neutral line).
        if stop == "refusal":
            logger.warning("AGENT_LOOP_REFUSAL user=%s iter=%d blocks=%s", user.id, i, block_types)
            return text or _LOOP_DEGRADE_REPLY

        # Normal terminal turn (end_turn / stop_sequence) with a reply.
        if text:
            return text

        # A terminal stop with no text is a genuine anomaly — log stop_reason + block
        # types (not a token-count guessing game) and fall back to legacy.
        logger.error("AGENT_LOOP_NO_TEXT user=%s iter=%d stop_reason=%s blocks=%s",
                     user.id, i, stop, block_types)
        raise RuntimeError(f"agent loop returned no text block (stop_reason={stop}, blocks={block_types})")

    # Iteration bound reached: the tools ran (writes persisted) but the model never
    # emitted a final reply. Degrade with what we have — never raise into fallback.
    logger.warning("AGENT_LOOP_MAX_ITERS user=%s iters=%d — returning best-effort reply",
                   user.id, config.AGENT_LOOP_MAX_TOOL_ITERS)
    return last_text or _LOOP_DEGRADE_REPLY
