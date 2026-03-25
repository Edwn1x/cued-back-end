import anthropic
from datetime import datetime, timezone
from pathlib import Path
import config
from models import get_session, User, Message, Workout, DailyLog
from skill_loader import get_skills_for_message_type, get_all_skills

client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

# Keep the old template as fallback
SYSTEM_PROMPT_TEMPLATE = Path("prompts/system_prompt.txt").read_text()


def build_context(user: User, message_type: str = "freeform") -> str:
    """Build the full system prompt with user context injected.
    
    Uses modular skills based on message_type:
    - workout_request, workout_log, post_workout -> personality + safety + training
    - meal_suggestion, meal_swap -> personality + safety + nutrition
    - morning_briefing -> personality + safety + nutrition + readiness
    - freeform, rating, evening_wrap -> personality + safety (all skills as fallback)
    """
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
        recent_messages.reverse()

        conversation_history = "\n".join(
            f"{'Coach' if m.direction == 'out' else user.name}: {m.body}"
            for m in recent_messages
        ) or "No previous messages yet -- this is the first interaction."

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
                notes = f" -- User said: {w.user_notes}" if w.user_notes else ""
                training_lines.append(f"{date_str} ({w.workout_type}): {exercises_str}{notes}")
            training_history = "\n".join(training_lines)
        else:
            training_history = "No training history yet -- this user is just getting started."

        now = datetime.now(timezone.utc)

        # Load the right skills for this message type
        if message_type in ("freeform", "rating", "evening_wrap"):
            skills_content = get_all_skills()
        else:
            skills_content = get_skills_for_message_type(message_type)

        # Calculate days since last workout
        if recent_workouts:
            last_workout_date = recent_workouts[-1].date
            days_since = (now.date() - last_workout_date).days if hasattr(last_workout_date, 'days') else "unknown"
        else:
            days_since = "N/A -- no workouts logged yet"

        # Assemble the full system prompt: skills + user context
        system_prompt = f"""{skills_content}

---

## USER PROFILE
{user.profile_summary}

## RECENT CONVERSATION HISTORY
{conversation_history}

## TRAINING LOG (recent sessions)
{training_history}

## CURRENT CONTEXT
Today: {now.strftime("%A, %B %d, %Y")}
Time: ~{now.strftime("%I:%M %p")}
Message type: {message_type}
Days since last workout: {days_since}

## YOUR TASK
Respond to the user's latest message, or generate the scheduled touchpoint message. Be precise. Be useful. Be the coach that's impossible to ignore. This is a text message -- if it wouldn't fit on a phone screen, it's too long.
"""
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

    triggers = {
        "morning_briefing": (
            f"Generate the morning briefing for {user.name}. "
            f"Open with a greeting (Gm or Morning). Reference sleep data if available. "
            f"Preview today's training. Suggest breakfast. Keep it to 2-3 texts max."
        ),
        "breakfast": (
            f"Suggest a breakfast for {user.name} based on their diet preferences "
            f"and today's training. Include rough calories and protein. Offer a swap option."
        ),
        "meal_suggestion": (
            f"Suggest a meal for {user.name}. Include calories/protein and a running daily total. "
            f"Offer a swap with 'Reply M for something else.'"
        ),
        "pre_workout": (
            f"Send a quick pre-workout message to {user.name}. Tell them their session is ready "
            f"and to reply W when they want it."
        ),
        "workout_request": (
            f"Generate today's full workout for {user.name}. Include specific exercises, "
            f"sets, reps, and target weights based on their training history. "
            f"Format as a clean numbered list. Add a note about key lifts."
        ),
        "post_workout": (
            f"Check in with {user.name} about how their session went. "
            f"Ask what they hit. Keep it casual -- one line."
        ),
        "evening_wrap": (
            f"Send the evening wrap to {user.name}. Ask them to rate the day 1-5. "
            f"Preview tomorrow briefly. Close with goodnight. 2-3 sentences max."
        ),
    }

    # Map old trigger names for backward compatibility
    trigger_map = {
        "morning": "morning_briefing",
        "lunch": "meal_suggestion",
        "dinner": "meal_suggestion",
        "workout": "workout_request",
        "evening": "evening_wrap",
    }
    mapped_type = trigger_map.get(message_type, message_type)
    trigger = triggers.get(mapped_type, f"Send a coaching check-in to {user.name}.")

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
        text = text.replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception:
        return None