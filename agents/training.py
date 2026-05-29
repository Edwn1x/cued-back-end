"""
Training Agent — Cued
======================
Specialist for workouts, exercise programming, progression, form,
injury accommodation, and workout logging.
Returns structured data that the personality layer turns into SMS responses.
"""

import json
import logging
import re
import threading
import anthropic
import config
from models import get_session, User, Message, Workout
from skill_loader import load_skill
from models import is_workout_confirmed_today

logger = logging.getLogger("cued.training")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

EXERCISE_DEMO_LINKS = {
    "machine_pec_deck": "https://youtu.be/S6rqpxVGKZ4?t=262",
    "weighted_dips": "https://youtu.be/S6rqpxVGKZ4?t=340",
    "bench_press": "https://youtu.be/S6rqpxVGKZ4?t=592",
    "overhead_cable_triceps_extension": "https://youtu.be/S6rqpxVGKZ4?t=872",
    "incline_bench_press": "https://youtu.be/S6rqpxVGKZ4?t=1109",
    "machine_lat_pullover": "https://youtu.be/S6rqpxVGKZ4?t=38",
    "bayesian_cable_curl": "https://youtu.be/S6rqpxVGKZ4?t=406",
    "preacher_curl": "https://youtu.be/S6rqpxVGKZ4?t=906",
    "chest_supported_t_bar_row": "https://youtu.be/S6rqpxVGKZ4?t=998",
    "pull_up": "https://youtu.be/S6rqpxVGKZ4?t=1172",
    "dumbbell_shrugs": "https://youtu.be/S6rqpxVGKZ4?t=74",
    "reverse_pec_deck": "https://youtu.be/S6rqpxVGKZ4?t=307",
    "overhead_press": "https://youtu.be/S6rqpxVGKZ4?t=506",
    "lateral_raise": "https://youtu.be/S6rqpxVGKZ4?t=947",
    "standing_calf_raise": "https://youtu.be/S6rqpxVGKZ4?t=100",
    "nautilus_glute_drive": "https://youtu.be/S6rqpxVGKZ4?t=374",
    "walking_lunge": "https://youtu.be/S6rqpxVGKZ4?t=543",
    "seated_leg_curl": "https://youtu.be/S6rqpxVGKZ4?t=770",
    "leg_extension": "https://youtu.be/S6rqpxVGKZ4?t=819",
    "romanian_deadlift": "https://youtu.be/S6rqpxVGKZ4?t=1060",
    "squat": "https://youtu.be/S6rqpxVGKZ4?t=1252",
    "dumbbell_wrist_curls_and_extensions": "https://youtu.be/S6rqpxVGKZ4?t=125",
    "neck_curls_and_extensions": "https://youtu.be/S6rqpxVGKZ4?t=172",
    "cable_crunch": "https://youtu.be/S6rqpxVGKZ4?t=224",
    "deadlift": "https://youtu.be/S6rqpxVGKZ4?t=506",
}


def _normalize_exercise_name(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lower())


def _get_demo_links_for_exercises(user, exercise_names: list[str]) -> dict[str, str]:
    """Return {original_name: url} for exercises the user hasn't seen yet. Max 1."""
    seen = user.seen_exercise_demos or {}
    result = {}
    for name in exercise_names:
        key = _normalize_exercise_name(name)
        if key in EXERCISE_DEMO_LINKS and not seen.get(key):
            result[name] = EXERCISE_DEMO_LINKS[key]
            break  # one new demo per message max

    if result:
        new_key = _normalize_exercise_name(list(result.keys())[0])
        def _mark_seen():
            session = get_session()
            try:
                user_row = session.get(User, user.id)
                if user_row:
                    updated = dict(user_row.seen_exercise_demos or {})
                    updated[new_key] = True
                    user_row.seen_exercise_demos = updated
                    session.commit()
            finally:
                session.close()
        threading.Thread(target=_mark_seen, daemon=True).start()

    return result


def _build_training_context(user: User) -> str:
    """Build training-specific context — only what the training agent needs."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    try:
        user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
    except Exception:
        user_tz = ZoneInfo("America/Los_Angeles")

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
            f"[{m.created_at.replace(tzinfo=timezone.utc).astimezone(user_tz).strftime('%b %d %I:%M %p')}] {'Coach' if m.direction == 'out' else user.name}: {m.body}"
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

    # Build available demo links (exercises user hasn't seen yet)
    seen = user.seen_exercise_demos or {}
    unseen_demos = {k: v for k, v in EXERCISE_DEMO_LINKS.items() if not seen.get(k)}
    if unseen_demos:
        demo_list = "\n".join(f"  {k}: {v}" for k, v in unseen_demos.items())
        demo_block = (
            f"\n\n## EXERCISE DEMO LINKS (unseen by this user)\n"
            f"If you are programming any of these exercises for a beginner or intermediate user, "
            f"include the demo link inline: \"bench press — 3x8 (form demo: <url>)\"\n"
            f"Only include ONE new demo per message. Don't send demos for exercises they've done before.\n"
            f"{demo_list}"
        )
    else:
        demo_block = ""

    # Session state — what the user is currently doing
    from models import get_session_state
    current_state = get_session_state(user.id)
    if current_state:
        session_block = (
            f"\n\n## ACTIVE SESSION\n"
            f"The user is currently: {current_state.get('status', 'unknown')}. "
            f"Started: {current_state.get('started_at', 'unknown')}. "
            f"Keep responses brief and action-oriented — they're mid-session."
        )
    else:
        session_block = ""

    return (
        f"## USER PROFILE\n{profile}\n\n"
        f"## CONFIRMED DECISIONS (settled — do not re-ask or re-explain reasoning)\n{confirmed_block}\n\n"
        f"## TODAY'S TRAINING STATUS\n{workout_status}"
        f"{session_block}\n\n"
        f"## RECENT WORKOUT HISTORY\n{workout_history}"
        f"{memory_block}"
        f"{demo_block}\n\n"
        f"## RECENT CONVERSATION\n{conversation}"
    )


def handle(user: User, user_message: str, image_data: dict = None) -> dict:
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

LIVE WORKOUT REPORTING RULES (when user is texting between sets):
- Single set report (e.g. "set 1: 225x5") → brief ack. "Logged" or a one-line form cue. Do NOT ask about the next set or remaining sets — they're resting and will report when ready.
- All sets reported at once (e.g. "bench 3x8 at 185") → log all sets, give a brief summary with any progression note.
- Resting / "still resting" / "one more" / "doing another set" → acknowledge and wait. Don't coach. Don't suggest. "Take your time" or "got it" is enough.
- Transition between exercises (e.g. "moving on to rows" / "done with bench") → summarize what they logged for the previous exercise (sets x reps x weight), acknowledge the transition. Don't prescribe the next exercise unless they ask.
- End of workout (e.g. "done" / "that's it" / "heading out") → give a complete session summary: exercises, total sets, any PRs or notable numbers. Keep it tight.
- During live reporting, keep ALL responses under 160 characters. The user is mid-set with their phone. Be terse.
"""

    user_content = [{"type": "text", "text": user_message}]
    if image_data:
        user_content.insert(0, {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image_data.get("content_type", "image/jpeg"),
                "data": image_data["data"],
            },
        })

    response = client.messages.create(
        model=config.COACH_MODEL,
        max_tokens=config.MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
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

    # Mark any demo'd exercises as seen so they aren't repeated
    content = parsed.get("content", {})
    exercises = content.get("exercises", [])
    if exercises and isinstance(exercises, list):
        names = [e.get("name", "") for e in exercises if isinstance(e, dict) and e.get("name")]
        if names:
            _get_demo_links_for_exercises(user, names)

    return {
        "agent": "training",
        "intent": parsed.get("intent", "training_general"),
        "content": content,
        "clarifying_question": parsed.get("clarifying_question"),
        "log_action": parsed.get("log_action"),
    }
