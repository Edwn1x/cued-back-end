"""
Training Agent — Cued
======================
Specialist for workouts, exercise programming, progression, form,
injury accommodation, and workout logging.
Returns structured data that the personality layer turns into SMS responses.
"""

import json
import logging
import anthropic
import config
from models import get_session, User, Message, Workout
from skill_loader import load_skill
from models import is_workout_confirmed_today

logger = logging.getLogger("cued.training")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _build_training_context(user: User) -> str:
    """Build training-specific context — only what the training agent needs."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    session = get_session()
    try:
        # Recent messages for conversation continuity
        recent = (
            session.query(Message)
            .filter(Message.user_id == user.id)
            .order_by(Message.created_at.desc())
            .limit(6)
            .all()
        )
        recent.reverse()
        conversation = "\n".join(
            f"[{m.created_at.strftime('%b %d %I:%M %p')}] {'Coach' if m.direction == 'out' else user.name}: {m.body}"
            for m in recent
        ) or "(no recent messages)"

        # Recent workout history
        recent_workouts = (
            session.query(Workout)
            .filter(Workout.user_id == user.id)
            .order_by(Workout.date.desc())
            .limit(10)
            .all()
        )
        recent_workouts.reverse()

        if recent_workouts:
            workout_lines = []
            for w in recent_workouts:
                date_str = w.date.strftime("%a %b %d")
                exercises_str = ""
                if w.exercises:
                    exercises_str = ", ".join(
                        f"{e.get('name', '?')} {e.get('sets', '?')}x{e.get('reps', '?')} @{e.get('weight', '?')}lb"
                        for e in w.exercises
                    )
                notes = f" — {w.user_notes}" if w.user_notes else ""
                workout_lines.append(f"  {date_str} ({w.workout_type}): {exercises_str}{notes}")
            workout_history = "\n".join(workout_lines)
        else:
            workout_history = "  No workouts logged yet — this user hasn't trained with Cued yet."
    finally:
        session.close()

    # Workout confirmation status
    workout_confirmed = is_workout_confirmed_today(user.id)
    workout_status = (
        "User has CONFIRMED training today — you can reference the session."
        if workout_confirmed else
        "User has NOT confirmed training today. Do NOT assume they trained, do NOT ask how the workout went unless they mention it first."
    )

    # All confirmed decisions
    decisions = []
    if user.confirmed_goal_priority:
        decisions.append(f"Goal priority: {user.confirmed_goal_priority} (CONFIRMED)")
    if user.calorie_target:
        decisions.append(f"Daily calories: {user.calorie_target} cal (CONFIRMED)")
    if user.protein_target:
        decisions.append(f"Daily protein: {user.protein_target}g (CONFIRMED)")
    if user.confirmed_training_split:
        decisions.append(f"Training split: {user.confirmed_training_split} (CONFIRMED)")
    if user.confirmed_workout_time:
        decisions.append(f"Workout time: {user.confirmed_workout_time} (CONFIRMED)")
    if user.confirmed_training_days:
        decisions.append(f"Training days: {user.confirmed_training_days} (CONFIRMED)")
    if user.activity_level:
        decisions.append(f"Activity level: {user.activity_level} (CONFIRMED)")
    confirmed_block = "\n".join(decisions) if decisions else "No decisions confirmed yet."

    # Training-relevant profile
    profile = f"""Name: {user.name}
Age: {user.age or "unknown"}
Goal: {user.goal}
Experience: {user.experience or "unknown"}
Equipment: {user.equipment or "unknown"}
Injuries: {user.injuries or "none reported"}
Height: {f"{user.height_ft}'{user.height_in or 0}" if user.height_ft else "unknown"}
Weight: {f"{user.weight_lbs} lbs" if user.weight_lbs else "unknown"}"""

    memory_block = f"\n\n## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n{user.memory}" if user.memory else ""

    return (
        f"## USER PROFILE\n{profile}\n\n"
        f"## CONFIRMED DECISIONS (settled — do not re-ask or re-explain reasoning)\n{confirmed_block}\n\n"
        f"## TODAY'S TRAINING STATUS\n{workout_status}\n\n"
        f"## RECENT WORKOUT HISTORY\n{workout_history}"
        f"{memory_block}\n\n"
        f"## RECENT CONVERSATION\n{conversation}"
    )


def handle(user: User, user_message: str) -> dict:
    """
    Process a training-related message and return structured coaching content.

    Returns structured JSON that the personality layer turns into SMS.
    """
    personality = load_skill("personality")
    safety = load_skill("safety")
    training_skill = load_skill("training")
    context = _build_training_context(user)

    system_prompt = f"""{personality}

---

{safety}

---

{training_skill}

---

{context}

## YOUR TASK
You are the training specialist. The user sent a message related to workouts, exercises, programming, form, or progression. Analyze what they need and return STRUCTURED JSON. Another agent will turn your output into the actual SMS.

DO NOT write prose. DO NOT use first person. Return ONLY valid JSON.

Return this structure:
{{
  "intent": "brief label like workout_request, workout_log, form_question, progression_check, split_question, exercise_swap, deload_check",
  "content": {{
    // Fields relevant to this intent. Examples:
    // For workout_request: "workout_type", "exercises" (list of name/sets/reps/weight), "duration_estimate", "focus_note"
    // For workout_log: "exercises_logged", "observations", "progression_note"
    // For form_question: "exercise", "advice", "common_mistakes"
    // For progression_check: "exercise", "recent_numbers", "recommendation"
    // For exercise_swap: "original", "replacement", "reason"
  }},
  "clarifying_question": "a natural question if more info needed, or null",
  "coaching_note": "any additional coaching observation, or null",
  "log_action": "what should be logged to the workout DB, or null"
}}

Rules:
- Reference CONFIRMED DECISIONS as settled facts — do not re-explain why a split or schedule was chosen
- If the user asks for a workout, build it from their confirmed split, equipment, and experience level
- Reference workout history for progression — if they hit 155x8 last week, suggest 160x8 or 155x10
- Injuries listed in the profile mean automatic exercise modifications — don't wait for them to mention it
- If workout_time is confirmed, reference it naturally ("your 5pm session") but don't repeat the reasoning
- For workout logs, acknowledge what they hit and note any PRs or regressions
- Keep exercises practical for their equipment access
"""

    response = client.messages.create(
        model=config.COACH_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )

    text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    if "}" in text:
        text = text[:text.rindex("}") + 1]

    try:
        parsed = json.loads(text)
    except Exception as e:
        logger.error(f"Training agent returned invalid JSON: {text[:200]} — {e}")
        parsed = {
            "intent": "training_general",
            "content": {"note": "Unable to parse specialist output"},
            "clarifying_question": None,
            "coaching_note": None,
        }

    return {
        "agent": "training",
        "intent": parsed.get("intent", "training_general"),
        "content": parsed.get("content", {}),
        "clarifying_question": parsed.get("clarifying_question"),
        "log_action": parsed.get("log_action"),
    }
