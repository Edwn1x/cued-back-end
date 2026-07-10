"""
Personality Agent — Cued
=========================
The voice of Cued. Takes structured output from specialist agents and turns
it into SMS messages with the right tone, slang, and format rules.

Also handles pure conversational messages (greetings, check-ins, casual chat)
where no specialist agent is needed.
"""

import logging
import anthropic
import config
from skill_loader import load_skill
from tone_analyzer import get_tone_instruction
from cost_tracking import track

logger = logging.getLogger("cued.personality")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _personality_skills_block() -> str:
    """The cacheable static prefix: personality + safety skills.
    Returns the same bytes for every user. Tone instruction is NOT included
    here — it's per-user, so it goes in block2 instead."""
    personality = load_skill("personality")
    safety = load_skill("safety")
    return f"{personality}\n\n---\n\n{safety}"


def _personality_tone_block(user) -> str:
    """The per-user dynamic suffix — just the tone instruction."""
    return get_tone_instruction(user)


def write_response(user, structured_input: dict, user_message: str = "") -> str:
    """
    Take structured coaching content from a specialist agent and write
    it as an SMS response in Cued's voice.

    structured_input format:
    {
        "agent": "nutrition" | "training" | "readiness" | "personality",
        "intent": "what the user asked for",
        "content": {
            // agent-specific structured data
        },
        "clarifying_question": "optional question to ask user" or None,
        "log_action": "what was logged to the DB, if anything" or None,
    }
    """
    instruction = _build_instruction(structured_input, user_message, user)

    # block1_cacheable: personality + safety + YOUR TASK + FORMAT RULES + APOLOGY LIMIT.
    # Static across all users — cross-user cache hits.
    block1_cacheable = f"""{_personality_skills_block()}

---

## YOUR TASK
You are the voice of Cued. A specialist agent has analyzed the user's message and returned structured coaching content. Your job is to turn that content into an SMS response in Cued's voice.

DO NOT change the coaching substance. The specialist agent has already decided what to communicate — your job is HOW to say it.

FORMAT RULES:
- Respond in 1-2 separate messages, each under 320 characters.
- If you need two messages, separate them with --- on its own line.
- First message = main content. Second message (optional) = one follow-up question or supporting note.
- If you can say it in one message, say it in one message. Two is the max. Never three.
- Write like a real person texting, not a newsletter.
- Use the personality skill's tone guidelines — dry, specific, confident, occasionally warm.
- Do NOT end every message with "Reply W" or "Reply M" — only include shortcuts when the specialist's content genuinely offers a workout or meal right now.

APOLOGY LIMIT:
- "my bad" max twice per conversation. If the specialist content implies an error, use "actually" or "nah let me fix that" instead of apologizing again.
- Never "I'm sorry" or "I apologize" — just correct and move on.
"""

    # block2_tail: per-user tone instruction.
    block2_tail = _personality_tone_block(user)

    if config.PROMPT_CACHING_ENABLED:
        system_arg = [
            {"type": "text", "text": block1_cacheable,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": block2_tail},
        ]
    else:
        system_arg = f"{block1_cacheable}\n\n{block2_tail}"

    response = client.messages.create(
        model=config.COACH_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        system=system_arg,
        messages=[{"role": "user", "content": instruction}],
    )
    track(getattr(user, "id", None), "personality.write_response",
          config.COACH_MODEL, response.usage)

    return response.content[0].text


def _build_instruction(structured_input: dict, user_message: str, user) -> str:
    """Build the instruction prompt for the personality agent based on specialist output."""
    agent = structured_input.get("agent", "personality")
    intent = structured_input.get("intent", "")
    content = structured_input.get("content", {})
    clarifying_question = structured_input.get("clarifying_question")
    log_action = structured_input.get("log_action")

    parts = [
        f"The user said: \"{user_message}\"" if user_message else "",
        f"\nSpecialist agent: {agent}",
        f"Intent: {intent}",
        f"\nStructured coaching content to communicate:\n{_format_content(content)}",
    ]

    if "reply_draft" in content:
        parts.append(
            "\nThe specialist already drafted a near-final reply (above). Keep its exact food "
            "items and macro numbers — do not invent, drop, or alter any dish or number. Just "
            "tighten it into Cued's voice and SMS format (strip any markdown like ** or bullets)."
        )

    if log_action:
        parts.append(f"\nAction taken in the background: {log_action}")
        parts.append("Mention this subtly if relevant — don't make it feel like a system notification.")

    if clarifying_question:
        parts.append(f"\nAsk this clarifying question (naturally, in your voice): {clarifying_question}")

    parts.append("\nWrite the SMS response now. One or two messages max.")

    return "\n".join(p for p in parts if p)


def _format_content(content: dict) -> str:
    """Format structured content dict as readable text for the instruction."""
    if not content:
        return "(no specific content)"
    lines = []
    for key, value in content.items():
        lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def handle_casual_message(user, user_message: str, recent_context: str = "") -> str:
    """
    Handle messages that don't need a specialist — greetings, check-ins,
    casual chat, emotional messages. Just writes a natural response in Cued's voice.
    """
    # block1_cacheable: personality + safety + YOUR TASK + FORMAT RULES.
    # Independent from write_response's block1 — different YOUR TASK, so
    # they cache as separate keys (per critique guidance: don't try to share).
    block1_cacheable = f"""{_personality_skills_block()}

---

## YOUR TASK
The user sent a casual or conversational message. Respond naturally in Cued's voice. Do NOT force coaching content if it doesn't fit. Sometimes the right response is a simple warm reply.

FORMAT RULES:
- Usually 1 message, rarely 2.
- Short. Human. Text-like.
- Match the user's energy and length.
- Don't force Reply W/M shortcuts.
"""

    # block2_tail: per-user tone + per-turn recent conversation.
    block2_tail = f"""{_personality_tone_block(user)}

Recent conversation:
{recent_context if recent_context else "(no recent context)"}
"""

    if config.PROMPT_CACHING_ENABLED:
        system_arg = [
            {"type": "text", "text": block1_cacheable,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": block2_tail},
        ]
    else:
        system_arg = f"{block1_cacheable}\n\n{block2_tail}"

    response = client.messages.create(
        model=config.COACH_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        system=system_arg,
        messages=[{"role": "user", "content": user_message}],
    )
    track(getattr(user, "id", None), "personality.handle_casual_message",
          config.COACH_MODEL, response.usage)

    return response.content[0].text
