"""
Phase 2 — the single agent loop (inbound only).

One model call per inbound, one voice, full context. Replaces
classifier→specialists→merge behind SINGLE_AGENT_LOOP_ENABLED; the webhook falls
back to the legacy pipeline (orchestrator.route_message) on any exception.

Context is UNIFIED — all memory categories rendered (not the per-agent slice), so
a fact told in one domain is available in another (fixes failure 1). Safety
constraints stay universal via render_categories(include_safety_universal=True).
Model: claude-opus-4-8 (config.AGENT_LOOP_MODEL), no sampling params, adaptive
thinking + low effort held constant (Opus 4.8 rejects temperature/budget_tokens;
switching thinking modes would break the messages cache — see INVESTIGATION §5).
"""

from __future__ import annotations

import logging
import re
import os
from datetime import datetime, timezone

import anthropic
import config
from cost_tracking import track
from memory import render_categories, CATEGORIES, render_body_line, render_dietary_line
from models import get_session, User, Message, Workout, Meal, active
from events import todays_events
from split_pointer import get_split_pointer
from llm_client import make_client

logger = logging.getLogger("cued.agent_loop")
client = make_client()

_IDENTITY_PATH = os.path.join(os.path.dirname(__file__), "prompts", "identity.md")
_VOICE_PATH = os.path.join(os.path.dirname(__file__), "prompts", "voice.md")
_identity_cache: str | None = None
_voice_cache: str | None = None

_MEAL_ESTIMATION_PATH = os.path.join(os.path.dirname(__file__), "prompts", "meal_estimation.md")
_meal_estimation_cache: str | None = None

_MEAL_ROUTING_PATH = os.path.join(os.path.dirname(__file__), "prompts", "meal_routing.md")
_meal_routing_cache: str | None = None


def identity_prompt() -> str:
    """prompts/identity.md — the ONE identity (a friend at Berkeley who knows
    training and food cold). Loaded first on every surface: coach loop, heartbeat
    (via _voice_prompt) and onboarding (directly — it never gets the tool rules)."""
    global _identity_cache
    if _identity_cache is None:
        with open(_IDENTITY_PATH, "r", encoding="utf-8") as f:
            _identity_cache = f.read()
    return _identity_cache


def _voice_prompt() -> str:
    """identity.md + voice.md as ONE stable, cacheable prefix. The heartbeat
    composes its system from this same string, so both surfaces stay one person."""
    global _voice_cache
    if _voice_cache is None:
        with open(_VOICE_PATH, "r", encoding="utf-8") as f:
            _voice_cache = identity_prompt() + "\n\n---\n\n" + f.read()
    return _voice_cache


def _meal_estimation_prompt() -> str:
    global _meal_estimation_cache
    if _meal_estimation_cache is None:
        with open(_MEAL_ESTIMATION_PATH, "r", encoding="utf-8") as f:
            _meal_estimation_cache = f.read()
    return _meal_estimation_cache


def _meal_routing_prompt() -> str:
    global _meal_routing_cache
    if _meal_routing_cache is None:
        with open(_MEAL_ROUTING_PATH, "r", encoding="utf-8") as f:
            _meal_routing_cache = f.read()
    return _meal_routing_cache


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

    # Render every timestamp in the user's LOCAL zone (burn-in fix) — the model has no
    # local clock to reason "tomorrow/later" against otherwise, and bare UTC is 7h ahead.
    from timefmt import render_time, render_date, now_anchor, local_day_bounds, to_local
    _local = config.CONTEXT_LOCAL_TIME_ENABLED

    def _t(dt, *, relative=True):
        return render_time(dt, user, relative=relative) if _local else f"{dt:%H:%M}Z"

    def _d(dt):
        return render_date(dt, user) if _local else f"{dt:%m-%d}"

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

    # 3. Events, lifecycle-aware (memory-freshness Fix 1). Upcoming vs passed is a
    # FACT computed from the row's datetimes — the model must never infer it from a
    # timeless string (that's how a Jul 31 interview resurfaced Aug 3 as upcoming).
    from events import upcoming_events, recently_passed_events, event_end
    _now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    def _is_all_day(e):
        if not e.occurred_at or not e.ends_at:
            return False
        s_l, e_l = to_local(e.occurred_at, user), to_local(e.ends_at, user)
        return (s_l.hour, s_l.minute) == (0, 0) and (e_l.hour, e_l.minute) == (23, 59)

    # 3a. Today's events (local-day). Regex floor (went_to_gym / in_class) AND
    # model-logged dated schedule items (log_event) — the latter carry a description
    # in raw_text + a start/end window, so a timely check-in can reference them.
    evs = todays_events(user.id)
    if evs:
        def _fmt_event(e):
            if e.source == "model":
                label = (e.raw_text or e.event_type or "").strip()
                if _is_all_day(e):
                    span = " (all day)"
                elif e.occurred_at:
                    span = f" {_t(e.occurred_at, relative=False)}" + (
                        f"–{_t(e.ends_at, relative=False)}" if e.ends_at else "")
                elif e.ends_at:
                    span = f" until {_t(e.ends_at, relative=False)}"
                else:
                    span = ""
            else:
                label = e.event_type
                span = f" until {_t(e.ends_at, relative=False)}" if e.ends_at else ""
            # [id N] so the model can reference/correct/delete an event via manage_log,
            # the same way it can for meals and workouts.
            line = f"[id {e.id}] {label}{span}"
            # A same-day 2:15pm event at 6pm must not read like one at 9pm.
            if e.source == "model" and e.occurred_at and event_end(e) < _now_utc:
                line += (" — PASSED (already happened; never treat as upcoming, "
                         "at most one natural follow-up)")
            return line
        ev_txt = "; ".join(_fmt_event(e) for e in evs)
        parts.append(f"## TODAY'S EVENTS\n{ev_txt}")

    # 3b. Forward visibility — the reader gap: an event for Friday used to be
    # invisible until Friday, so "interview tomorrow, get some sleep" was impossible.
    ups = upcoming_events(user.id)
    if ups:
        def _fmt_upcoming(e):
            label = (e.raw_text or e.event_type or "").strip()
            when = _d(e.occurred_at)
            if not _is_all_day(e):
                when += f" {_t(e.occurred_at, relative=False)}"
            return f"[id {e.id}] {label} — {when}"
        parts.append("## UPCOMING EVENTS (next 7 days — logged ahead of time; you may "
                     "reference or prep them)\n"
                     + "\n".join(_fmt_upcoming(e) for e in ups))

    # 3c. Bounded follow-up window (48h), then the event retires. Once-ness comes
    # from the anti-repetition machinery (tick history / RECENT PROACTIVE), and
    # manage_log delete is the explicit retire path — no stored lifecycle state.
    passed = recently_passed_events(user.id)
    if passed:
        parts.append("## RECENTLY PASSED (happened, not followed up on yet — at most "
                     "ONE natural \"how'd it go\", then let it go; NEVER mention as "
                     "still upcoming)\n"
                     + "\n".join(f"[id {e.id}] {(e.raw_text or e.event_type or '').strip()}"
                                 f" — {_d(e.occurred_at)}" for e in passed))

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
            nl = "\n".join(f"- {_d(n.occurred_on)}: {n.text}" for n in notes)
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
        def _line(m):
            if m.direction == "out":
                return f"Coach: {m.body}"
            # Their iMessages carry a ref the react/reply tools can target.
            tag = f" [m{m.id}]" if (m.channel == "imessage" and m.provider_sid) else ""
            return f"{user.name}{tag}: {m.body}"
        window = "\n".join(_line(m) for m in msgs)
        parts.append(f"## RECENT CONVERSATION\n{window}")

    # 7. Today's logged meals (ACTIVE only) — with short IDs + macros. This is the
    # "read" for read-before-write: the model sees what's already logged and decides
    # log vs skip vs a legitimate second serving, instead of a deterministic dedup
    # silently dropping real calories. It also lets the model reference/correct an
    # entry by id via manage_log.
    _start, _end = local_day_bounds(user)   # ONE shared local-day window (timefmt)
    meals = (active(session, Meal, user_id=user.id)
             .filter(Meal.eaten_at >= _start, Meal.eaten_at < _end)
             .order_by(Meal.eaten_at).all())
    if meals:
        ml = "\n".join(
            f"[id {m.id}] {_t(m.eaten_at)} {m.description} — {m.calories or 0}cal/{m.protein_g or 0}g protein"
            for m in meals)
        parts.append("## TODAY'S LOGGED MEALS (already recorded — reference by id; do not "
                     "double-log the same serving; a genuine second serving is fine)\n" + ml)

    # Code-computed totals — the model must NOT re-add these (LLM arithmetic drifts).
    # Authoritative source: the SUM over the already-fetched active meals (soft-delete
    # filtered via active(), local-day windowed). ALWAYS rendered — even at 0 — so the
    # first meal of a new day reads against 0, and the model never falls back to a stale
    # running total from earlier in the thread (the live 2026-09-17/18 bug: yesterday's
    # total, still in RECENT CONVERSATION, got carried across midnight). Denormalized
    # user.calories_today counters exist for legacy use but deliberately do NOT reach here.
    tot_cal = sum(m.calories or 0 for m in meals)
    tot_pro = sum(m.protein_g or 0 for m in meals)
    tot_carb = sum(m.carbs_g or 0 for m in meals)
    tot_fat = sum(m.fat_g or 0 for m in meals)
    lines = [f"calories: {tot_cal} | protein: {tot_pro}g | carbs: {tot_carb}g | fat: {tot_fat}g"]
    if user.calorie_target:
        lines.append(f"calories remaining vs target: {user.calorie_target - tot_cal} "
                     f"({tot_cal}/{user.calorie_target})")
    if user.protein_target:
        lines.append(f"protein remaining vs target: {user.protein_target - tot_pro}g "
                     f"({tot_pro}/{user.protein_target}g)")
    parts.append(
        "## TODAY'S TOTALS (authoritative — the ONLY source for today's running total; "
        "it resets to 0 at local midnight)\n" + "\n".join(lines) +
        "\nQuote these numbers exactly. Do NOT re-add or re-derive them, and do NOT carry "
        "forward or add to any calorie/protein total you mentioned earlier in the thread — "
        "that number may be from a previous day.")

    # 8. Recent training log (active only).
    workouts = (active(session, Workout, user_id=user.id)
                .order_by(Workout.date.desc()).limit(5).all())
    if workouts:
        wl = "\n".join(f"[id {w.id}] {_d(w.date)} ({w.workout_type})" for w in workouts)
        parts.append(f"## RECENT WORKOUTS\n{wl}")

    # 8. Known gaps + follow-up permission.
    gaps = _known_gaps(user)
    if gaps:
        parts.append(
            "## KNOWN GAPS\nYou don't currently know: " + "; ".join(gaps) +
            ". If one matters to this reply, you may ask at most ONE follow-up."
        )

    # 9. Their profile page — a fact the code knows, so the coach never has to
    # "remember" it. Live 2026-09-11: "can you send me that link again to my
    # profile" → "i don't have a profile link to send you tbh" — the kickoff that
    # sent it had rolled to the edge of the history window. This is the ONE link
    # the coach may always send.
    if getattr(user, "phone", None):
        # profile_page.profile_url(user) is the single link builder (PR #34: a signed
        # token, the phone number never in the URL) — the same one the kickoff sends.
        from profile_page import profile_url as _profile_url
        _link = _profile_url(user)
        parts.append("## THEIR PROFILE PAGE\n"
                     f"{_link}\n"
                     "This is their own profile/settings page. When they ask for their profile, "
                     "their link, or want to double-check what you have on them, send this URL "
                     "(the one link you may always send). Never say you don't have it.")

    # Series §1.5: what they have at home (receipts / text), so dinner suggestions
    # prefer it. Flag-gated inside pantry_context.
    if (getattr(user, "onboarding_step", 0) or 0) >= 3:
        try:
            from receipts import pantry_context
            _pc = pantry_context(user.id)
            if _pc:
                parts.append(_pc)
        except Exception as e:  # noqa: BLE001
            logger.warning("PANTRY_CONTEXT_FAILED user=%s err=%s", user.id, e)

    # Adaptive targets: the weight trend, and today's cycle result if there is one.
    if (getattr(user, "onboarding_step", 0) or 0) >= 3:
        try:
            from adaptive_targets import weight_context, todays_adjustment_context
            for _blk in (weight_context(user, session), todays_adjustment_context(user, session)):
                if _blk:
                    parts.append(_blk)
        except Exception as e:  # noqa: BLE001
            logger.warning("ADAPTIVE_CONTEXT_FAILED user=%s err=%s", user.id, e)

    # Contextual reveals: capabilities they haven't touched yet (capabilities.py).
    # Post-onboarding only; the voice rule limits it to one clause when it fits.
    if (getattr(user, "onboarding_step", 0) or 0) >= 3:
        try:
            from capabilities import unused_context
            _uc = unused_context(user, session)
            if _uc:
                parts.append(_uc)
        except Exception as e:  # noqa: BLE001 — never break a turn over a hint
            logger.warning("UNUSED_CAPABILITIES_CONTEXT_FAILED user=%s err=%s", user.id, e)

    if _local:
        parts.append(f"## NOW\n{now_anchor(user)}")
    else:
        now = datetime.now(timezone.utc)
        parts.append(f"## NOW\n{now:%A %Y-%m-%d %H:%M}Z (resolve times against the user's "
                     f"timezone: {user.user_timezone or 'America/Los_Angeles'})")

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


_GYM_RE = re.compile(r"\b(gym|rsf|weight ?room|lift(?:ing)?|workout|train(?:ing)?|push|pull|legs|bench|squat|crowded|busy|packed|line)\b", re.I)


def _gym_mentioned(text: str, user) -> bool:
    if text and _GYM_RE.search(text):
        return True
    try:
        from workouts.session_ops import active_session_id
        return active_session_id(user.id) is not None
    except Exception:  # noqa: BLE001
        return False


def run_agent_loop(user, combined_body: str, message_type: str, image_data: dict = None,
                   message_id: str = None) -> str:
    """One agentic turn → the reply text. Raises only on genuine anomalies (caller
    falls back to legacy); truncation and the iteration bound degrade gracefully."""
    session = get_session()
    try:
        context = build_loop_context(user, session)
        # Series §2.4: when a workout is discussed or the gym comes up, the coach
        # gets the meter as ONE code line — it phrases it, never invents a number.
        if config.RSF_METER_ENABLED and _gym_mentioned(combined_body, user):
            try:
                import occupancy as _occ
                from gym_beats import rsf_context_block
                context += rsf_context_block(_occ.now())   # '' when no fresh reading
            except Exception as e:  # noqa: BLE001
                logger.warning("RSF_CONTEXT_FAILED user=%s err=%s", user.id, e)
    finally:
        session.close()

    voice = _voice_prompt()

    # Macro-accuracy Phase A: portion/label estimation guidance rides ONLY on turns
    # where the model can actually see an image — a separate block, never a voice.md
    # edit (voice.md is the heartbeat's shared cached prefix), appended after the
    # cache breakpoint so the voice prefix cache is unaffected.
    estimation = None
    if image_data and config.READ_IMAGE_ENABLED and config.MEAL_ESTIMATION_PROMPT_ENABLED:
        estimation = _meal_estimation_prompt()

    # Phase E routing (label → history → dining → USDA → web → ask) rides EVERY
    # reactive turn: meals arrive mostly as text and there's no pre-classifier. It's
    # stable text, so it extends the CACHED prefix — [voice, routing] — keeping the
    # per-turn cost at cache-read rates. Heartbeat composes its own system; unaffected.
    routing = _meal_routing_prompt() if config.MEAL_ROUTING_PROMPT_ENABLED else None

    if config.PROMPT_CACHING_ENABLED:
        system = [{"type": "text", "text": voice, "cache_control": {"type": "ephemeral"}}]
        if routing:
            system.append({"type": "text", "text": routing,
                           "cache_control": {"type": "ephemeral"}})
        system.append({"type": "text", "text": context})
        if estimation:
            system.append({"type": "text", "text": estimation})
    else:
        system = f"{voice}\n\n{routing}\n\n{context}" if routing else f"{voice}\n\n{context}"
        if estimation:
            system += f"\n\n{estimation}"

    if image_data and config.READ_IMAGE_ENABLED and config.RECEIPTS_ENABLED:
        # Series §1.2: a cheap pre-classifier. A receipt is itemized into the pantry
        # and answered in code — the meal path never sees it. meal/other → unchanged.
        from receipts import handle_receipt_image
        receipt_reply = handle_receipt_image(user.id, image_data)
        if receipt_reply is not None:
            return receipt_reply
    if image_data and config.READ_IMAGE_ENABLED:
        # The model sees the image and routes it in-call (food/calendar/whiteboard/
        # other) — no pre-classifier beyond the receipt check. Log the receipt as corpus.
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
    if config.SET_TARGETS_TOOL_ENABLED:
        from agent_tools import SET_TARGETS_TOOL
        tools.append(SET_TARGETS_TOOL)
    if config.LOG_WEIGHT_TOOL_ENABLED:
        from agent_tools import LOG_WEIGHT_TOOL
        tools.append(LOG_WEIGHT_TOOL)
    if config.SET_DAY_RESET_TOOL_ENABLED:
        from agent_tools import SET_DAY_RESET_TOOL
        tools.append(SET_DAY_RESET_TOOL)
    if config.START_WORKOUT_TOOL_ENABLED:
        from agent_tools import START_WORKOUT_SESSION_TOOL
        tools.append(START_WORKOUT_SESSION_TOOL)
    if config.LOG_EVENT_TOOL_ENABLED:
        from agent_tools import LOG_EVENT_TOOL
        tools.append(LOG_EVENT_TOOL)
    if config.GET_DINING_MENU_TOOL_ENABLED:
        from agent_tools import GET_DINING_MENU_TOOL
        tools.append(GET_DINING_MENU_TOOL)
    if config.MEAL_HISTORY_TOOL_ENABLED:
        from agent_tools import MATCH_MEAL_HISTORY_TOOL
        tools.append(MATCH_MEAL_HISTORY_TOOL)
    if config.DINING_MATCH_TOOL_ENABLED:
        from agent_tools import MATCH_DINING_ITEM_TOOL
        tools.append(MATCH_DINING_ITEM_TOOL)
    if config.USDA_LOOKUP_TOOL_ENABLED:
        from agent_tools import USDA_FOOD_LOOKUP_TOOL
        tools.append(USDA_FOOD_LOOKUP_TOOL)
    if config.IMESSAGE_REACTIONS_ENABLED:
        # Tapbacks + threaded replies exist only on iMessage: offered by the SAME
        # router the send uses (a tripped breaker = no tools), so an SMS user's
        # model never sees an affordance it can't deliver.
        from sms import _resolve_channel
        if _resolve_channel(user.id) == "imessage":
            from agent_tools import REACT_TOOL, THREAD_REPLY_TOOL
            tools.extend([REACT_TOOL, THREAD_REPLY_TOOL])
    if config.WEB_SEARCH_TOOL_ENABLED:
        # Server-side tool: Anthropic runs the search inline and returns results as
        # content blocks; no client handler. When-to-search + output/query hygiene
        # are prompt rules in identity.md / voice.md. One shared definition for
        # every surface lives in agent_tools (cap = WEB_SEARCH_MAX_USES per reply).
        from agent_tools import WEB_SEARCH_TOOL
        tools.append(WEB_SEARCH_TOOL)

    from agent_tools import begin_turn, peek_turn_state
    begin_turn(user.id)  # react/reply_in_thread record into this; the caller pops it

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
            track(user.id, "agent_loop.run", config.AGENT_LOOP_MODEL, resp)
        except Exception as e:  # cost telemetry must never break the reply
            logger.warning("AGENT_LOOP_COST_TRACK_FAILED user=%s err=%s", user.id, e)

        stop = getattr(resp, "stop_reason", None)
        block_types = [getattr(b, "type", None) for b in resp.content]

        # Every search the model issued this call, logged with the user id
        # (WEB_SEARCH_QUERY) — the line that shows what the coach reaches for.
        from agent_tools import log_web_search_queries
        log_web_search_queries(user.id, resp.content, "agent_loop.run")

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

        # Normal terminal turn (end_turn / stop_sequence) with a reply — unless the
        # reply is the reaction-only sentinel (or the "no text needed" note a model
        # drifts to): then the tapback WAS the reply and nothing is sent.
        from agent_tools import is_reaction_only_text, is_single_emoji_text
        state = peek_turn_state(user.id)
        if text and is_reaction_only_text(text, state.get("reacted")):
            logger.info("AGENT_LOOP_REACTION_ONLY user=%s iter=%d swallowed=%r", user.id, i, text[:40])
            return ""
        from agent_tools import leaked_tool_call
        leak = leaked_tool_call(text) if (text and tools) else None
        if leak and leak[0] == "react_to_message" and not state.get("reacted"):
            from agent_tools import latest_inbound_imessage_sid
            from sms import react_to_message, _resolve_channel
            sid = latest_inbound_imessage_sid(user.id) if _resolve_channel(user.id) == "imessage" else None
            if sid and react_to_message(user.id, sid, leak[1]):
                logger.warning("AGENT_LOOP_TOOL_CALL_IN_TEXT user=%s executed=%s %r", user.id, leak[0], leak[1])
                return ""
            logger.warning("AGENT_LOOP_TOOL_CALL_IN_TEXT user=%s dropped=%r", user.id, text[:60])
            return ""  # never text a tool name to the user
        if leak and leak[0] == "reply_in_thread":
            logger.warning("AGENT_LOOP_TOOL_CALL_IN_TEXT user=%s dropped=%r", user.id, text[:60])
            return ""
        if text and is_single_emoji_text(text) and tools and not state.get("reacted"):
            # A bare ❤️ as a TEXT on iMessage is a tapback that lost its way — send it
            # as the reaction on their latest message instead (never as a bubble).
            from agent_tools import latest_inbound_imessage_sid
            from sms import react_to_message, _resolve_channel
            sid = latest_inbound_imessage_sid(user.id) if _resolve_channel(user.id) == "imessage" else None
            if sid and react_to_message(user.id, sid, text.strip()):
                logger.info("AGENT_LOOP_EMOJI_TEXT_AS_REACTION user=%s emoji=%r", user.id, text.strip())
                return ""
        if text:
            return text

        # A reaction-only turn: the tapback WAS the reply. Empty text is the correct
        # outcome, not an anomaly — the caller sends nothing and clears the bubble.
        if state.get("reacted"):
            logger.info("AGENT_LOOP_REACTION_ONLY user=%s iter=%d", user.id, i)
            return ""

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
