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


def _training_day_status(user, now) -> str:
    """
    Return a human-readable string describing today's training status.
    Used to inject ground truth into the coaching context so reactive
    messages don't contradict the morning briefing.
    """
    import re
    days_str = (user.confirmed_training_days or user.workout_days or "").strip().lower()

    is_training = True  # default: unknown, assume training day
    next_training_label = ""

    if days_str and re.search(r'[a-z]', days_str):
        day_map = {
            "mon": 0, "monday": 0,
            "tue": 1, "tuesday": 1,
            "wed": 2, "wednesday": 2,
            "thu": 3, "thursday": 3,
            "fri": 4, "friday": 4,
            "sat": 5, "saturday": 5,
            "sun": 6, "sunday": 6,
        }
        today_num = now.weekday()
        tokens = re.split(r'[\s,/]+', days_str)
        day_nums = [day_map[t] for t in tokens if t in day_map]

        if day_nums:
            is_training = today_num in day_nums
            if not is_training:
                # Find next training day
                for offset in range(1, 8):
                    next_day = (today_num + offset) % 7
                    if next_day in day_nums:
                        next_day_name = [k for k, v in day_map.items() if v == next_day and len(k) > 3]
                        next_day_label = next_day_name[0].capitalize() if next_day_name else f"day+{offset}"
                        if user.workout_time:
                            next_training_label = f" (next training day: {next_day_label} at {user.workout_time})"
                        else:
                            next_training_label = f" (next training day: {next_day_label})"
                        break

    workout_time_str = f" at {user.workout_time}" if user.workout_time else ""
    split_str = f" — {user.current_split}" if getattr(user, "current_split", None) else ""

    if is_training:
        return f"TRAINING DAY{split_str}{workout_time_str}"
    else:
        return f"REST DAY{next_training_label}"


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

        # Last 3 outbound messages — used to prevent repetition across scheduled + reactive
        last_outbound = (
            session.query(Message)
            .filter(Message.user_id == user.id, Message.direction == "out")
            .order_by(Message.created_at.desc())
            .limit(3)
            .all()
        )
        last_outbound.reverse()

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

        # Build last-outbound block for repetition prevention (Fix 4)
        if last_outbound:
            outbound_lines = "\n".join(
                f"[{m.created_at.strftime('%b %d %I:%M %p')} — {m.message_type or 'freeform'}] {m.body}"
                for m in last_outbound
            )
            last_outbound_block = f"## LAST 3 MESSAGES YOU SENT\nThese are your most recent outbound messages. Do NOT repeat the same coaching point, exercise name, or nutrition note you already made in these messages. Vary the angle or stay silent on that topic.\n{outbound_lines}"
        else:
            last_outbound_block = ""

        # Build training day status (Fix 9)
        today_training_status = _training_day_status(user, now)

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

{last_outbound_block}

## RECENT CONVERSATION HISTORY
{conversation_history}

## TRAINING LOG (recent sessions)
{training_history}

## CURRENT CONTEXT
Today: {now.strftime("%A, %B %d, %Y")}
Time: ~{now.strftime("%I:%M %p")}
Today's training status: {today_training_status}
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
Planned workout time: {user.workout_time or "not set"}
Average daily steps: {f"{user.avg_steps:,}" if getattr(user, "avg_steps", None) else "not recorded"}
Current training split: {getattr(user, "current_split", None) or "not set — build from scratch if programming"}
{"DO NOT ask any questions in this message. Deliver value only — meal, workout, or brief encouragement." if (user.unanswered_count or 0) >= 2 else ""}
{"IMPORTANT: The user has NOT confirmed they trained today. Do NOT reference a completed session, do NOT ask them to rate it, do NOT say things like 'first day in the books'. If this is an evening wrap, just preview tomorrow." if not is_workout_confirmed_today(user.id) else "The user confirmed they trained today — you can reference the session."}

## HARD RULES — NEVER OVERRIDE THESE

Rule 0 — Confirmed nutrition targets are locked: {"The user's confirmed daily targets are " + str(user.calorie_target) + " cal and " + str(user.protein_target) + "g protein. NEVER recalculate, re-derive, or override these numbers from the user's profile data. Always reference these exact values. If you see a different number in the profile or macro breakdown, ignore it — the confirmed targets are the source of truth." if user.calorie_target and user.protein_target else "Nutrition targets not yet confirmed — compute from profile when needed and explain the derivation."}

Rule 1 — Time awareness: The current time is shown above. The user's planned workout time is also shown. NEVER ask how a session went, reference a completed workout, or say anything implying the user has already trained if the current time is before their planned workout time and they have not confirmed training today. Before the workout time: you can reference what's coming up. After the workout time: you can ask how it went. This is non-negotiable.

Rule 2 — Pull-based meals: NEVER suggest specific food, recipes, or meals unless the user explicitly asks. Trigger phrases that invite a suggestion: "what should I eat," "give me a meal idea," "help me hit my protein," "what's good for lunch." When the user mentions eating without asking for help, respond with something like "nice, text me what you go with and I'll log it" or "cool, let me know what you eat." Exception: if the user is significantly under their protein or calorie target late in the day, you may flag it as an offer — "you're at 80g protein with dinner left, want help hitting 146?" — but do not prescribe what to eat.

Rule 3 — No hallucinated causes: NEVER speculate about the cause of a problem unless the user told you the cause. If the user has a bad sleep schedule, do not guess why. Address it directly or ask. Wrong: "Your sleep's probably off from the holidays." Right: "That sleep window is rough — what's keeping you up that late?" Coach the problem. Do not invent a backstory for it.

Rule 4 — No patronizing defaults: NEVER use language that implies the user is a beginner regardless of their experience level. Banned phrases: "just the basics," "learn the movements," "keeping it simple," "nothing crazy," "ease into it," "start slow," "beginner-friendly," "we'll build up gradually," "get comfortable with." Describe workouts by what they target and why. If the user needs an explanation, they'll ask — and then you explain. Do not preemptively dumb things down.

Rule 5 — One accountability statement per topic per day: After giving daily calorie or protein totals, do NOT ask about the user's next meal or suggest they eat something. You've already addressed nutrition. Move on. If the user wants help with their next meal, they'll ask. One nutrition accountability statement per day max — do not circle back to it.

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


def get_coach_response(user: User, user_message: str, message_type: str = "freeform", image_data: dict = None) -> str:
    """Get an AI coaching response for a user's message. Supports image analysis."""
    system_prompt = build_context(user, message_type)

    # Build the message content
    if image_data:
        # User sent an image (MMS) — use Claude's vision with pre-downloaded base64 data
        content = []
        content.append(image_data)
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


def _build_pre_workout_trigger(user: User) -> str:
    """Build the pre-workout trigger, injecting today's morning briefing if it was sent."""
    session = get_session()
    try:
        from zoneinfo import ZoneInfo
        try:
            user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
        except Exception:
            user_tz = ZoneInfo("America/Los_Angeles")
        # Convert user's local midnight to naive UTC for DB comparison (DB stores naive UTC)
        from datetime import timezone as _tz
        local_midnight = datetime.now(user_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_utc = local_midnight.astimezone(_tz.utc).replace(tzinfo=None)

        morning_msg = (
            session.query(Message)
            .filter(
                Message.user_id == user.id,
                Message.direction == "out",
                Message.message_type == "morning_briefing",
                Message.created_at >= today_start_utc,
            )
            .order_by(Message.created_at.asc())
            .first()
        )
    finally:
        session.close()

    morning_context = ""
    if morning_msg:
        morning_context = (
            f"\nThis morning you already sent: \"{morning_msg.body}\"\n"
            f"Do NOT repeat any exercises, training cues, or content from that message."
        )

    return (
        f"Send a pre-workout message to {user.name}. Use --- to separate each message.\n"
        f"Message 1: A readiness check — energy level, hydration, recent meal. 1-2 sentences max.\n"
        f"Message 2 (optional): One motivational push specific to today's session. No coaching cues — just focus.\n"
        f"IMPORTANT: Do not name the exercises again or repeat anything from this morning's briefing. "
        f"This message is about readiness and mindset, not programming.{morning_context}"
    )


def generate_scheduled_message(user: User, message_type: str) -> str:
    """Generate a scheduled coaching message (morning briefing, meal, etc.)."""
    system_prompt = build_context(user, message_type)

    triggers = {
        "morning_briefing": (
            f"Generate the morning briefing for {user.name}. Use --- to separate each message.\n"
            f"Message 1: Short greeting (Gm or Morning) + what today's training session is and when.\n"
            f"Message 2 (optional): One thing to focus on mentally today.\n"
            f"IMPORTANT: Never suggest specific meals, foods, or recipes in this message. State the user's calorie and protein targets for the day and let them decide what to eat. Nutrition direction only — no food suggestions."
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
        "pre_workout": _build_pre_workout_trigger(user),
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
                f"Send the evening wrap to {user.name}. Maximum 2 messages — use --- to separate if needed.\n"
                f"Message 1 (required): Quick win or observation from today's training + ask them to rate the session 1-5. Keep it under 160 characters.\n"
                f"Message 2 (optional): One-line preview of tomorrow + goodnight. Under 100 characters. Only send if it adds something — otherwise fold it into message 1."
            ) if is_workout_confirmed_today(user.id) else (
                f"Send the evening wrap to {user.name}. One message only, no ---.\n"
                f"A brief encouraging thought + one-line preview of tomorrow + goodnight. No reference to training today. Under 200 characters total."
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