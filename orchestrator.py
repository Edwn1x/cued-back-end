"""
Orchestrator — Cued
====================
Entry point for processing inbound messages after the buffer flushes.
Classifies intent, routes to appropriate specialist agents, merges responses
through the personality layer.

Phase 1: Passthrough mode — classifies for observability but routes everything
to the legacy monolith. Lets us validate the classifier without changing behavior.
"""

import json
import logging
import threading
import anthropic
import config

logger = logging.getLogger("cued.orchestrator")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def classify_message(user_message: str, recent_context: str = "") -> dict:
    """
    Classify an incoming message to determine which agent(s) should handle it.
    Returns a dict like:
    {
      "primary_agent": "nutrition" | "training" | "readiness" | "personality",
      "secondary_agents": [],  // additional agents that should also respond
      "intent_type": "meal_report" | "meal_question" | "workout_request" | etc.,
      "confidence": "high" | "medium" | "low"
    }
    """
    prompt = f"""You are a message classifier for an SMS fitness coaching system. Your job is to determine which specialist agent(s) should respond to the user's message.

Available agents:
- nutrition: meals, food, macros, calories, what to eat, food photos, meal tracking, daily totals
- training: workouts, exercises, sets/reps/weights, form, programming, progression, deload, split questions
- readiness: sleep, recovery, energy, HRV, how tired, rest days, how hard to push
- personality: greetings, casual chat, emotional check-ins, venting, general questions, pushback, goodnight

Recent context (last few messages):
{recent_context if recent_context else "(no recent context)"}

User's latest message: "{user_message}"

Return ONLY valid JSON:
{{
  "primary_agent": "nutrition" | "training" | "readiness" | "personality",
  "secondary_agents": [],
  "intent_type": "brief description like meal_report, workout_log, casual_chat, etc.",
  "confidence": "high" | "medium" | "low"
}}

Rules:
- Default to "personality" for anything unclear, casual, or emotional
- Only pick a specialist when there's a clear domain signal
- "I'm tired" alone → personality (casual check-in). "I'm tired, should I skip today's workout?" → readiness
- "I had chicken and rice" → nutrition (meal report). "What should I eat?" → nutrition (meal question)
- "Did 155 on bench for 5" → training (workout log). "How's my bench progressing?" → training
- Multiple domains possible: "I ate chicken then did chest day" → primary nutrition, secondary training

Examples:
- "goodnight" → {{"primary_agent": "personality", "secondary_agents": [], "intent_type": "goodnight", "confidence": "high"}}
- "what should I eat for lunch" → {{"primary_agent": "nutrition", "secondary_agents": [], "intent_type": "meal_suggestion_request", "confidence": "high"}}
- "hit 185 on bench today" → {{"primary_agent": "training", "secondary_agents": [], "intent_type": "workout_log", "confidence": "high"}}
- "yeah idk" → {{"primary_agent": "personality", "secondary_agents": [], "intent_type": "unclear", "confidence": "low"}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        if "}" in text:
            text = text[:text.rindex("}") + 1]
        classification = json.loads(text)
        return classification
    except Exception as e:
        logger.error(f"Classification failed: {e}")
        return {
            "primary_agent": "personality",
            "secondary_agents": [],
            "intent_type": "unknown",
            "confidence": "low",
        }


def route_message(user, combined_body: str, message_type: str, image_url: str = None) -> str:
    """
    Classifies the message and routes to the appropriate agent path.

    Phase 2a: Nutrition messages go through the new pipeline.
    Everything else still goes to the legacy monolith.
    """
    from coach import get_coach_response
    from agents.nutrition import handle as nutrition_handle
    from agents.personality import write_response
    from models import get_session, Message

    # Get recent context for classification
    session = get_session()
    try:
        recent = (
            session.query(Message)
            .filter(Message.user_id == user.id)
            .order_by(Message.created_at.desc())
            .limit(5)
            .all()
        )
        recent.reverse()
        recent_context = "\n".join(
            f"{'Coach' if m.direction == 'out' else user.name}: {m.body}"
            for m in recent
        )
    finally:
        session.close()

    # Classify
    classification = classify_message(combined_body, recent_context)
    logger.info(f"Classified message from {user.name}: {classification}")

    primary = classification.get("primary_agent", "personality")
    confidence = classification.get("confidence", "low")

    # Route nutrition messages through the new pipeline
    if primary == "nutrition" and confidence in ("high", "medium"):
        logger.info(f"Routing to nutrition agent for {user.name}")
        try:
            structured = nutrition_handle(user, combined_body, image_url=image_url)
            response = write_response(user, structured, user_message=combined_body)
            # Fire meal extraction in background
            from agents.meal_extractor import extract_and_log_meal
            threading.Thread(
                target=extract_and_log_meal,
                args=(user.id, combined_body, response, recent_context),
                daemon=True,
            ).start()
            return response
        except Exception as e:
            logger.error(f"Nutrition pipeline failed, falling back to legacy: {e}")
            # Fall through to legacy on any error

    # Everything else: legacy monolith
    response = get_coach_response(user, combined_body, message_type, image_url=image_url)
    return response
