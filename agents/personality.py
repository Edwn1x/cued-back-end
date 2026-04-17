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

logger = logging.getLogger("cued.personality")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _load_personality_context(user) -> str:
    """Load the personality skill + user's communication style."""
    personality = load_skill("personality")
    safety = load_skill("safety")
    tone_instruction = get_tone_instruction(user)
    return f"{personality}\n\n---\n\n{safety}\n\n---\n\n{tone_instruction}"


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
    personality_context = _load_personality_context(user)
    instruction = _build_instruction(structured_input, user_message, user)

    system_prompt = f"""{personality_context}

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
"""

    response = client.messages.create(
        model=config.COACH_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": instruction}],
    )

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
    personality_context = _load_personality_context(user)

    system_prompt = f"""{personality_context}

## YOUR TASK
The user sent a casual or conversational message. Respond naturally in Cued's voice. Do NOT force coaching content if it doesn't fit. Sometimes the right response is a simple warm reply.

Recent conversation:
{recent_context if recent_context else "(no recent context)"}

FORMAT RULES:
- Usually 1 message, rarely 2.
- Short. Human. Text-like.
- Match the user's energy and length.
- Don't force Reply W/M shortcuts.
"""

    response = client.messages.create(
        model=config.COACH_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    return response.content[0].text
