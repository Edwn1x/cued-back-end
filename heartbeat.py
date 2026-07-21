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
from models import get_session, User, Message, HeartbeatTick
from sms import send_sms
from agent_loop import build_loop_context, _voice_prompt

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

HEARTBEAT_PROMPT = """It's a quiet moment — a heartbeat tick, NOT a reply. The user did not just text you.

Decide: would a genuinely good coach text right now, or stay silent? DEFAULT SILENT — a good coach mostly says nothing. Only speak if there's something real worth saying:
- a warm accountability nudge (they've skipped a pattern — "you've skipped twice this week, what's going on")
- a timely, personal follow-up on an open thread ("how'd the midterm go")
- a relevant, well-timed check-in tied to today's events or schedule

HARD RULES:
- If you already sent this thought recently, or recently decided to stay silent on it, STAY SILENT. Read RECENT PROACTIVE MESSAGES and TICK HISTORY below — never send the same nudge twice, and never open like your last few texts.
- Accountability is the job; fun is the delivery, not a substitute for it.
- One text. No preamble.

To STAY SILENT: call the stay_silent tool with a one-line reason.
To SPEAK: write the single SMS to send — nothing else, in your voice."""


def _naive_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _log_tick(user_id, spoke, reason, message=None):
    session = get_session()
    try:
        session.add(HeartbeatTick(user_id=user_id, spoke=spoke, reason=reason, message=message))
        session.commit()
    finally:
        session.close()
    logger.info("HEARTBEAT_TICK user=%s spoke=%s reason=%s", user_id, spoke, reason)


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
    # minimum gap after an unanswered outbound — don't pile on
    from engagement_tracker import has_unanswered_outbound
    if has_unanswered_outbound(user.id):
        return "unanswered_gap"
    # FLOOR events hard-gate (deterministic): don't interrupt someone who is provably
    # mid-class. Only the high-precision regex floor gates here; a MODEL-logged event
    # ("summit 12-2:30") deliberately does NOT hard-gate — it informs the decision call
    # via context instead (deterministic guardrails, model decisions). See in_class_now.
    from events import in_class_now
    if in_class_now(user.id):
        return "in_class"
    return None


def _proactive_context(user, session) -> str:
    parts = [build_loop_context(user, session)]

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
    if todays_out:
        parts.append("## RECENT PROACTIVE MESSAGES (today — do NOT repeat these)\n"
                     + "\n".join(f"- {m.body}" for m in reversed(todays_out)))

    ticks = (session.query(HeartbeatTick)
             .filter(HeartbeatTick.user_id == user.id)
             .order_by(HeartbeatTick.decided_at.desc())
             .limit(config.HEARTBEAT_RECENT_TICKS).all())
    if ticks:
        tl = "\n".join(
            f"- {'SPOKE' if t.spoke else 'silent'}: {t.message if t.spoke else t.reason}"
            for t in reversed(ticks))
        parts.append("## TICK HISTORY (your recent proactive decisions — don't re-send a thought)\n" + tl)

    return "\n\n".join(parts)


def decide(user_id: int) -> tuple[bool, str]:
    """One decision call. Returns (spoke, payload): payload is the message if spoke,
    else the silence reason. The model calls stay_silent to stay quiet. Loads the
    user in its own session so callers can pass just an id (no detached instance)."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        context = _proactive_context(user, session)
    finally:
        session.close()

    system = [
        {"type": "text", "text": _voice_prompt(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": HEARTBEAT_PROMPT + "\n\n" + context},
    ]
    tools = [STAY_SILENT_TOOL]
    if config.HEARTBEAT_WEB_SEARCH:
        tools.append({"type": "web_search_20260209", "name": "web_search",
                      "max_uses": config.WEB_SEARCH_MAX_USES})

    messages = [{"role": "user", "content": "[heartbeat tick — decide: speak or stay silent]"}]
    for _ in range(config.AGENT_LOOP_MAX_TOOL_ITERS):
        resp = client.messages.create(
            model=config.AGENT_LOOP_MODEL, max_tokens=config.MAX_RESPONSE_TOKENS,
            thinking={"type": "adaptive"}, output_config={"effort": "low"},
            system=system, messages=messages, tools=tools,
        )
        try:
            track(user_id, "heartbeat.decide", config.AGENT_LOOP_MODEL, resp.usage)
        except Exception as e:
            logger.warning("HEARTBEAT_COST_TRACK_FAILED user=%s err=%s", user_id, e)

        if getattr(resp, "stop_reason", None) == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if getattr(resp, "stop_reason", None) == "tool_use":
            silent = next((b for b in resp.content
                           if getattr(b, "type", None) == "tool_use" and b.name == "stay_silent"), None)
            if silent is not None:
                return (False, (silent.input or {}).get("reason", "chose silence"))
            # a server tool (web_search) — feed nothing back for client tools; continue
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": "n/a"}
                for b in resp.content if getattr(b, "type", None) == "tool_use" and b.name != "stay_silent"
            ] or "continue"})
            continue

        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        if text.strip():
            return (True, text.strip())
        return (False, "no message composed")
    return (False, "decision loop exhausted")


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

    spoke, payload = decide(user_id)
    if spoke:
        send_sms(phone, payload, user_id=user_id, message_type="heartbeat")
        _log_tick(user_id, True, "spoke", payload)
    else:
        _log_tick(user_id, False, payload)


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
