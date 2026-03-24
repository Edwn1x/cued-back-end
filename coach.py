import anthropic
from datetime import datetime, timezone
from pathlib import Path
import config
from models import get_session, User, Message, Workout, DailyLog

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# Load system prompt template
SYSTEM_PROMPT_TEMPLATE = Path("prompts/system_prompt.txt").read_text()


def build_context(user: User, message_type: str = "freeform") -> str:
    """Build the full system prompt with user context injected."""
    session = get_session()
    try:
        # Get recent conversation history
        recent_messages = (
            session.query(Message)
            .filter(Message.user_id == user.id)
            .order_by(Message.created_at.desc())
            .limit(config.CONVERSATION_HISTORY_LIMIT)
            .all()
        )
        recent_messages.reverse()  # chronological order

        conversation_history = "\n".join(
            f"{'Coach' if m.direction == 'out' else user.name}: {m.body}"
            for m in recent_messages
        ) or "No previous messages yet — this is the first interaction."

        # Get recent training history
        recent_workouts = (
            session.query(Workout)
            .filter(Workout.user_id == user.id)
            .order_by(Workout.date.desc())
            .limit(5)
            .all()
        )
        recent_workouts.reverse()

        if recent_workouts:
            training_lines = []
            for w in recent_workouts:
                date_str = w.date.strftime("%a %m/%d")
                exercises_str = ""
                if w.exercises:
                    exercises_str = ", ".join(
                        f"{e.get('name', '?')} {e.get('sets', '?')}x{e.get('reps', '?')} @{e.get('weight', '?')}lb"
                        for e in w.exercises
                    )
                notes = f" — User said: {w.user_notes}" if w.user_notes else ""
                training_lines.append(f"{date_str} ({w.workout_type}): {exercises_str}{notes}")
            training_history = "\n".join(training_lines)
        else:
            training_history = "No training history yet — this user is just getting started."

        now = datetime.now(timezone.utc)

        # Fill in the template
        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            user_profile=user.profile_summary,
            conversation_history=conversation_history,
            training_history=training_history,
            today=now.strftime("%A, %B %d, %Y"),
            current_time=now.strftime("%I:%M %p"),
            message_type=message_type,
        )

        return system_prompt

    finally:
        session.close()


def get_coach_response(user: User, user_message: str, message_type: str = "freeform") -> str:
    """Get an AI coaching response for a user's message."""
    system_prompt = build_context(user, message_type)

    response = client.messages.create(
        model=config.COACH_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_message}
        ],
    )

    return response.content[0].text


def generate_scheduled_message(user: User, message_type: str) -> str:
    """Generate a scheduled coaching message (morning briefing, meal, etc.)."""
    system_prompt = build_context(user, message_type)

    # For scheduled messages, the "user message" is a trigger instruction
    triggers = {
        "morning": (
            f"Generate the morning briefing for {user.name}. Greet them, ask about "
            f"sleep/energy, and preview today's training. Keep it to 2-3 sentences."
        ),
        "breakfast": (
            f"Suggest a breakfast for {user.name} based on their diet preferences "
            f"and today's training. Include rough calories and protein. Offer a swap option."
        ),
        "lunch": (
            f"Suggest a lunch for {user.name}. Check in briefly. Include calories/protein."
        ),
        "pre_workout": (
            f"Send a quick pre-workout reminder to {user.name}. Reference what they'll "
            f"be doing and any relevant notes from previous sessions."
        ),
        "workout": (
            f"Generate today's full workout for {user.name}. Include specific exercises, "
            f"sets, reps, and target weights. Format as a clean list."
        ),
        "post_workout": (
            f"Check in with {user.name} about how their workout went. Ask what weights "
            f"they hit. Keep it casual — one question."
        ),
        "dinner": (
            f"Suggest dinner for {user.name}. Include a rough daily macro summary "
            f"of where they're at. Keep it brief."
        ),
        "evening": (
            f"Send the evening wrap-up to {user.name}. Ask them to rate the day 1-5. "
            f"Give a brief preview of tomorrow. Keep it to 2-3 sentences max."
        ),
    }

    trigger = triggers.get(message_type, f"Send a coaching check-in to {user.name}.")

    response = client.messages.create(
        model=config.COACH_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=[
            {"role": "user", "content": trigger}
        ],
    )

    return response.content[0].text


def parse_workout_log(user: User, user_message: str) -> dict | None:
    """Use AI to parse a natural language workout report into structured data."""
    prompt = f"""Parse this workout report into JSON. Extract exercises with name, sets, reps, and weight.
If something is unclear, make your best guess. Return ONLY valid JSON, no other text.

Format: {{"exercises": [{{"name": "...", "sets": N, "reps": N, "weight": N}}, ...], "notes": "..."}}

User's report: "{user_message}"
"""
    try:
        response = client.messages.create(
            model=config.COACH_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        import json
        text = response.content[0].text.strip()
        # Clean potential markdown fences
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception:
        return None
