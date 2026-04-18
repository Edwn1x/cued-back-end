"""
Onboarding Agent — Cued
========================
Dynamic data collection through conversation. Adapts tone based on
experience level and biggest obstacle from signup.

Instead of tracking step numbers, tracks which data points have been
collected. Each exchange:
1. Parse user's message for any data points
2. Store what was found
3. If fields still missing → ask about the next one
4. If all collected → calculate targets, present summary, confirm

The coach knows experience, goal, and obstacle from signup, which
shapes HOW it asks questions (tone, depth of explanation).
"""

import os
import json
import logging
import threading
from datetime import datetime

from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, COACH_MODEL, MAX_RESPONSE_TOKENS
from sms import send_sms
from macro_calculator import calculate_targets

logger = logging.getLogger("cued.onboarding")
client = Anthropic(api_key=ANTHROPIC_API_KEY)

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")


def load_skill(skill_name: str) -> str:
    path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return ""


# Data points the coach needs to collect, in priority order
# Each entry: (field_name, question_context, priority)
REQUIRED_FIELDS = [
    ("height_weight", "height and weight"),
    ("occupation", "what they do — student, desk job, physical work, etc."),
    ("activity_level", "how active they are outside the gym — sedentary desk life, walking campus, on their feet all day"),
    ("workout_days", "how many days per week they can train"),
    ("workout_time", "what time they prefer to work out"),
    ("cooking_situation", "food situation — do they cook at home, eat at a dining hall, mostly eat out, or a mix"),
    ("diet", "dietary preferences or restrictions — vegetarian, vegan, allergies, halal, or no restrictions"),
    ("injuries", "any injuries or physical limitations"),
    ("wake_sleep", "when they typically wake up and go to bed"),
    ("existing_tools", "fitness apps or wearables they currently use"),
]


def _get_missing_fields(user) -> list:
    """Return list of (field_name, question_context) for fields still null."""
    missing = []

    if not user.height_ft or not user.weight_lbs:
        missing.append(("height_weight", "height and weight"))
    if not user.occupation:
        missing.append(("occupation", "what they do — student, desk job, physical work, etc."))
    if not user.activity_level or user.activity_level == "lightly_active":
        missing.append(("activity_level", "how active they are outside the gym — sedentary desk life, walking around campus, on their feet all day"))
    if not user.workout_days:
        missing.append(("workout_days", "how many days per week they can train"))
    if not user.workout_time:
        missing.append(("workout_time", "what time they prefer to work out"))
    if not user.cooking_situation:
        missing.append(("cooking_situation", "food situation — do they cook at home, eat at a dining hall, mostly eat out, or a mix"))
    if not user.diet:
        missing.append(("diet", "dietary preferences or restrictions — vegetarian, vegan, allergies, halal, or no restrictions"))
    # Injuries can be "none" which is a valid answer, so check differently
    if user.injuries is None:
        missing.append(("injuries", "any injuries or physical limitations"))
    if not user.wake_time or not user.sleep_time:
        missing.append(("wake_sleep", "when they typically wake up and go to bed"))
    if user.existing_tools is None:
        missing.append(("existing_tools", "fitness apps or wearables they currently use"))

    return missing


def _get_experience_context(user) -> str:
    """Return coaching tone guidance based on experience level."""
    exp = user.experience or "none"

    if exp == "none":
        return (
            "This user has NEVER trained before. Explain concepts briefly as you go — "
            "don't assume they know what a training split is, what macros are, or how "
            "calorie targets work. Be encouraging without being patronizing. "
            "Frame starting as the hardest part — they already did it."
        )
    elif exp == "beginner":
        return (
            "This user has been training UNDER 6 MONTHS. They know the basics but "
            "may not understand programming, progression, or nutrition deeply. "
            "Light explanations are fine, skip the absolute fundamentals."
        )
    elif exp == "intermediate":
        return (
            "This user has been training 6 MONTHS TO 2 YEARS. They know their way "
            "around a gym and have preferences. Don't over-explain — ask what they're "
            "currently doing and build from there. They may have existing routines "
            "they want to keep or modify."
        )
    else:  # advanced
        return (
            "This user has been training 2+ YEARS. They know what they're doing. "
            "Use shorthand, skip explanations, respect their existing knowledge. "
            "They're here for accountability and optimization, not education. "
            "Ask about their current programming and work with it."
        )


def _build_system_prompt(user) -> str:
    """Build the system prompt for onboarding exchanges."""
    personality = load_skill("personality")
    safety = load_skill("safety")
    experience_context = _get_experience_context(user)

    profile_parts = [
        f"Name: {user.name}",
        f"Age: {user.age}" if user.age else None,
        f"Gender: {user.gender}" if user.gender and user.gender != "prefer_not_to_say" else None,
        f"Goal: {user.goal}" if user.goal else None,
        f"Biggest obstacle: {user.biggest_obstacle}" if user.biggest_obstacle else None,
        f"Experience: {user.experience}" if user.experience else None,
        f"Equipment: {user.equipment}" if user.equipment else None,
        f"Height: {user.height_ft}'{user.height_in}\"" if user.height_ft else None,
        f"Weight: {user.weight_lbs} lbs" if user.weight_lbs else None,
        f"Workout days: {user.workout_days}" if user.workout_days else None,
        f"Workout time: {user.workout_time}" if user.workout_time else None,
        f"Diet: {user.diet}" if user.diet else None,
        f"Cooking: {user.cooking_situation}" if user.cooking_situation else None,
        f"Injuries: {user.injuries}" if user.injuries else None,
        f"Wake time: {user.wake_time}" if user.wake_time else None,
        f"Sleep time: {user.sleep_time}" if user.sleep_time else None,
        f"Existing tools: {user.existing_tools}" if user.existing_tools else None,
    ]
    profile = "\n".join(p for p in profile_parts if p)

    return f"""{personality}

---

{safety}

---

## EXPERIENCE CALIBRATION
{experience_context}

## USER PROFILE (what we know so far)
{profile}

## ONBOARDING RULES
- You are collecting information to build this user's coaching plan.
- Ask ONE question at a time. Never ask two questions in one message.
- If the user asks a question instead of answering yours, answer their question first, then circle back to your question.
- If the user provides multiple data points in one message, acknowledge all of them.
- Keep messages short — 1-2 sentences per message, max 2 messages.
- Never mention database fields, system internals, or "your profile."
- Never say "great question" or "that's a good point" — just answer and move on.
- Reference their goal and obstacle naturally when relevant — don't re-explain them.
- Do not use --- separators during onboarding. One message at a time.
"""


def _extract_data_from_message(user_message: str, user, last_asked_field: str = None) -> dict:
    """
    Use a lightweight AI call to extract any data points from the user's message.
    Returns a dict of field names to values.
    """
    missing = _get_missing_fields(user)
    if not missing:
        return {}

    missing_list = ", ".join(f[0] for f in missing)

    context_hint = ""
    if last_asked_field:
        context_hint = f"""IMPORTANT CONTEXT: The coach just asked the user about: {last_asked_field}
The user's response is MOST LIKELY answering that question. Prioritize mapping their answer to that field unless the message clearly refers to something else.

For example:
- If the coach asked about workout_days and user says "5" → workout_days="5", NOT height_ft=5
- If the coach asked about workout_time and user says "5 or 6" → workout_time="17:00", NOT height or workout_days
- If the coach asked about diet and user says "no" → diet="omnivore" (no restrictions)
- If the coach asked about injuries and user says "no" → injuries="none"
- If the coach asked about existing_tools and user says "nah" → existing_tools="none"

"""

    prompt = f"""{context_hint}Extract any fitness coaching profile data from this user message. Only extract what the user CLEARLY stated.

User said: "{user_message}"

Fields we still need: {missing_list}

Return ONLY valid JSON. Use null for anything NOT found in this message.
{{
  "height_ft": number or null (e.g. 5 from "5'7"),
  "height_in": number or null (e.g. 7 from "5'7"),
  "weight_lbs": number or null,
  "occupation": "student, desk job, retail, construction, etc." or null,
  "activity_level": "sedentary" or "lightly_active" or "active" or "very_active" or null,
  "workout_days": "comma separated days like mon,tue,wed,thu,fri" or number like "4" or null,
  "workout_time": "HH:MM in 24h format" or "description like afternoon, morning" or null,
  "diet": "omnivore, vegetarian, vegan, pescatarian, keto, halal, kosher" or null,
  "cooking_situation": "cook_myself, dining_hall, mostly_eat_out, mix" or null,
  "injuries": "description of injuries" or "none" or null,
  "wake_time": "HH:MM in 24h format" or null,
  "sleep_time": "HH:MM in 24h format" or null,
  "existing_tools": "comma separated app/device names" or "none" or null
}}

Examples:
"I'm 5'7 and 145 lbs" → {{"height_ft": 5, "height_in": 7, "weight_lbs": 145, ...rest null}}
"I can do 4 days a week, usually around 5pm" → {{"workout_days": "4", "workout_time": "17:00", ...rest null}}
"I cook most of the time but eat out on weekends" → {{"cooking_situation": "mix", ...rest null}}
"no injuries" → {{"injuries": "none", ...rest null}}
"I use Strava and Apple Watch" → {{"existing_tools": "strava,apple_watch", ...rest null}}
"idk" → {{all null}}

Short answer rules:
- "No", "nah", "nope", "none", "I don't think so" when asked about injuries → injuries="none"
- "No", "nah", "nope", "none" when asked about diet/restrictions → diet="omnivore"
- "No", "nah", "none", "nothing" when asked about existing_tools → existing_tools="none"
- "No" when asked about cooking_situation → ambiguous, return null (coach should follow up)
- Single number like "5" → map to whatever field was just asked about, not height

Activity level mapping (use your judgment):
- Desk job, mostly sitting, drives everywhere → "sedentary"
- Student who walks to class, some daily movement but mostly sitting → "lightly_active"
- 8k+ steps daily, walks a lot, plays recreational sports, runs occasionally → "active"
- Physical job, athlete, trains twice a day, very high daily movement → "very_active"
- If the user describes ANY regular physical activity beyond walking to class (basketball, running, sports), they are at least "active"
- When in doubt between two levels, pick the higher one"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        if "}" in text:
            text = text[:text.rindex("}") + 1]
        return json.loads(text)
    except Exception as e:
        logger.error(f"Onboarding data extraction failed: {e}")
        return {}


def _store_extracted_data(user_id: int, data: dict):
    """Write extracted fields to the user record."""
    from models import get_session, User

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return

        changed = False

        if data.get("height_ft") and not user.height_ft:
            user.height_ft = data["height_ft"]
            changed = True
        if data.get("height_in") is not None and user.height_in is None:
            user.height_in = data["height_in"]
            changed = True
        if data.get("weight_lbs") and not user.weight_lbs:
            user.weight_lbs = data["weight_lbs"]
            changed = True
        if data.get("occupation") and not user.occupation:
            user.occupation = data["occupation"]
            changed = True
        if data.get("workout_days") and not user.workout_days:
            user.workout_days = str(data["workout_days"])
            changed = True
        if data.get("workout_time") and not user.workout_time:
            wt = data["workout_time"]
            time_map = {"morning": "08:00", "afternoon": "14:00", "evening": "18:00"}
            if isinstance(wt, str) and wt.lower() in time_map:
                wt = time_map[wt.lower()]
            user.workout_time = wt
            changed = True
        if data.get("diet") and not user.diet:
            user.diet = data["diet"]
            changed = True
        if data.get("cooking_situation") and not user.cooking_situation:
            user.cooking_situation = data["cooking_situation"]
            changed = True
        if data.get("injuries") is not None and user.injuries is None:
            user.injuries = data["injuries"]
            changed = True
        if data.get("wake_time") and not user.wake_time:
            user.wake_time = data["wake_time"]
            changed = True
        if data.get("sleep_time") and not user.sleep_time:
            user.sleep_time = data["sleep_time"]
            changed = True
        if data.get("existing_tools") is not None and user.existing_tools is None:
            user.existing_tools = data["existing_tools"]
            changed = True
        if data.get("activity_level") and not user.activity_level:
            user.activity_level = data["activity_level"]
            changed = True

        if changed:
            session.commit()
            logger.info(f"Stored onboarding data for {user.name}: {data}")
    finally:
        session.close()


def _generate(system_prompt: str, instruction: str) -> str:
    response = client.messages.create(
        model=COACH_MODEL,
        max_tokens=MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": instruction}],
    )
    return response.content[0].text


def _build_confirmation_summary(user) -> str:
    """Build the confirmation message with calculated targets."""
    targets = calculate_targets(user)

    height_str = f"{user.height_ft}'{user.height_in or 0}\""
    goal_map = {
        "fat_loss": "cutting",
        "muscle_building": "building muscle",
        "fat_loss,muscle_building": "recomp",
        "muscle_building,fat_loss": "recomp",
        "general_fitness": "general fitness",
    }
    goal_label = goal_map.get(user.goal, user.goal)

    return (
        f"Here's what I'm working with: {height_str}, {user.weight_lbs} lbs, {user.age} years old. "
        f"Goal is {goal_label}. Training {user.workout_days} days/week around {user.workout_time}. "
        f"I'm setting you at {targets['calories']} cal and {targets['protein']}g protein daily. "
        f"Sound right?"
    )


def start_onboarding(user):
    """
    Entry point — called from app.py after signup.
    Sends the welcome message + first question in a background thread.
    """
    def _run():
        from models import get_session, User as UserModel
        try:
            system_prompt = _build_system_prompt(user)

            missing = _get_missing_fields(user)
            first_field = missing[0] if missing else None

            instruction = (
                f"Send the first message to {user.name}. They just signed up.\n"
                f"Their experience: {user.experience}. Goal: {user.goal}. Biggest obstacle: {user.biggest_obstacle}.\n"
                f"Equipment: {user.equipment}.\n\n"
                f"In ONE message (2-3 sentences max):\n"
                f"1. Welcome them warmly — reference something specific from their profile that shows you read it\n"
                f"2. Tell them you need a few details to build their plan\n"
                f"3. Ask the first question: {first_field[1] if first_field else 'nothing needed'}\n\n"
                f"Do NOT explain how the service works. Do NOT list what's coming. Just welcome + first question."
            )

            text = _generate(system_prompt, instruction)
            send_sms(user.phone, text, user_id=user.id, message_type="onboarding")

            session = get_session()
            try:
                user_row = session.get(UserModel, user.id)
                if user_row:
                    user_row.onboarding_step = 1
                    session.commit()
            finally:
                session.close()

            logger.info(f"Onboarding started for {user.name}")
        except Exception as e:
            logger.error(f"Onboarding start failed for {user.name}: {e}")

    threading.Thread(target=_run, daemon=True).start()


def handle_onboarding_reply(user, incoming_message: str) -> bool:
    """
    Called from webhook on every message while onboarding_step < 2.

    1. Extract any data points from the user's message
    2. Store them
    3. Check what's still missing
    4. If all collected → present confirmation with calculated targets
    5. If confirmed → complete onboarding
    6. If fields missing → ask about the next one

    Returns True if onboarding is now complete.
    """
    from models import get_session, User as UserModel

    session = get_session()
    try:
        user_row = session.get(UserModel, user.id)
        if not user_row:
            return False
    finally:
        session.close()

    missing_before = _get_missing_fields(user_row)

    if not missing_before:
        # All data collected — this is a confirmation response
        confirmation_keywords = [
            "yeah", "yes", "yep", "sounds good", "looks good", "correct",
            "that's right", "perfect", "ok", "sure", "let's go", "lets go",
            "good", "right", "yea", "ya", "bet", "fs",
        ]
        is_confirmed = any(kw in incoming_message.lower().strip() for kw in confirmation_keywords)

        if is_confirmed:
            return _complete_onboarding(user_row, incoming_message)
        else:
            # User wants to adjust something
            extracted = _extract_data_from_message(incoming_message, user_row, last_asked_field=None)
            if any(v is not None for v in extracted.values()):
                _store_extracted_data(user_row.id, extracted)
                session = get_session()
                try:
                    user_row = session.get(UserModel, user.id)
                finally:
                    session.close()

            system_prompt = _build_system_prompt(user_row)
            instruction = (
                f"The user was shown their plan summary and targets but didn't confirm. "
                f"They said: \"{incoming_message}\"\n\n"
                f"Address their concern or make the adjustment they're asking for. "
                f"Then re-present the updated summary and ask for confirmation again. "
                f"One message, brief."
            )
            text = _generate(system_prompt, instruction)
            send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
            return False

    # Determine what was just asked so extraction knows the context
    last_asked = missing_before[0][0] if missing_before else None
    extracted = _extract_data_from_message(incoming_message, user_row, last_asked_field=last_asked)

    non_null = {k: v for k, v in extracted.items() if v is not None}
    if non_null:
        _store_extracted_data(user_row.id, extracted)

    # Re-fetch after potential updates
    session = get_session()
    try:
        user_row = session.get(UserModel, user.id)
    finally:
        session.close()

    missing_after = _get_missing_fields(user_row)
    system_prompt = _build_system_prompt(user_row)

    if not missing_after:
        # All fields collected — present confirmation
        summary = _build_confirmation_summary(user_row)

        instruction = (
            f"You've collected all the data you need. Present this summary to the user "
            f"and ask if it sounds right:\n\n{summary}\n\n"
            f"If the user also said something in their last message that needs acknowledging, "
            f"acknowledge it first. Then present the summary. One message."
        )
        text = _generate(system_prompt, instruction)
        send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
        logger.info(f"Onboarding confirmation presented to {user_row.name}")
        return False

    else:
        # Still missing fields — ask about the next one
        next_field = missing_after[0]

        if non_null:
            acknowledged_fields = ", ".join(non_null.keys())
            instruction = (
                f"The user just provided: {incoming_message}\n"
                f"You extracted and stored: {acknowledged_fields}.\n\n"
                f"Briefly acknowledge what they shared (one sentence). "
                f"Then ask about: {next_field[1]}.\n"
                f"One question only. Keep it casual and natural."
            )
        else:
            instruction = (
                f"The user said: \"{incoming_message}\"\n"
                f"This didn't contain data you needed. They might be asking a question "
                f"or making a comment.\n\n"
                f"First, respond to what they said (answer their question, acknowledge their comment). "
                f"Then naturally ask about: {next_field[1]}.\n"
                f"One message, brief."
            )

        text = _generate(system_prompt, instruction)
        send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
        logger.info(f"Onboarding — asked about {next_field[0]} for {user_row.name} ({len(missing_after)} fields remaining)")
        return False


def _complete_onboarding(user, incoming_message: str) -> bool:
    """Finalize onboarding — calculate targets, store confirmed decisions, schedule, send kickoff message."""
    from models import get_session, User as UserModel
    from scheduler import schedule_user

    targets = calculate_targets(user)

    session = get_session()
    try:
        user_row = session.get(UserModel, user.id)

        user_row.calorie_target = targets["calories"]
        user_row.protein_target = targets["protein"]
        user_row.confirmed_goal_priority = targets.get("goal_label", user_row.goal)
        user_row.onboarding_step = 2  # complete

        session.commit()
        logger.info(f"Onboarding complete for {user_row.name} — {targets['calories']} cal, {targets['protein']}g protein")

        try:
            schedule_user(user_row)
        except Exception as e:
            logger.error(f"Scheduling failed for {user_row.name}: {e}")

        system_prompt = _build_system_prompt(user_row)
        instruction = (
            f"The user just confirmed their plan. Onboarding is complete.\n"
            f"Targets: {targets['calories']} cal, {targets['protein']}g protein daily.\n"
            f"Send ONE brief message that:\n"
            f"1. Confirms everything is locked in\n"
            f"2. Tells them when they'll hear from you next (based on their wake_time: {user_row.wake_time})\n"
            f"3. Feels like the starting gun — they now have a coach\n"
            f"No explanations. No previews of features. Just confidence."
        )
        text = _generate(system_prompt, instruction)
        send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")

    finally:
        session.close()

    return True
