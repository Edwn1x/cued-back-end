"""
Meal Extractor — Cued
======================
Runs after every nutrition exchange. Analyzes user message + coach response
and determines if a meal should be logged or an existing entry updated.

Active meal context:
- When a meal is first created, active_meal_id is set on the user
- For 10 minutes, follow-up messages about the same meal update the existing
  row instead of inserting a new one
- Context closes after the window expires, a topic change, or explicit close

Classifications:
- user_reported: user described something they ate without coach suggesting it
- confirmed_suggestion: coach suggested a meal, user confirmed eating it
- refinement: user is adding details/corrections to an already-logged meal
- new_separate_meal: user mentioned a clearly different food (separate entry)
- no_meal: no eating reported
- suggestion_only: coach suggested, user hasn't confirmed yet
"""

import json
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import anthropic
import config
from models import get_session, User, Meal, ensure_todays_totals, get_active_meal, set_active_meal, clear_active_meal

logger = logging.getLogger("cued.meal_extractor")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def _build_prompt(user_message: str, coach_response: str, recent_coach_messages: str, active_meal_desc: str | None) -> str:
    active_context = ""
    if active_meal_desc:
        active_context = f"""
ACTIVE MEAL CONTEXT: The user recently logged this meal and may be adding details or corrections:
"{active_meal_desc}"

Classification rules when active meal context is set:
- If the user is adding a condiment, cooking method, portion correction, or side to the active meal → "refinement"
- If the user mentions a clearly separate food that wouldn't be part of the same plate or sitting → "new_separate_meal"
- If the user says "actually" or corrects a previous detail → "refinement"
- "I used avocado oil" or "oh and I had hot sauce" → "refinement"
- "I also had a protein shake after" or "and then I had a banana" → "new_separate_meal"
"""

    return f"""You are analyzing an SMS fitness coaching exchange to determine if a meal should be logged.

Recent coach messages (for context on what the coach may have suggested):
{recent_coach_messages if recent_coach_messages else "(none)"}

User said: "{user_message}"
Coach just responded: "{coach_response}"
{active_context}
Classify this exchange as exactly ONE of:
- user_reported: User described eating something specific, WITHOUT the coach having suggested it first
- confirmed_suggestion: Coach previously suggested a meal, and user confirmed eating it (possibly with modifications)
- refinement: User is adding details or corrections to the active meal (only valid when active meal context is set)
- new_separate_meal: User mentioned a clearly separate food item that should be a new entry
- no_meal: No eating was reported — could be a question, suggestion only, or unrelated
- suggestion_only: Coach suggested a meal in this exchange, but user hasn't confirmed eating it

Return ONLY valid JSON:
{{
  "classification": "user_reported" | "confirmed_suggestion" | "refinement" | "new_separate_meal" | "no_meal" | "suggestion_only",
  "should_log": true | false,
  "is_update": true | false,
  "meal": {{
    "description": "brief description of what they ate",
    "calories": number (best estimate),
    "protein_g": number,
    "carbs_g": number,
    "fat_g": number,
    "confidence": "high" | "medium" | "low",
    "notes": "any relevant details like portion, oil used, etc. or empty string"
  }} or null if should_log is false
}}

Rules:
- should_log=true for: user_reported, confirmed_suggestion, new_separate_meal
- is_update=true ONLY for: refinement (update existing row, do not create new)
- For refinement: meal should contain COMPLETE revised totals for the entire meal (not just the addition)
- For no_meal or suggestion_only: meal=null, should_log=false, is_update=false
- Be realistic with estimates — account for typical portions, added oils/sauces
- Confidence: high=specific details given, medium=generic description, low=vague

Examples:
User: "just had a chicken burrito bowl from chipotle"
→ {{"classification": "user_reported", "should_log": true, "is_update": false, "meal": {{"description": "chicken burrito bowl", "calories": 700, "protein_g": 45, "carbs_g": 85, "fat_g": 20, "confidence": "medium", "notes": ""}}}}

User: "oh I also used avocado oil to cook it" (active meal: salmon fillet 325 cal)
→ {{"classification": "refinement", "should_log": false, "is_update": true, "meal": {{"description": "salmon fillet with avocado oil", "calories": 445, "protein_g": 42, "carbs_g": 0, "fat_g": 28, "confidence": "medium", "notes": "added ~120 cal from avocado oil"}}}}

User: "I also had a protein shake after"
→ {{"classification": "new_separate_meal", "should_log": true, "is_update": false, "meal": {{"description": "protein shake", "calories": 150, "protein_g": 25, "carbs_g": 8, "fat_g": 3, "confidence": "medium", "notes": ""}}}}

User: "what's my protein at for today?"
→ {{"classification": "no_meal", "should_log": false, "is_update": false, "meal": null}}"""


def extract_and_log_meal(user_id: int, user_message: str, coach_response: str, recent_coach_messages: str = ""):
    """
    Analyze the exchange and log or update a meal if appropriate.
    Checks active_meal_id first — if set and within the 10-min window,
    routes to update path instead of creating a new row.
    Updates the user's running daily totals.
    """
    try:
        # Check for active meal context
        active_meal = get_active_meal(user_id)
        active_meal_desc = active_meal.description if active_meal else None

        prompt = _build_prompt(user_message, coach_response, recent_coach_messages, active_meal_desc)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        if "}" in text:
            text = text[:text.rindex("}") + 1]
        data = json.loads(text)

        classification = data.get("classification")
        is_update = data.get("is_update", False)
        meal_data = data.get("meal")

        # Topic change — close the active meal context
        if classification in ("no_meal", "suggestion_only"):
            logger.info(f"No meal to log for user {user_id} (classification: {classification})")
            return

        if not meal_data:
            return

        ensure_todays_totals(user_id)
        session = get_session()
        try:
            user = session.get(User, user_id)
            if not user:
                return

            try:
                user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
            except Exception:
                user_tz = ZoneInfo("America/Los_Angeles")

            if is_update and active_meal:
                # UPDATE path — edit existing meal row in place
                existing = session.get(Meal, active_meal.id)
                if existing:
                    old_cal = existing.calories or 0
                    old_protein = existing.protein_g or 0
                    old_carbs = existing.carbs_g or 0
                    old_fat = existing.fat_g or 0

                    existing.description = meal_data.get("description", existing.description)
                    existing.calories = meal_data.get("calories", existing.calories)
                    existing.protein_g = meal_data.get("protein_g", existing.protein_g)
                    existing.carbs_g = meal_data.get("carbs_g", existing.carbs_g)
                    existing.fat_g = meal_data.get("fat_g", existing.fat_g)
                    existing.confidence = meal_data.get("confidence", existing.confidence)
                    if meal_data.get("notes"):
                        existing.notes = meal_data["notes"]

                    # Adjust daily totals by the delta
                    user.calories_today = (user.calories_today or 0) + (existing.calories - old_cal)
                    user.protein_today = (user.protein_today or 0) + (existing.protein_g - old_protein)
                    user.carbs_today = (user.carbs_today or 0) + (existing.carbs_g - old_carbs)
                    user.fat_today = (user.fat_today or 0) + (existing.fat_g - old_fat)

                    # Extend the active meal window
                    user.active_meal_updated_at = datetime.now(timezone.utc)

                    session.commit()
                    logger.info(
                        f"Updated meal for {user.name}: {existing.description} "
                        f"({existing.calories} cal) — totals now {user.calories_today}/{user.calorie_target or '?'} cal"
                    )
                    return

            # CREATE path — new meal row
            meal = Meal(
                user_id=user_id,
                eaten_at=datetime.now(user_tz),
                description=meal_data.get("description", ""),
                calories=meal_data.get("calories", 0),
                protein_g=meal_data.get("protein_g", 0),
                carbs_g=meal_data.get("carbs_g", 0),
                fat_g=meal_data.get("fat_g", 0),
                source="text",
                log_type=classification,
                confidence=meal_data.get("confidence", "medium"),
                notes=meal_data.get("notes", ""),
            )
            session.add(meal)
            session.flush()  # get meal.id before commit

            # Update running daily totals
            user.calories_today = (user.calories_today or 0) + (meal_data.get("calories") or 0)
            user.protein_today = (user.protein_today or 0) + (meal_data.get("protein_g") or 0)
            user.carbs_today = (user.carbs_today or 0) + (meal_data.get("carbs_g") or 0)
            user.fat_today = (user.fat_today or 0) + (meal_data.get("fat_g") or 0)

            # Set new active meal context
            user.active_meal_id = meal.id
            user.active_meal_updated_at = datetime.now(timezone.utc)

            session.commit()
            logger.info(
                f"Logged meal for {user.name}: {meal_data.get('description')} "
                f"({meal_data.get('calories')} cal) — totals now {user.calories_today}/{user.calorie_target or '?'} cal"
            )
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Meal extraction failed for user {user_id}: {e}")
