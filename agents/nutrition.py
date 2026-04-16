"""
Nutrition Agent — Cued
=======================
Specialist for meals, macros, food questions, meal suggestions.
Returns structured data that the personality layer turns into SMS responses.

Phase 2a: Generates coaching content, no tracking yet.
Phase 2b: Adds meal extraction, photo handling, daily totals, weight logging.
"""

import json
import logging
import anthropic
import config
from models import get_session, User, Message
from skill_loader import load_skill

logger = logging.getLogger("cued.nutrition")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _build_nutrition_context(user: User) -> str:
    """Build nutrition-specific context — profile, today's totals, recent meals, and conversation."""
    from models import Meal, ensure_todays_totals
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    ensure_todays_totals(user.id)

    session = get_session()
    try:
        # Re-fetch user so we have fresh totals after ensure_todays_totals
        user = session.query(User).get(user.id)

        try:
            user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
        except Exception:
            user_tz = ZoneInfo("America/Los_Angeles")

        now_local = datetime.now(user_tz)

        # Recent conversation (last 6 messages)
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

        # Today's logged meals
        today_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        today_meals = (
            session.query(Meal)
            .filter(Meal.user_id == user.id, Meal.eaten_at >= today_start)
            .order_by(Meal.eaten_at.asc())
            .all()
        )

        # Last 3 days meals (excluding today)
        three_days_ago = today_start - timedelta(days=3)
        recent_meals = (
            session.query(Meal)
            .filter(
                Meal.user_id == user.id,
                Meal.eaten_at >= three_days_ago,
                Meal.eaten_at < today_start,
            )
            .order_by(Meal.eaten_at.desc())
            .limit(10)
            .all()
        )
    finally:
        session.close()

    # Build totals block
    cal_target = user.calorie_target
    pro_target = user.protein_target
    if cal_target:
        cal_remaining = cal_target - (user.calories_today or 0)
        totals_block = (
            f"Calories: {user.calories_today or 0} / {cal_target} ({cal_remaining} remaining)\n"
            f"Protein: {user.protein_today or 0}g / {f'{pro_target}g' if pro_target else '?'}\n"
            f"Carbs: {user.carbs_today or 0}g | Fat: {user.fat_today or 0}g"
        )
    else:
        totals_block = "Targets not set — calculate before giving specific macro guidance."

    if today_meals:
        meals_lines = "\n".join(
            f"  - {m.eaten_at.strftime('%I:%M %p')}: {m.description} ({m.calories} cal, {m.protein_g}g protein)"
            for m in today_meals
        )
        totals_block += f"\n\nToday's logged meals:\n{meals_lines}"
    else:
        totals_block += "\n\nNo meals logged yet today."

    if recent_meals:
        recent_lines = "\n".join(
            f"  - {m.eaten_at.strftime('%a %b %d')}: {m.description} ({m.calories} cal)"
            for m in recent_meals
        )
        totals_block += f"\n\nRecent meals (last 3 days):\n{recent_lines}"

    profile = f"""Name: {user.name}
Goal: {user.goal}
Confirmed goal priority: {user.confirmed_goal_priority or "not set"}
Calorie target: {user.calorie_target or "not set — collect info and calculate before recommending"}
Protein target: {f"{user.protein_target}g" if user.protein_target else "not set"}
Height: {f"{user.height_ft}'{user.height_in or 0}" if user.height_ft else "not known"}
Weight: {f"{user.weight_lbs} lbs" if user.weight_lbs else "not known"}
Diet: {user.diet or "omnivore"}
Restrictions: {user.restrictions or "none reported"}
Cooking situation: {user.cooking_situation or "unknown"}
Food context: {user.food_context or "not collected yet"}
Activity level: {user.activity_level or "not set"}"""

    return f"## USER PROFILE\n{profile}\n\n## TODAY'S TRACKING\n{totals_block}\n\n## RECENT CONVERSATION\n{conversation}"


def handle(user: User, user_message: str, image_url: str = None) -> dict:
    """
    Process a nutrition-related message and return structured coaching content.

    Returns:
    {
        "agent": "nutrition",
        "intent": "meal_suggestion" | "meal_question" | "macro_check" | etc.,
        "content": {...},
        "clarifying_question": str or None,
        "log_action": str or None,  # Phase 2b will populate this
    }
    """
    personality = load_skill("personality")
    safety = load_skill("safety")
    nutrition_skill = load_skill("nutrition")
    context = _build_nutrition_context(user)

    system_prompt = f"""{personality}

---

{safety}

---

{nutrition_skill}

---

{context}

## YOUR TASK
You are the nutrition specialist. The user sent a message related to food, meals, or macros. Your job is to analyze what they need and return STRUCTURED JSON describing the coaching content. Another agent will turn your structured output into the actual SMS response.

DO NOT write prose. DO NOT use first person. Return ONLY valid JSON.

Return this structure:
{{
  "intent": "brief label like meal_suggestion, meal_question, macro_check, food_report, recipe_request",
  "content": {{
    // Fields relevant to this intent. Examples:
    // For meal_suggestion: "meal_description", "calories", "protein", "timing_note"
    // For macro_check: "current_status", "remaining", "recommendation"
    // For food_report: "what_user_ate", "estimated_calories", "estimated_protein", "notes"
    // For recipe_request: "recipe_name", "ingredients", "macros", "prep_note"
  }},
  "clarifying_question": "a natural question to ask if more info is needed, or null",
  "coaching_note": "any additional coaching observation to include, or null"
}}

Rules:
- If targets aren't set and you need them, put that in clarifying_question (ask for weight/height/activity)
- NEVER recommend other apps (MyFitnessPal, Cronometer) — Cued handles tracking
- If the user reported eating something, acknowledge it in content.what_user_ate
- If you need a portion size to estimate, put a clarifying_question
- Use the user's food_context when suggesting meals — they've told you their situation
- Be specific with numbers. "Around 500 cal" not "a good amount of calories"
"""

    user_content = [{"type": "text", "text": user_message}]
    if image_url:
        user_content.insert(0, {
            "type": "image",
            "source": {"type": "url", "url": image_url},
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
        logger.error(f"Nutrition agent returned invalid JSON: {text[:200]} — {e}")
        parsed = {
            "intent": "nutrition_general",
            "content": {"note": "Unable to parse specialist output"},
            "clarifying_question": None,
            "coaching_note": None,
        }

    return {
        "agent": "nutrition",
        "intent": parsed.get("intent", "nutrition_general"),
        "content": parsed.get("content", {}),
        "clarifying_question": parsed.get("clarifying_question"),
        "log_action": None,  # Phase 2b will populate
    }
