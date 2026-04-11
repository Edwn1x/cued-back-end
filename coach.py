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
Respond to the user's latest message, or generate the scheduled touchpoint message. Be precise. Be useful. Be the coach that's impossible to ignore.

FORMAT RULES:
- Respond in 2-3 separate messages, each under 320 characters.
- Separate each message with --- on its own line.
- Structure: first message = main content (the meal, workout, or key info), second message = context (daily totals, timing, rationale), third message (optional) = CTA or question ("Reply W for your workout", "How'd that feel?").
- One idea per message. Never cram greeting + training + food into one block.
- No bullet points in scheduled messages — write like a person texting, not a newsletter.
- Do not use --- for any other purpose (not for section breaks, not for emphasis).
"""
        return system_prompt

    finally:
        session.close()


def get_coach_response(user: User, user_message: str, message_type: str = "freeform", image_url: str = None) -> str:
    """Get an AI coaching response for a user's message. Supports image analysis."""
    system_prompt = build_context(user, message_type)

    # Build the message content
    if image_url:
        # User sent an image (MMS) — use Claude's vision
        content = []
        content.append({
            "type": "image",
            "source": {"type": "url", "url": image_url}
        })
        if user_message:
            content.append({"type": "text", "text": user_message})
        else:
            if message_type == "food_photo":
                content.append({"type": "text", "text": "The user sent a photo of their food. Estimate the calories, protein, and macros. Be specific but brief. Add it to their daily running total."})
            elif message_type == "progress_photo":
                content.append({"type": "text", "text": "The user sent a progress photo. Comment on what you see — be honest, specific, and encouraging without being fake."})
            else:
                content.append({"type": "text", "text": "The user sent an image. If it's food, estimate macros. If it's a gym photo, comment on form or setup. If it's a supplement label, give your take."})

        messages = [{"role": "user", "content": content}]
    else:
        messages = [{"role": "user", "content": user_message}]

    response = client.messages.create(
        model=config.COACH_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=messages,
    )

    return response.content[0].text


def generate_scheduled_message(user: User, message_type: str) -> str:
    """Generate a scheduled coaching message (morning briefing, meal, etc.)."""
    system_prompt = build_context(user, message_type)

    triggers = {
        "morning_briefing": (
            f"Generate the morning briefing for {user.name}. Use --- to separate each message.\n"
            f"Message 1: Short greeting (Gm or Morning) + what today's training session is and when.\n"
            f"Message 2: One thing to focus on mentally today. No food — that's the breakfast message.\n"
            f"Message 3 (optional): Brief CTA like 'Reply W when you're ready for your workout.'"
        ),
        "breakfast": (
            f"Suggest a specific breakfast for {user.name} based on their diet and today's training load. Use --- to separate each message.\n"
            f"Message 1: The breakfast suggestion with specific foods.\n"
            f"Message 2: Rough calories and protein count.\n"
            f"Message 3: 'Reply B for a different option.'"
        ),
        "meal_suggestion": (
            f"Suggest a meal for {user.name}. Use --- to separate each message.\n"
            f"Message 1: The meal suggestion with specific foods.\n"
            f"Message 2: Calories/protein + running daily total.\n"
            f"Message 3: 'Reply M for something else.'"
        ),
        "pre_workout": (
            f"Send a pre-workout message to {user.name}. Use --- to separate each message.\n"
            f"Message 1: Their session is locked and ready — name the main lift or focus.\n"
            f"Message 2: 'Reply W when you're ready and I'll send the full plan.'"
        ),
        "workout_request": (
            f"Generate today's full workout for {user.name}. Use --- to separate each message.\n"
            f"Message 1: The workout — exercises, sets, reps, target weights based on their history. Numbered list is fine here.\n"
            f"Message 2: Key coaching note on the main lift or what to watch for.\n"
            f"Message 3: 'Log it when you're done — just text me what you hit.'"
        ),
        "post_workout": (
            f"Check in with {user.name} after their session. One message only, no ---. "
            f"Ask what they hit. Casual, one line."
        ),
        "evening_wrap": (
            f"Send the evening wrap to {user.name}. Use --- to separate each message.\n"
            f"Message 1: Quick win or observation from today.\n"
            f"Message 2: Rate the day 1-5 + brief preview of tomorrow.\n"
            f"Message 3: Goodnight, one line."
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