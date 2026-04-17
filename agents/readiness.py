"""
Readiness Agent — Cued
=======================
Specialist for sleep, recovery, energy levels, HRV data,
rest day decisions, and training intensity adjustments.
Returns structured data that the personality layer turns into SMS responses.
"""

import json
import logging
import anthropic
import config
from models import get_session, User, Message, DailyLog
from skill_loader import load_skill
from models import is_workout_confirmed_today

logger = logging.getLogger("cued.readiness")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _build_readiness_context(user: User) -> str:
    """Build readiness-specific context — sleep, recovery, energy."""
    session = get_session()
    try:
        # Recent messages
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

        # Recent daily logs for recovery trends
        recent_logs = (
            session.query(DailyLog)
            .filter(DailyLog.user_id == user.id)
            .order_by(DailyLog.date.desc())
            .limit(7)
            .all()
        )
        recent_logs.reverse()

        if recent_logs:
            log_lines = []
            for dl in recent_logs:
                date_str = dl.date.strftime("%a %b %d")
                parts = []
                if dl.sleep_hours:
                    parts.append(f"sleep: {dl.sleep_hours}h")
                if dl.energy_level:
                    parts.append(f"energy: {dl.energy_level}/5")
                if dl.daily_rating:
                    parts.append(f"rating: {dl.daily_rating}/5")
                if dl.workout_confirmed:
                    parts.append("trained")
                log_lines.append(f"  {date_str}: {', '.join(parts) if parts else 'no data'}")
            logs_text = "\n".join(log_lines)
        else:
            logs_text = "  No daily logs yet."
    finally:
        session.close()

    # Workout confirmation status
    workout_confirmed = is_workout_confirmed_today(user.id)
    workout_status = (
        "User has CONFIRMED training today."
        if workout_confirmed else
        "User has NOT confirmed training today."
    )

    # Confirmed decisions
    decisions = []
    if user.confirmed_goal_priority:
        decisions.append(f"Goal priority: {user.confirmed_goal_priority} (CONFIRMED)")
    if user.confirmed_training_split:
        decisions.append(f"Training split: {user.confirmed_training_split} (CONFIRMED)")
    if user.confirmed_training_days:
        decisions.append(f"Training days: {user.confirmed_training_days} (CONFIRMED)")
    if user.confirmed_workout_time:
        decisions.append(f"Workout time: {user.confirmed_workout_time} (CONFIRMED)")
    confirmed_block = "\n".join(decisions) if decisions else "No decisions confirmed yet."

    profile = f"""Name: {user.name}
Age: {user.age or "unknown"}
Sleep quality (self-reported at signup): {user.sleep_quality or "unknown"}
Stress level (self-reported at signup): {user.stress_level or "unknown"}
Wearable: {user.wearable or "none"}
Weight: {f"{user.weight_lbs} lbs" if user.weight_lbs else "unknown"}"""

    memory_block = f"\n\n## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n{user.memory}" if user.memory else ""

    return (
        f"## USER PROFILE\n{profile}\n\n"
        f"## CONFIRMED DECISIONS (settled — do not re-ask or re-explain)\n{confirmed_block}\n\n"
        f"## TODAY'S TRAINING STATUS\n{workout_status}\n\n"
        f"## RECENT RECOVERY DATA (last 7 days)\n{logs_text}"
        f"{memory_block}\n\n"
        f"## RECENT CONVERSATION\n{conversation}"
    )


def handle(user: User, user_message: str) -> dict:
    """
    Process a readiness-related message and return structured coaching content.
    """
    personality = load_skill("personality")
    safety = load_skill("safety")
    readiness_skill = load_skill("readiness")
    context = _build_readiness_context(user)

    system_prompt = f"""{personality}

---

{safety}

---

{readiness_skill}

---

{context}

## YOUR TASK
You are the readiness specialist. The user sent a message about sleep, recovery, energy, fatigue, or whether they should train today. Analyze and return STRUCTURED JSON. Another agent writes the SMS.

DO NOT write prose. Return ONLY valid JSON.

Return this structure:
{{
  "intent": "sleep_report, recovery_check, rest_day_question, energy_report, training_intensity_question, hrv_data",
  "content": {{
    // Fields relevant to this intent. Examples:
    // For sleep_report: "hours_reported", "quality_assessment", "impact_on_training"
    // For recovery_check: "readiness_score", "recommendation", "reasoning"
    // For rest_day_question: "recommendation" (train/rest/light), "reasoning"
    // For energy_report: "level", "likely_cause", "adjustment"
  }},
  "clarifying_question": "question if needed, or null",
  "coaching_note": "additional observation, or null"
}}

Rules:
- If the user says they're tired, distinguish between "didn't sleep well" (recovery issue) and "long day" (mental fatigue) — different recommendations
- Poor sleep + training day = suggest lighter intensity or shorter session, not skip entirely (unless multiple bad nights in a row)
- If wearable data is mentioned (HRV, resting heart rate), interpret it practically — "your HRV is down 15% from baseline, take it easier today"
- Don't be overly cautious — most people can train through mild fatigue. Only suggest rest for genuine recovery concerns
- Reference recent daily logs for trends — one bad night is fine, three in a row is a pattern worth addressing
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
        logger.error(f"Readiness agent returned invalid JSON: {text[:200]} — {e}")
        parsed = {
            "intent": "readiness_general",
            "content": {"note": "Unable to parse specialist output"},
            "clarifying_question": None,
            "coaching_note": None,
        }

    return {
        "agent": "readiness",
        "intent": parsed.get("intent", "readiness_general"),
        "content": parsed.get("content", {}),
        "clarifying_question": parsed.get("clarifying_question"),
        "log_action": None,
    }
