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

    # Confirmed decisions — shared state across all agents
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

    profile = f"""Name: {user.name}
Goal: {user.goal}
Height: {f"{user.height_ft}'{user.height_in or 0}" if user.height_ft else "not known"}
Weight: {f"{user.weight_lbs} lbs" if user.weight_lbs else "not known"}
Diet: {user.diet or "omnivore"}
Restrictions: {user.restrictions or "none reported"}
Cooking situation: {user.cooking_situation or "unknown"}
Food context: {user.food_context or "not collected yet"}"""

    return f"## USER PROFILE\n{profile}\n\n## CONFIRMED DECISIONS (settled — do not re-ask)\n{confirmed_block}\n\n## TODAY'S TRACKING\n{totals_block}\n\n## RECENT CONVERSATION\n{conversation}"


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
        "log_action": None,
    }


def is_daily_log_query(message: str) -> bool:
    """Detect if the user is asking for their daily log."""
    msg = message.lower().strip()
    if msg in ("log", "daily log", "my log", "food log", "food log today"):
        return True
    trigger_phrases = [
        "what have i eaten", "what did i eat today", "what have i ate today",
        "my meals today", "todays meals", "today's meals",
        "where am i at", "how am i doing today", "show log", "show my log",
    ]
    for phrase in trigger_phrases:
        if phrase in msg and len(msg) < 60:
            return True
    return False


def handle_daily_log_query(user) -> str:
    """Return a formatted summary of today's meals and totals."""
    from models import Meal, ensure_todays_totals, get_session, User
    from datetime import datetime
    from zoneinfo import ZoneInfo

    ensure_todays_totals(user.id)

    session = get_session()
    try:
        user = session.query(User).get(user.id)

        try:
            user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
        except Exception:
            user_tz = ZoneInfo("America/Los_Angeles")

        today_start = datetime.now(user_tz).replace(hour=0, minute=0, second=0, microsecond=0)
        todays_meals = (
            session.query(Meal)
            .filter(Meal.user_id == user.id, Meal.eaten_at >= today_start)
            .order_by(Meal.eaten_at.asc())
            .all()
        )

        if not todays_meals:
            return "Nothing logged yet today. Tell me what you've eaten and I'll start tracking."

        lines = ["Today so far:"]
        for m in todays_meals:
            time_str = m.eaten_at.astimezone(user_tz).strftime("%I:%M %p").lstrip("0")
            lines.append(f"— {time_str}: {m.description} (~{m.calories} cal, {m.protein_g}g protein)")

        cal_total = user.calories_today or 0
        protein_total = user.protein_today or 0
        cal_target = user.calorie_target or "?"
        protein_target = user.protein_target or "?"

        lines.append("")
        lines.append(f"Total: {cal_total}/{cal_target} cal, {protein_total}g/{protein_target}g protein")

        if isinstance(cal_target, int):
            remaining = cal_target - cal_total
            if remaining > 0:
                lines.append(f"Room for {remaining} cal today.")

        return "\n".join(lines)
    finally:
        session.close()


def handle_food_photo(user, user_message: str, image_url: str) -> dict:
    """First pass on a food photo. Estimates a range and asks clarifying questions."""
    import json as json_lib
    from models import get_session, User

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

## YOUR TASK — FOOD PHOTO FIRST PASS

The user sent a food photo. Your job is to:
1. Identify what's in the photo
2. Give an initial calorie + protein estimate as a RANGE (not a single number)
3. Ask 1-2 specific clarifying questions to narrow the estimate

DO NOT log the meal yet. The user will answer your questions and then you'll refine.

Common things to clarify: portion size, cooking method (grilled vs fried, oil used),
added sauces/dressings, whether it's a branded item, whether they ate all of it.

Return ONLY valid JSON:
{{
  "intent": "food_photo_first_pass",
  "content": {{
    "identified_items": "what you see in the photo",
    "initial_range": {{
      "calories_low": number,
      "calories_high": number,
      "protein_low": number,
      "protein_high": number
    }},
    "initial_description": "brief description for later logging"
  }},
  "clarifying_question": "1-2 specific questions as ONE sentence",
  "coaching_note": null
}}"""

    user_content = [
        {"type": "image", "source": {"type": "url", "url": image_url}},
        {"type": "text", "text": user_message or "Here's my meal."},
    ]

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
        parsed = json_lib.loads(text)
    except Exception as e:
        logger.error(f"Photo first pass JSON parse failed: {e}")
        parsed = {
            "intent": "food_photo_first_pass",
            "content": {"note": "couldn't analyze photo cleanly"},
            "clarifying_question": "Mind describing what's in the meal?",
        }

    # Save pending estimate for refinement after user answers
    session = get_session()
    try:
        user_row = session.query(User).get(user.id)
        user_row.pending_photo_meal = json_lib.dumps({
            "image_url": image_url,
            "initial_estimate": parsed.get("content", {}),
            "clarifying_question": parsed.get("clarifying_question", ""),
        })
        session.commit()
    finally:
        session.close()

    return {
        "agent": "nutrition",
        "intent": parsed.get("intent", "food_photo_first_pass"),
        "content": parsed.get("content", {}),
        "clarifying_question": parsed.get("clarifying_question"),
        "log_action": None,
    }


def handle_photo_refinement(user, user_message: str) -> dict:
    """User answered clarifying questions about a food photo. Refine and log."""
    import json as json_lib
    from models import get_session, User, Meal, ensure_todays_totals
    from datetime import datetime
    from zoneinfo import ZoneInfo

    session = get_session()
    try:
        user_row = session.query(User).get(user.id)
        if not user_row or not user_row.pending_photo_meal:
            return None
        pending = json_lib.loads(user_row.pending_photo_meal)
    finally:
        session.close()

    personality = load_skill("personality")
    safety = load_skill("safety")
    nutrition_skill = load_skill("nutrition")
    context = _build_nutrition_context(user)

    initial_estimate = pending.get("initial_estimate", {})
    initial_question = pending.get("clarifying_question", "")

    system_prompt = f"""{personality}

---

{safety}

---

{nutrition_skill}

---

{context}

## YOUR TASK — FOOD PHOTO REFINEMENT

Earlier you analyzed a photo and estimated:
{json_lib.dumps(initial_estimate, indent=2)}

You asked the user: "{initial_question}"
They just answered: "{user_message}"

Now refine your estimate into a single number (not a range) and prepare to log.

Return ONLY valid JSON:
{{
  "intent": "food_photo_refined",
  "content": {{
    "description": "final meal description for logging",
    "calories": number,
    "protein_g": number,
    "carbs_g": number,
    "fat_g": number,
    "running_total_note": "running total context after this meal"
  }},
  "log_this_meal": true,
  "clarifying_question": null
}}"""

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
        parsed = json_lib.loads(text)
    except Exception as e:
        logger.error(f"Photo refinement JSON parse failed: {e}")
        return None

    meal_content = parsed.get("content", {})

    if parsed.get("log_this_meal") and meal_content.get("description"):
        ensure_todays_totals(user.id)

        session = get_session()
        try:
            user_row = session.query(User).get(user.id)
            try:
                user_tz = ZoneInfo(user_row.user_timezone or "America/Los_Angeles")
            except Exception:
                user_tz = ZoneInfo("America/Los_Angeles")

            meal = Meal(
                user_id=user.id,
                eaten_at=datetime.now(user_tz),
                description=meal_content.get("description", ""),
                calories=meal_content.get("calories", 0),
                protein_g=meal_content.get("protein_g", 0),
                carbs_g=meal_content.get("carbs_g", 0),
                fat_g=meal_content.get("fat_g", 0),
                source="photo",
                log_type="user_reported",
                confidence="medium",
                notes="logged from photo with clarifying questions",
            )
            session.add(meal)

            user_row.calories_today = (user_row.calories_today or 0) + meal_content.get("calories", 0)
            user_row.protein_today = (user_row.protein_today or 0) + meal_content.get("protein_g", 0)
            user_row.carbs_today = (user_row.carbs_today or 0) + meal_content.get("carbs_g", 0)
            user_row.fat_today = (user_row.fat_today or 0) + meal_content.get("fat_g", 0)
            user_row.pending_photo_meal = None

            session.commit()
            logger.info(f"Logged photo meal for {user_row.name}: {meal_content.get('description')} ({meal_content.get('calories')} cal)")
        finally:
            session.close()

    return {
        "agent": "nutrition",
        "intent": "food_photo_refined",
        "content": meal_content,
        "clarifying_question": None,
        "log_action": f"Logged meal: {meal_content.get('description')} ({meal_content.get('calories')} cal)",
    }
