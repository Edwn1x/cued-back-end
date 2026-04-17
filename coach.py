import anthropic
from datetime import datetime
from pathlib import Path
import config
from models import get_session, User, Message, Workout, DailyLog
from skill_loader import get_skills_for_message_type, get_all_skills
from engagement_tracker import get_tier
from models import is_workout_confirmed_today
from tone_analyzer import get_tone_instruction

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
            f"[{m.created_at.strftime('%b %d %I:%M %p')}] {'Coach' if m.direction == 'out' else user.name}: {m.body}"
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

        from zoneinfo import ZoneInfo
        try:
            user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
        except Exception:
            user_tz = ZoneInfo("America/Los_Angeles")
        now = datetime.now(user_tz)

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

        # Build adaptive tone instruction
        tone_instruction = get_tone_instruction(user)

        # Build memory block
        if user.memory:
            memory_block = f"## WHAT YOU REMEMBER ABOUT {user.name.upper()}\nThese are permanent facts you've learned about this user over time. Reference them naturally when relevant — but never list them out or make the user feel surveilled.\n{user.memory}"
        else:
            memory_block = "## WHAT YOU REMEMBER ABOUT THIS USER\nNothing accumulated yet — you're just getting to know them."

        # Build coaching summary block
        if user.coaching_summary:
            summary_block = f"## COACHING RELATIONSHIP SUMMARY\nWhat's happened so far in this coaching relationship:\n{user.coaching_summary}"
        else:
            summary_block = ""

        # Build confirmed decisions block
        decisions = []
        if user.confirmed_goal_priority:
            decisions.append(f"Goal priority: {user.confirmed_goal_priority} (CONFIRMED — do not re-ask)")
        if user.calorie_target:
            decisions.append(f"Daily calories: {user.calorie_target} cal (CONFIRMED — do not re-explain unless user asks)")
        if user.protein_target:
            decisions.append(f"Daily protein: {user.protein_target}g (CONFIRMED — do not re-explain unless user asks)")
        if user.confirmed_training_split:
            decisions.append(f"Training split: {user.confirmed_training_split} (CONFIRMED — do not suggest a different split)")
        if user.confirmed_workout_time:
            decisions.append(f"Workout time: {user.confirmed_workout_time} (CONFIRMED — do not assume a different time)")
        if user.confirmed_training_days:
            decisions.append(f"Training days: {user.confirmed_training_days} (CONFIRMED — do not re-ask)")

        confirmed_decisions = "\n".join(decisions) if decisions else "No decisions confirmed yet — collect information before making recommendations."

        # Assemble the full system prompt: skills + user context
        system_prompt = f"""{skills_content}

{tone_instruction}

---

## USER PROFILE
{user.profile_summary}

{memory_block}

{summary_block}

## CONFIRMED DECISIONS (treat these as settled facts — never re-ask or re-explain)
{confirmed_decisions}

## RECENT CONVERSATION HISTORY
{conversation_history}

## TRAINING LOG (recent sessions)
{training_history}

## CURRENT CONTEXT
Today: {now.strftime("%A, %B %d, %Y")}
Time: ~{now.strftime("%I:%M %p")}
Message type: {message_type}
Days since last workout: {days_since}
Engagement tier: {get_tier(user.unanswered_count or 0)} (unanswered streak: {user.unanswered_count or 0})
Food context (what they actually have/eat): {user.food_context if user.food_context else "Not collected yet — ask during onboarding or suggest generic options until known."}
Daily calorie target: {user.calorie_target if user.calorie_target else "Not set — compute from profile if needed."}
Daily protein target: {f"{user.protein_target}g" if user.protein_target else "Not set."}
Targets explained to user already: {bool(user.targets_explained)}
Pending clarification topic: {user.pending_clarification_topic or "none"}
Clarification answer received: {user.pending_clarification_answer or "not yet answered"}
{"ACTION REQUIRED: The user has answered the clarification question about '" + user.pending_clarification_topic + "'. Their answer: '" + user.pending_clarification_answer + "'. In your next relevant message, explicitly reference this answer to show you incorporated it. Do not treat it as background info — name it." if user.pending_clarification_topic and user.pending_clarification_answer else ""}
{"NOTE: You asked the user about '" + user.pending_clarification_topic + "' and have not received an answer yet. If this topic materially affects your current recommendation, acknowledge the gap: say you're using a safe default until they answer. Do not silently proceed as if you have the information." if user.pending_clarification_topic and not user.pending_clarification_answer else ""}
Workout confirmed today: {is_workout_confirmed_today(user.id)}
{"DO NOT ask any questions in this message. Deliver value only — meal, workout, or brief encouragement." if (user.unanswered_count or 0) >= 2 else ""}
{"IMPORTANT: The user has NOT confirmed they trained today. Do NOT reference a completed session, do NOT ask them to rate it, do NOT say things like 'first day in the books'. If this is an evening wrap, just preview tomorrow." if not is_workout_confirmed_today(user.id) else "The user confirmed they trained today — you can reference the session."}

## YOUR TASK
Respond to the user's latest message, or generate the scheduled touchpoint message. Be precise. Be useful. Be the coach that's impossible to ignore.

FORMAT RULES:
- Respond in 1-2 separate messages, each under 320 characters.
- If you need two messages, separate them with --- on its own line.
- First message = your main content or answer. Second message (optional) = supporting context or one follow-up question.
- If you can say it in one message, say it in one message. Two is the max. Never three.
- Do NOT end every message with "Reply W" or "Reply M" — only when actually offering a workout or meal right now.
- If the user is in conversation, just converse. Don't force a CTA.
- One idea per message. No bullet points — write like a person texting.
- Do not use --- for any other purpose.
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
            f"Message 2 (optional): One thing to focus on mentally today. No food — that's the breakfast message."
        ),
        "breakfast": (
            f"Suggest a specific breakfast for {user.name} based on their diet and today's training load. Use --- to separate each message.\n"
            f"Message 1: The breakfast suggestion with specific foods.\n"
            f"Message 2: Rough calories and protein count. Mention they can text you if they want a different option."
        ),
        "meal_suggestion": (
            f"Suggest a meal for {user.name}. Use --- to separate each message.\n"
            f"Message 1: The meal suggestion with specific foods.\n"
            f"Message 2: Calories/protein + running daily total. Mention they can text if they want something different."
        ),
        "pre_workout": (
            f"Send a pre-workout message to {user.name}. Use --- to separate each message.\n"
            f"Message 1: Their session is locked and ready — name the main lift or focus.\n"
            f"Message 2 (optional): One coaching note about today's focus. Let them know they can text when they're ready."
        ),
        "workout_request": (
            f"Generate today's full workout for {user.name}. Use --- to separate each message.\n"
            f"Message 1: The workout — exercises, sets, reps, target weights based on their history. Numbered list is fine here.\n"
            f"Message 2: Key coaching note on the main lift or what to watch for. Let them know to text you what they hit when they're done."
        ),
        "post_workout": (
            f"Check in with {user.name} after their session. One message only, no ---. "
            f"Ask what they hit. Casual, one line."
            if is_workout_confirmed_today(user.id) else
            f"Send a neutral check-in to {user.name}. One message only, no ---. "
            f"Ask if they made it to the gym today. No assumptions — they haven't confirmed."
        ),
        "evening_wrap": (
            (
                f"Send the evening wrap to {user.name}. Use --- to separate each message.\n"
                f"Message 1: Quick win or observation from today's training.\n"
                f"Message 2: Rate the session 1-5 + brief preview of tomorrow.\n"
                f"Message 3: Goodnight, one line."
            ) if is_workout_confirmed_today(user.id) else (
                f"Send the evening wrap to {user.name}. Use --- to separate each message.\n"
                f"Message 1: A brief encouraging thought — no reference to training today.\n"
                f"Message 2: Preview tomorrow briefly.\n"
                f"Message 3: Goodnight, one line. Do NOT ask them to rate today's session."
            )
        ),
        "nudge": (
            f"Send a single low-pressure re-engagement message to {user.name} who hasn't replied in a while. "
            f"One sentence. No questions. No guilt. Something like 'still here when you're ready' or a brief relevant tip. "
            f"Do not use ---. Do not mention how long they've been silent."
        ),
        "weigh_in": (
            f"Send a warm, brief weigh-in check-in to {user.name}. One message, no ---. "
            f"Ask for today's weight. Keep it casual. "
            f"Something like 'Morning check-in — what's the scale say today?' or 'It's weigh-in day. How's the scale looking?'"
        ),
        "adherence_gentle": (
            f"Send a gentle check-in to {user.name}. One message, no ---. "
            f"Ask how their eating has been — noticed they haven't logged meals today. "
            f"No guilt, no pressure. Example: 'Haven't heard about meals today — everything good?'"
        ),
        "adherence_firm": (
            f"Send a slightly firmer nudge to {user.name}. One message, no ---. "
            f"The tracking is the point. Be honest without shaming. "
            f"Example: 'Not tracking means I\\'m flying blind. Even a rough rundown of today helps — what\\'d you eat?'"
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