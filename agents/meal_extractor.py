"""
Meal Extractor — Cued
======================
Runs after every nutrition exchange. Analyzes user message + coach response
and determines if a meal should be logged.

Classifications:
- user_reported: user described something they ate without coach suggesting it
- confirmed_suggestion: coach suggested a meal, user confirmed eating it (possibly with swaps)
- no_meal: no eating reported
- suggestion_only: coach suggested, user hasn't confirmed yet
"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import anthropic
import config
from models import get_session, User, Meal, ensure_todays_totals

logger = logging.getLogger("cued.meal_extractor")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def extract_and_log_meal(user_id: int, user_message: str, coach_response: str, recent_coach_messages: str = ""):
    """
    Analyze the exchange and log a meal if appropriate.
    Updates the user's running daily totals.
    """
    prompt = f"""You are analyzing an SMS fitness coaching exchange to determine if a meal should be logged.

Recent coach messages (for context on what the coach may have suggested):
{recent_coach_messages if recent_coach_messages else "(none)"}

User said: "{user_message}"
Coach just responded: "{coach_response}"

Classify this exchange as exactly ONE of:
- user_reported: User described eating something specific, WITHOUT the coach having suggested it first
- confirmed_suggestion: Coach previously suggested a meal, and user confirmed eating it (possibly with modifications)
- no_meal: No eating was reported — could be a question, suggestion only, or unrelated
- suggestion_only: Coach suggested a meal in this exchange, but user hasn't confirmed eating it

If classification is user_reported or confirmed_suggestion, extract the meal details.
For confirmed_suggestion, account for any swaps or modifications the user mentioned.

Return ONLY valid JSON:
{{
  "classification": "user_reported" | "confirmed_suggestion" | "no_meal" | "suggestion_only",
  "should_log": true | false,
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
- Only set should_log=true for user_reported or confirmed_suggestion
- For no_meal or suggestion_only, meal=null and should_log=false
- Be realistic with estimates — account for typical portions, added oils/sauces
- Confidence is high if the user gave specific details (weights, exact items), medium if generic ("a burrito bowl"), low if vague ("some chicken")
- Description should be what they ate, not what the coach suggested (unless they match)
- If user said "had it" or "ate that" confirming a specific suggestion, use the suggested meal's details

Examples:
User: "just had a chicken burrito bowl from chipotle"
Coach: "Nice — that's around 700 cal, 45g protein. You're at 1200/2200 cal for the day."
→ {{"classification": "user_reported", "should_log": true, "meal": {{"description": "chicken burrito bowl from Chipotle", "calories": 700, "protein_g": 45, "carbs_g": 85, "fat_g": 20, "confidence": "medium", "notes": ""}}}}

User: "what should I have for dinner?"
Coach: "Try grilled chicken, rice, and veggies — around 600 cal, 50g protein."
→ {{"classification": "suggestion_only", "should_log": false, "meal": null}}

User: "okay yeah I had that, but I used brown rice instead"
Coach: "Solid swap. Brown rice bumps fiber without changing the totals much."
→ {{"classification": "confirmed_suggestion", "should_log": true, "meal": {{"description": "grilled chicken, brown rice, and veggies", "calories": 600, "protein_g": 50, "carbs_g": 70, "fat_g": 15, "confidence": "medium", "notes": "swapped white rice for brown"}}}}

User: "what's my protein at for today?"
Coach: "You're at 85g out of 145g."
→ {{"classification": "no_meal", "should_log": false, "meal": null}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        if "}" in text:
            text = text[:text.rindex("}") + 1]
        data = json.loads(text)

        if not data.get("should_log") or not data.get("meal"):
            logger.info(f"No meal to log for user {user_id} (classification: {data.get('classification')})")
            return

        meal_data = data["meal"]

        # Ensure today's totals are for today (resets if new day)
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

            meal = Meal(
                user_id=user_id,
                eaten_at=datetime.now(user_tz),
                description=meal_data.get("description", ""),
                calories=meal_data.get("calories", 0),
                protein_g=meal_data.get("protein_g", 0),
                carbs_g=meal_data.get("carbs_g", 0),
                fat_g=meal_data.get("fat_g", 0),
                source="text",
                log_type=data["classification"],
                confidence=meal_data.get("confidence", "medium"),
                notes=meal_data.get("notes", ""),
            )
            session.add(meal)

            # Update running daily totals
            user.calories_today = (user.calories_today or 0) + (meal_data.get("calories") or 0)
            user.protein_today = (user.protein_today or 0) + (meal_data.get("protein_g") or 0)
            user.carbs_today = (user.carbs_today or 0) + (meal_data.get("carbs_g") or 0)
            user.fat_today = (user.fat_today or 0) + (meal_data.get("fat_g") or 0)

            session.commit()
            logger.info(
                f"Logged meal for {user.name}: {meal_data.get('description')} "
                f"({meal_data.get('calories')} cal) — totals now {user.calories_today}/{user.calorie_target or '?'} cal"
            )
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Meal extraction failed for user {user_id}: {e}")
