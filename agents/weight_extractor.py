"""
Weight Extractor — Cued
========================
Detects when a user reports a weight reading and logs it to WeightLog.
Also updates the user's current weight on their profile.
"""

import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import anthropic
import config
from models import get_session, User, WeightLog

logger = logging.getLogger("cued.weight_extractor")
client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def extract_and_log_weight(user_id: int, user_message: str, coach_response: str):
    """
    Check if the user reported a weight reading and log it if so.
    Uses a two-pass approach: quick regex pre-filter, then LLM confirmation.
    """
    # Quick pre-filter — only run LLM check if message looks like it might contain a weight
    weight_pattern = re.compile(r'\b\d{2,3}(\.\d)?\s*(lb|lbs|pound|pounds|kg)?\b', re.IGNORECASE)
    if not weight_pattern.search(user_message):
        return

    prompt = f"""Analyze this SMS message and determine if the user is reporting their current body weight.

User said: "{user_message}"
Coach just responded: "{coach_response}"

Return ONLY valid JSON:
{{
  "is_weight_report": true | false,
  "weight_lbs": number or null,
  "context_notes": "any relevant context the user mentioned, or empty string"
}}

Rules:
- is_weight_report is true ONLY if the user is reporting THEIR current body weight
- NOT a weight report: mentioning how much to lift, a friend's weight, a food weight ("6oz chicken"), calorie numbers
- If weight is given in kg, convert to lbs (1 kg = 2.205 lbs)
- If user says "around 146" or "I'm like 145-ish", extract the number they gave
- If message is ambiguous, err on the side of false

Examples:
"I weigh 146 lbs" → {{"is_weight_report": true, "weight_lbs": 146, "context_notes": ""}}
"Weighed in at 145 this morning, down a pound" → {{"is_weight_report": true, "weight_lbs": 145, "context_notes": "down a pound from last weigh-in"}}
"I can bench 145" → {{"is_weight_report": false, "weight_lbs": null, "context_notes": ""}}
"Had 6oz of chicken" → {{"is_weight_report": false, "weight_lbs": null, "context_notes": ""}}
"I'm like 70kg" → {{"is_weight_report": true, "weight_lbs": 154, "context_notes": ""}}"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        if "}" in text:
            text = text[:text.rindex("}") + 1]
        data = json.loads(text)

        if not data.get("is_weight_report") or not data.get("weight_lbs"):
            return

        weight_lbs = data["weight_lbs"]
        notes = data.get("context_notes", "")

        session = get_session()
        try:
            user = session.get(User, user_id)
            if not user:
                return

            try:
                user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
            except Exception:
                user_tz = ZoneInfo("America/Los_Angeles")

            log = WeightLog(
                user_id=user_id,
                weighed_at=datetime.now(user_tz),
                weight_lbs=weight_lbs,
                notes=notes,
            )
            session.add(log)

            # Update current weight on profile
            user.weight_lbs = weight_lbs

            session.commit()
            logger.info(f"Logged weight for {user.name}: {weight_lbs} lbs")
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Weight extraction failed for user {user_id}: {e}")
