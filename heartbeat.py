"""
Phase 4 — the heartbeat: a dumb clock and a smart decision. Per active user, on a
tick, guardrails run IN CODE first (a violating tick never reaches a model); a cheap
rules pre-gate resolves obvious silence; then one full-context decision call answers
"would a good coach say something right now, or stay silent?" Default silent.

Guardrails are limits, not behavior. The model composes freely when it speaks; it
never talks its way past a guardrail. Every tick logs its decision + reason — and the
recent tick history + today's outbound feed the next tick so the coach can't
re-conclude and re-send the same nudge (the anti-repetition signal, distinct from the
daily cap).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import anthropic
import config
from cost_tracking import track
from models import get_session, User, Message, HeartbeatTick, Workout, active
from sms import send_sms
from agent_loop import build_loop_context, _voice_prompt, _join_text

logger = logging.getLogger("cued.heartbeat")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

STAY_SILENT_TOOL = {
    "name": "stay_silent",
    "description": ("Call this to stay silent on this tick — a good coach mostly says "
                    "nothing. Pass a one-line reason (e.g. 'nothing new to add', "
                    "'already nudged about the skip today')."),
    "input_schema": {"type": "object",
                     "properties": {"reason": {"type": "string"}},
                     "required": ["reason"]},
}

# Speaking is a tool call too, NOT bare text. Burn-in finding: with only a
# stay_silent tool offered, a model that had DECIDED to speak still reflexively
# called stay_silent (reason: "actually should speak — but tool forces silence"),
# because "emit text" fought its strong prior to use an available tool. Making both
# outcomes explicit tools removes the trap — the model picks the action, it never
# has to route a speak decision through the silence channel. (Response-shape seam.)
SEND_TEXT_TOOL = {
    "name": "send_text",
    "description": ("Call this to send ONE proactive SMS to the user right now. Pass the "
                    "exact message to send in `message` — in your voice, nothing else, no "
                    "preamble. Use this whenever you've decided a text is warranted."),
    "input_schema": {"type": "object",
                     "properties": {"message": {"type": "string"}},
                     "required": ["message"]},
}

HEARTBEAT_PROMPT = """It's a quiet moment — a heartbeat tick, NOT a reply. The user did not just text you.

Decide: would a genuinely good friend-who-coaches text right now, or stay silent? DEFAULT SILENT — mostly you say nothing. But silence is the default, not the goal, and the question is NOT only "is something wrong?" — it's "would a real friend reach out here?" A friend calls out a slip AND marks a win, checks in on your week, passes along something that's actually relevant to you. You're a presence they're glad to hear from, not a tracker that only pings on failures.

A HEARTBEAT HAS NO NEW MESSAGE — that is what makes it proactive. Do NOT wait for something new to have happened before you speak. A STANDING CONDITION is a valid reason to text on its own: a days-long training gap, a broken pattern the user asked to be held to, an open thread still unanswered, a win worth marking. The longer a warranted nudge goes unsent, the MORE worth sending it is, not less. "Nothing new since the last tick" is NOT a reason to stay silent — that is true on every tick by design. Neither is "too early — better closer to the day" for a real upcoming event you haven't acknowledged: you cannot schedule a future text, so this tick is the one you have; the anti-repetition machinery already guarantees speaking now can't cost you a double-text later.

SPEAK when — accountability (the core wedge, non-negotiable):
- a multi-day skip or broken pattern for someone working toward consistency, or who asked to be called out / held accountable — an obvious yes

SPEAK when — warmth & presence (a friend, not a nag):
- a GENUINE win worth marking — a real, specific streak or goal hit (see MOMENTUM when present): "5 sessions this week, that's the consistency that actually sticks." Real progress, never participation-trophy praise.
- a RELEVANT bit of their world — something tied to their known goals, schedule, or interests that a friend would actually pass along. Grounded in what you remember about them.
- a TIMELY check-in — following up on something THEY mentioned (an event, a hard week, a stated intention): "how'd the summit go." BEFORE a known event counts the same: "how's the pitch prep going" / "good luck tomorrow." An upcoming real event you haven't acknowledged yet is check-in material NOW, not "closer to the day" — you cannot schedule a future text, so a deferred check-in is one that mostly never happens. Only where X is real and known.
- LEVITY or ease on a hard day — when the context shows a rough stretch, a friend lightens or backs off, doesn't pile on.

THE BAR FOR WARMTH IS HIGHER, NOT LOWER. A warranted accountability nudge is almost never unwelcome; generic warmth is exactly how proactive bots become insufferable ("hope you're having a great day!", "fun fact: bananas are berries"). So reach for a warm text ONLY when there is specific, real material about THIS person to make it land — their win, their event, their week, grounded in memory. If all you have is a generic pleasantry with nothing specific behind it, STAY SILENT. "How'd the summit go" (grounded in a known event) speaks; "hope your day's going well" (grounded in nothing) does not.

STAY SILENT when:
- a quiet, on-track day with nothing standing, no notable win, and no specific warm material — the honest default (a modest, unremarkable few workouts is NOT a win to text about)
- the user is mid-conversation (that's reactive territory — reply in-thread, don't proactively double-text)
- you already sent this thought, or recently decided to stay silent on it (see RECENT PROACTIVE MESSAGES / TICK HISTORY below when present) — never send the same nudge twice, and never open like your last few texts

DON'T RATION YOURSELF. The limits on over-texting are enforced in CODE, not by you: at most one unanswered proactive nudge at a time (an anti-stack window) and a hard daily cap. You cannot over-text past those. So do NOT hold back out of fear of nagging — that is already handled. Your only job is the single judgment call: is THIS worth a text right now? Answer that honestly and act on it.

Accountability is the job; fun is the delivery, not a substitute for it. One text. No preamble.

Call EXACTLY ONE tool:
- To SPEAK: call send_text with the exact SMS to send, in your voice — nothing else.
- To STAY SILENT: call stay_silent with a one-line reason.
If any part of your reasoning concludes a text is warranted, call send_text. NEVER call stay_silent and then say in the reason that you should have spoken — that is a contradiction; call send_text instead."""


def _naive_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _log_tick(user_id, spoke, reason, message=None, search=None):
    search = search or {}
    session = get_session()
    try:
        session.add(HeartbeatTick(user_id=user_id, spoke=spoke, reason=reason, message=message,
                                  search_available=search.get("available", False),
                                  search_used=search.get("used", False),
                                  search_query=search.get("query")))
        session.commit()
    finally:
        session.close()
    # stop= is the decide call's final stop_reason (None = no model call, e.g. a
    # guardrail tick) — it distinguishes a truncated tick from chosen silence.
    logger.info("HEARTBEAT_TICK user=%s spoke=%s reason=%s stop=%s search_available=%s search_used=%s",
                user_id, spoke, reason, search.get("stop"),
                search.get("available", False), search.get("used", False))


def _local_day_start_utc(user):
    try:
        tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


def guardrail_reason(user, session) -> str | None:
    """Return the first guardrail that blocks this tick, or None. Runs in code
    before any model call — the model can't talk past these."""
    # allowlist (burn-in: founder number only)
    if config.HEARTBEAT_ALLOWLIST and user.phone not in config.HEARTBEAT_ALLOWLIST:
        return "not_allowlisted"
    # quiet hours (goodnight / quiet_until, naive UTC)
    if user.quiet_until and _naive_utcnow() < user.quiet_until:
        return "quiet_hours"
    day_start = _local_day_start_utc(user)
    # daily cap on proactive messages
    spoke_today = (session.query(HeartbeatTick)
                   .filter(HeartbeatTick.user_id == user.id, HeartbeatTick.spoke.is_(True),
                           HeartbeatTick.decided_at >= day_start).count())
    if spoke_today >= config.HEARTBEAT_MAX_PER_DAY:
        return "daily_budget"
    # obvious-silence pre-gate: a recent inbound means an active conversation
    last_in = (session.query(Message)
               .filter(Message.user_id == user.id, Message.direction == "in")
               .order_by(Message.created_at.desc()).first())
    if last_in and last_in.created_at:
        age_min = (datetime.now(timezone.utc) - last_in.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
        if age_min < config.HEARTBEAT_ACTIVE_CONVO_MINUTES:
            return "active_conversation"
    # anti-STACK: don't pile a second proactive nudge on an unanswered first, but
    # ONLY within the window and ONLY when the most-recent outbound was itself a
    # proactive nudge. Clears on time-elapse (no user reply needed) — the fix for
    # the unanswered_gap deadlock, where any reactive reply or legacy briefing used
    # to mute all initiation until the user spoke. See rewrite/heartbeat-calibration.
    from engagement_tracker import has_unanswered_proactive
    if has_unanswered_proactive(user.id, config.HEARTBEAT_STACK_WINDOW_MINUTES):
        return "proactive_stack"
    # FLOOR events hard-gate (deterministic): don't interrupt someone who is provably
    # mid-class. Only the high-precision regex floor gates here; a MODEL-logged event
    # ("summit 12-2:30") deliberately does NOT hard-gate — it informs the decision call
    # via context instead (deterministic guardrails, model decisions). See in_class_now.
    from events import in_class_now
    if in_class_now(user.id):
        return "in_class"
    return None


def _search_available(user, session) -> bool:
    """Is web_search offered on this tick? Kill switch first; then the per-day
    budget — searched ticks (the model actually invoked search, i.e. spend) in the
    user's LOCAL day, derived from HeartbeatTick.search_used rather than a counter
    that could drift. Guardrail class: enforced here in code, before the tool ever
    reaches the model. At/over budget the tick still runs without the tool."""
    if not config.HEARTBEAT_WEB_SEARCH:
        return False
    from timefmt import local_day_bounds
    start, end = local_day_bounds(user)
    searched_today = (session.query(HeartbeatTick)
                      .filter(HeartbeatTick.user_id == user.id,
                              HeartbeatTick.search_used.is_(True),
                              HeartbeatTick.decided_at >= start,
                              HeartbeatTick.decided_at < end).count())
    return searched_today < config.HEARTBEAT_SEARCH_MAX_PER_DAY


def _recent_win_signal(user, session) -> str | None:
    """WARMTH material (Part 2), code-computed. Completed workouts in the last 7 days —
    the model can't be trusted to count this (date arithmetic; the playbook's 'the model
    quotes, code computes'), and RECENT WORKOUTS lists rows without a window total. This
    is the raw fact for a 'genuine win worth marking' text; the model judges whether the
    run is actually notable (a strong week) or thin (not a win — maybe an accountability
    signal instead). Neutral facts only — no 'great job', or code would be praising for
    the model. Returns None when there's nothing recent (clean-absent, no placeholder)."""
    cutoff = _naive_utcnow() - timedelta(days=7)
    done = (active(session, Workout, user_id=user.id)
            .filter(Workout.completed.is_(True), Workout.date >= cutoff)
            .order_by(Workout.date).all())
    if not done:
        return None
    types = ", ".join(w.workout_type or "workout" for w in done)
    return (f"## MOMENTUM (last 7 days — code-computed; a genuinely strong run may be worth "
            f"marking, a thin or falling-off one is NOT a win)\n"
            f"Completed {len(done)} workout(s) in the last 7 days: {types}.")


def _proactive_context(user, session) -> str:
    parts = [build_loop_context(user, session)]

    win = _recent_win_signal(user, session)
    if win:
        parts.append(win)

    last_out = (session.query(Message)
                .filter(Message.user_id == user.id, Message.direction == "out")
                .order_by(Message.created_at.desc()).first())
    if last_out and last_out.created_at:
        hrs = (datetime.now(timezone.utc) - last_out.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        parts.append(f"## TIME SINCE YOUR LAST MESSAGE\n~{hrs:.1f} hours")

    day_start = _local_day_start_utc(user)
    todays_out = (session.query(Message)
                  .filter(Message.user_id == user.id, Message.direction == "out",
                          Message.created_at >= day_start)
                  .order_by(Message.created_at.desc()).limit(6).all())

    ticks = (session.query(HeartbeatTick)
             .filter(HeartbeatTick.user_id == user.id)
             .order_by(HeartbeatTick.decided_at.desc())
             .limit(config.HEARTBEAT_RECENT_TICKS).all())

    # Invert the empty-history signal (bias #1): a quiet slate used to render as
    # NOTHING, and the model read that void as "no proof this isn't a duplicate →
    # stay cautious" — it wouldn't speak because it hadn't spoken. Empty history is
    # PERMISSION, not caution. Render it explicitly rather than omitting the blocks.
    if not todays_out and not ticks:
        parts.append("## PROACTIVE STATUS\n"
                     "You have sent NO proactive messages today and have no recent tick "
                     "decisions on file — you have NOT nudged about anything yet. The "
                     "absence of history is PERMISSION to speak if there's a real reason, "
                     "never a reason for caution: there is nothing here you could be "
                     "repeating.")
    else:
        if todays_out:
            parts.append("## RECENT PROACTIVE MESSAGES (today — do NOT repeat these)\n"
                         + "\n".join(f"- {m.body}" for m in reversed(todays_out)))
        if ticks:
            tl = "\n".join(
                f"- {'SPOKE' if t.spoke else 'silent'}: {t.message if t.spoke else t.reason}"
                for t in reversed(ticks))
            parts.append("## TICK HISTORY (your recent proactive decisions — don't re-send a thought)\n" + tl)

    return "\n\n".join(parts)


def decide(user_id: int) -> tuple[bool, str, dict]:
    """One decision call. Returns (spoke, payload, search): payload is the message if
    spoke, else the silence reason; search is the search DECISION for the tick record
    — {"available": offered-under-budget, "used": model-invoked-it, "query": first
    query or None}. The model calls send_text to speak or stay_silent to stay quiet
    (both outcomes are explicit tools — see SEND_TEXT_TOOL). Loads the user in its own
    session so callers can pass just an id (no detached instance)."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        context = _proactive_context(user, session)
        search = {"available": _search_available(user, session), "used": False, "query": None}
    finally:
        session.close()

    system = [
        {"type": "text", "text": _voice_prompt(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": HEARTBEAT_PROMPT + "\n\n" + context},
    ]
    tools = [SEND_TEXT_TOOL, STAY_SILENT_TOOL]
    if search["available"]:
        tools.append({"type": "web_search_20260209", "name": "web_search",
                      "max_uses": config.WEB_SEARCH_MAX_USES})

    messages = [{"role": "user", "content": "[heartbeat tick — decide: speak or stay silent]"}]
    for _ in range(config.AGENT_LOOP_MAX_TOOL_ITERS):
        # Dedicated ceiling, NOT MAX_RESPONSE_TOKENS: thinking + tool JSON + the
        # composed message all bill against this one budget (Aug 6 truncation).
        resp = client.messages.create(
            model=config.AGENT_LOOP_MODEL, max_tokens=config.HEARTBEAT_DECIDE_MAX_TOKENS,
            thinking={"type": "adaptive"}, output_config={"effort": "low"},
            system=system, messages=messages, tools=tools,
        )
        try:
            track(user_id, "heartbeat.decide", config.AGENT_LOOP_MODEL, resp)
        except Exception as e:
            logger.warning("HEARTBEAT_COST_TRACK_FAILED user=%s err=%s", user_id, e)

        stop = getattr(resp, "stop_reason", None)
        search["stop"] = stop  # surfaces in the HEARTBEAT_TICK log line

        # Search invocations arrive as inline server_tool_use blocks (executed
        # server-side), on any stop_reason — scan every response before branching.
        for b in resp.content:
            if (getattr(b, "type", None) in ("server_tool_use", "tool_use")
                    and getattr(b, "name", None) == "web_search"):
                search["used"] = True
                if search["query"] is None:
                    search["query"] = (getattr(b, "input", None) or {}).get("query")

        if stop == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if stop == "tool_use":
            # The two decision tools terminate the loop. send_text = speak (payload is
            # the message); stay_silent = quiet (payload is the reason).
            spoke_tool = next((b for b in resp.content
                               if getattr(b, "type", None) == "tool_use" and b.name == "send_text"), None)
            if spoke_tool is not None:
                msg = (spoke_tool.input or {}).get("message", "").strip()
                # send_text is terminal either way — an empty message can't be sent, so
                # log it as silence rather than continuing (which would leave this
                # tool_use unanswered and malform the next request).
                return (True, msg, search) if msg else (False, "send_text empty message", search)
            silent = next((b for b in resp.content
                           if getattr(b, "type", None) == "tool_use" and b.name == "stay_silent"), None)
            if silent is not None:
                return (False, (silent.input or {}).get("reason", "chose silence"), search)
            # a server tool (web_search) — feed nothing back for client tools; continue
            _decision_names = ("send_text", "stay_silent")
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": "n/a"}
                for b in resp.content
                if getattr(b, "type", None) == "tool_use" and b.name not in _decision_names
            ] or "continue"})
            continue

        # Truncation is its OWN outcome, checked BEFORE the bare-text fallback: a
        # max_tokens stop means the decision was cut off mid-generation — usually
        # zero text (thinking ate the budget), sometimes a partial compose. Neither
        # is chosen silence, and a partial must never reach the phone (a mid-sentence
        # SMS is the "glitchy connection" class). Log it by name so it can't hide as
        # a silent tick again; degrade to a labeled silent tick, don't raise.
        if stop == "max_tokens":
            block_types = [getattr(b, "type", None) for b in resp.content]
            logger.warning(
                "HEARTBEAT_TRUNCATED user=%s stop=max_tokens blocks=%s max_tokens=%d "
                "— decide cut off mid-generation; NOT chosen silence, nothing sent",
                user_id, block_types, config.HEARTBEAT_DECIDE_MAX_TOKENS)
            return (False, "truncated:max_tokens (decide hit the output cap "
                           "mid-generation — not chosen silence)", search)

        # Fallback: the model ended with bare text instead of calling send_text (rarer
        # now that speaking is an explicit tool, but kept for robustness). Concatenate
        # EVERY text block, not just the first — with adaptive thinking + inline
        # web_search the model splits the nudge across blocks (a lead-in before the
        # search result, the substance after). Same seam as agent_loop's _join_text.
        text = _join_text(resp.content)
        if text:
            return (True, text, search)
        # A clean terminal stop with neither a decision tool nor text is an anomaly
        # — log the response shape by name (the signal whose absence let the
        # truncation bug hide) before recording the silent tick.
        logger.warning("HEARTBEAT_NO_OUTPUT user=%s stop=%s blocks=%s",
                       user_id, stop, [getattr(b, "type", None) for b in resp.content])
        return (False, "no message composed", search)
    return (False, "decision loop exhausted", search)


def heartbeat_tick(user_id: int):
    """One tick for one user: guardrails -> decision -> log (+ send if speaking)."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not user.active:
            return
        reason = guardrail_reason(user, session)
        phone = user.phone
    finally:
        session.close()

    if reason:
        _log_tick(user_id, False, f"guardrail:{reason}")
        return

    spoke, payload, search = decide(user_id)
    if spoke:
        send_sms(phone, payload, user_id=user_id, message_type="heartbeat")
        _log_tick(user_id, True, "spoke", payload, search=search)
    else:
        _log_tick(user_id, False, payload, search=search)


def heartbeat_all():
    """Global tick: run a heartbeat for each active allowlisted user. Called by the
    scheduler on the (jittered) interval."""
    if not config.HEARTBEAT_ENABLED:
        return
    session = get_session()
    try:
        q = session.query(User).filter(User.active.is_(True))
        if config.HEARTBEAT_ALLOWLIST:
            q = q.filter(User.phone.in_(config.HEARTBEAT_ALLOWLIST))
        user_ids = [u.id for u in q.all()]
    finally:
        session.close()
    for uid in user_ids:
        try:
            heartbeat_tick(uid)
        except Exception as e:
            logger.error("HEARTBEAT_TICK_FAILED user=%s err=%s", uid, e, exc_info=True)
