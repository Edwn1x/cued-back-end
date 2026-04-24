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
    ("avg_steps", "average daily step count — if they don't know, tell them where to check: iPhone Health app under Browse > Activity > Steps, or Google Fit / Samsung Health on Android"),
    ("workout_days", "how many days per week they can train"),
    ("workout_time", "what time they prefer to work out"),
    ("current_split", "whether they already have a workout routine — PPL, upper/lower, full body, bro split, or if they need one built"),
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
    if user.avg_steps is None:
        missing.append(("avg_steps", "average daily step count — if they don't know, tell them: iPhone Health app (Browse > Activity > Steps), or Google Fit / Samsung Health on Android. Most phones track this automatically."))
    if not user.workout_days:
        missing.append(("workout_days", "how many days per week they can train"))
    if not user.workout_time:
        missing.append(("workout_time", "what time they prefer to work out"))
    if user.current_split is None:
        # Auto-fill "none" for users with no experience — skip the question entirely
        if user.experience == "none":
            _auto_fill_current_split_none(user)
        else:
            missing.append(("current_split", "whether they already have a workout routine they follow — ask neutrally: 'Do you already have a routine, or do you want me to build one?' If they have one, ask what the split is (PPL, upper/lower, full body, bro split, etc.)"))
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


def _auto_fill_current_split_none(user) -> None:
    """
    Write current_split = 'none' to the DB for users who indicated they don't
    currently train. Called when experience == 'none' so we skip the split
    question — there's nothing to ask about.
    """
    from models import get_session, User as UserModel
    session = get_session()
    try:
        user_row = session.get(UserModel, user.id)
        if user_row and user_row.current_split is None:
            user_row.current_split = "none"
            session.commit()
            # Update the in-memory object so _get_missing_fields sees the new value
            user.current_split = "none"
            logger.info(f"Auto-filled current_split=none for {user_row.name} (experience=none)")
    except Exception as e:
        logger.error(f"Failed to auto-fill current_split for user {user.id}: {e}")
    finally:
        session.close()


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
- CRITICAL: Never advance to the next onboarding step until you have fully responded to the user's most recent message. If the user asked a question, answer it completely. If the user made a comment or observation, acknowledge it. The user should never feel ignored or skipped over.
- If the user asks a question instead of answering yours, answer their question FIRST — fully and directly — then circle back to your question.
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
  "activity_level": "short phrase describing their activity, e.g. 'sedentary', 'lightly active', 'active — walks 8-10k steps, mix of sitting and moving', 'very active — physical job'" or null,
  "workout_days": "comma separated days like mon,tue,wed,thu,fri" or number like "4" or null,
  "workout_time": "HH:MM in 24h format" or "description like afternoon, morning" or null,
  "diet": "omnivore, vegetarian, vegan, pescatarian, keto, halal, kosher" or null,
  "cooking_situation": "cook_myself, dining_hall, mostly_eat_out, mix" or null,
  "injuries": "description of injuries" or "none" or null,
  "wake_time": "HH:MM in 24h format" or null,
  "wake_time_alt": "HH:MM in 24h format" or null,
  "wake_days_alt": "comma-separated day abbreviations that use the alt wake time, e.g. 'mon,wed,fri'" or null,
  "sleep_time": "HH:MM in 24h format" or null,
  "existing_tools": "comma separated app/device names" or "none" or null,
  "tools_decision": "integrate" or "acknowledged" or "none" or null,
  "avg_steps": integer (daily step count) or null,
  "current_split": "ppl" or "upper_lower" or "full_body" or "bro_split" or "custom" or "none" or null
}}

tools_decision rules:
- "none" → user has no tools (existing_tools="none")
- "integrate" → tool has data Cued can directly reference (e.g. Apple Health, Garmin, Oura)
- "acknowledged" → tool exists but Cued can't pull data from it (e.g. Nike Run Club, MyFitnessPal, Strava)
- null → user didn't mention tools in this message

avg_steps rules:
- "around 8k" or "like 8000" → 8000
- "8000-10000" or "8-10k" → 9000 (midpoint)
- "I don't track that" or "no idea" → null (do not block onboarding — move on)
- "not many" → null
- Only extract a number if clearly stated; otherwise null

current_split rules:
- "PPL" / "push pull legs" → "ppl"
- "upper lower" / "upper/lower" → "upper_lower"
- "full body" / "full body 3x" → "full_body"
- "bro split" / "chest day, arm day" → "bro_split"
- "yeah I have a routine" / "I follow [specific program name]" → "custom"
- "no" / "nah" / "I need one" / "build me one" → "none"
- null → user didn't answer this question in this message

Examples:
"I'm 5'7 and 145 lbs" → {{"height_ft": 5, "height_in": 7, "weight_lbs": 145, ...rest null}}
"I can do 4 days a week, usually around 5pm" → {{"workout_days": "4", "workout_time": "17:00", ...rest null}}
"I cook most of the time but eat out on weekends" → {{"cooking_situation": "mix", ...rest null}}
"I mostly buy my own groceries and cook but sometimes grab something on the way" → {{"cooking_situation": "mix", ...rest null}}
"I cook for myself" → {{"cooking_situation": "cook_myself", ...rest null}}
"I eat at the dining hall" → {{"cooking_situation": "dining_hall", ...rest null}}
"no injuries" → {{"injuries": "none", ...rest null}}
"I use Strava and Apple Watch" → {{"existing_tools": "strava,apple_watch", "tools_decision": "acknowledged", ...rest null}}
"Does Nike Run Club count?" → {{"existing_tools": "nike_run_club", "tools_decision": "acknowledged", ...rest null}}
"nah I don't use anything" → {{"existing_tools": "none", "tools_decision": "none", ...rest null}}
"idk" → {{all null}}

Short answer rules:
- "No", "nah", "nope", "none", "I don't think so" when asked about injuries → injuries="none"
- "No", "nah", "nope", "none" when asked about diet/restrictions → diet="omnivore"
- "No", "nah", "none", "nothing" when asked about existing_tools → existing_tools="none", tools_decision="none"
- "No" when asked about cooking_situation → ambiguous, return null (coach should follow up)
- Single number like "5" → map to whatever field was just asked about, not height

wake_time / wake_time_alt / wake_days_alt rules:
- "I wake up at 10" → wake_time="10:00", wake_time_alt=null, wake_days_alt=null
- "10 on tues thurs, 12 on mon wed fri" → wake_time="10:00", wake_time_alt="12:00", wake_days_alt="mon,wed,fri"
- "around 8 on weekdays, 10 on weekends" → wake_time="08:00", wake_time_alt="10:00", wake_days_alt="sat,sun"
- "7am except friday when I sleep in till 9" → wake_time="07:00", wake_time_alt="09:00", wake_days_alt="fri"
- Always put the EARLIER time as wake_time (primary), later time as wake_time_alt
- wake_days_alt lists which days use the LATER/alt time

Activity level — always extract something if the user described their daily movement. Use a short, plain-English phrase. Examples:
- "desk job, mostly sitting" → "sedentary — desk job, mostly sitting"
- "walk to class, mostly sitting" → "lightly active — walks to class, mostly sedentary"
- "mix of walking and sitting, some movement" → "lightly active — mix of walking and sitting"
- "8k+ steps, plays basketball, walks a lot" → "active — 8-10k steps, basketball"
- "physical job, on feet all day" → "very active — on feet all day"
- Never return null if the user described their activity, even vaguely"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
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
        if data.get("wake_time_alt") and not user.wake_time_alt:
            user.wake_time_alt = data["wake_time_alt"]
            changed = True
        if data.get("wake_days_alt") and not user.wake_days_alt:
            user.wake_days_alt = data["wake_days_alt"]
            changed = True
        if data.get("sleep_time") and not user.sleep_time:
            user.sleep_time = data["sleep_time"]
            changed = True
        if data.get("existing_tools") is not None and user.existing_tools is None:
            user.existing_tools = data["existing_tools"]
            changed = True
        if data.get("tools_decision") is not None and not user.tools_decision:
            user.tools_decision = data["tools_decision"]
            changed = True
        if data.get("activity_level") and (not user.activity_level or user.activity_level == "lightly_active"):
            user.activity_level = data["activity_level"]
            changed = True
        if data.get("avg_steps") is not None and user.avg_steps is None:
            user.avg_steps = int(data["avg_steps"])
            changed = True
        if data.get("current_split") is not None and user.current_split is None:
            user.current_split = data["current_split"]
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
        "endurance": "running / endurance",
        "strength": "getting stronger",
    }
    goal_label = goal_map.get(user.goal, user.goal.replace("_", " "))

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


def _maybe_auto_fill_no_training(user, message: str) -> None:
    """
    If the user's message contains clear no-training signals, write
    current_split = 'none' immediately so we skip the question.
    Only fires when current_split is still NULL.
    """
    import re as _re
    no_training_patterns = [
        r"\bi (don'?t|do not|never|haven'?t) (work out|workout|go to the gym|train|exercise|lift|lift weights)\b",
        r"\bi'?ve never (been to|gone to|stepped in) (a |the )?gym\b",
        r"\bi'?m not (currently |really )?(training|working out|lifting)\b",
        r"\bi don'?t have (a |any )?(routine|program|split|workout plan)\b",
        r"\bi need (you to build|a routine|a program|one built)\b",
        r"\bbuild me (a |one|a routine|a program)\b",
        r"\bi'?m (starting fresh|starting from scratch|brand new to (the gym|lifting|working out))\b",
    ]
    msg_lower = message.lower()
    for pattern in no_training_patterns:
        if _re.search(pattern, msg_lower):
            from models import get_session, User as UserModel
            session = get_session()
            try:
                user_row = session.get(UserModel, user.id)
                if user_row and user_row.current_split is None:
                    user_row.current_split = "none"
                    session.commit()
                    user.current_split = "none"
                    logger.info(f"Auto-filled current_split=none for {user_row.name} (no-training signal detected)")
            except Exception as e:
                logger.error(f"Failed to auto-fill current_split for user {user.id}: {e}")
            finally:
                session.close()
            break


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

    # Auto-fill current_split="none" if user explicitly indicated no training
    # before we ask them the question (Fix 2)
    if user_row.current_split is None:
        _maybe_auto_fill_no_training(user_row, incoming_message)
        # Re-fetch to pick up any write
        session = get_session()
        try:
            user_row = session.get(UserModel, user.id)
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
        msg_lower = incoming_message.lower().strip()
        is_confirmed = any(kw in msg_lower for kw in confirmation_keywords)

        # Check if the message also contains a question — if so, answer it first
        # before completing onboarding (Fix 1)
        import re as _re
        has_question = (
            "?" in incoming_message
            or bool(_re.search(r'\b(should i|can i|do i|will i|is it|what (should|do|can|is|are)|how (do|can|should|long|much|many)|when (should|do|can|will)|why (do|should|is|are))\b', msg_lower))
        )

        if is_confirmed and has_question:
            # Confirm AND answer the question before locking in
            summary = _build_confirmation_summary(user_row)
            system_prompt = _build_system_prompt(user_row)
            instruction = (
                f"Do NOT greet the user — you already said hello earlier.\n\n"
                f"The user just confirmed their plan but also asked a question: \"{incoming_message}\"\n\n"
                f"STEP 1: Answer their question directly and completely. Do not skip it or defer it.\n"
                f"STEP 2: In the same message, briefly acknowledge that their plan is confirmed.\n"
                f"STEP 3: Present this summary again so they can see it's locked:\n\n{summary}\n\n"
                f"Keep it tight. One message. End with 'sound right?' or similar."
            )
            text = _generate(system_prompt, instruction)
            send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
            # Now complete onboarding — question answered, confirmation received
            return _complete_onboarding(user_row, incoming_message)

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
                f"Do NOT greet the user — you already said hello earlier.\n\n"
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
            f"You've collected all the data you need. The user just said: \"{incoming_message}\"\n\n"
            f"STEP 1: If the user's last message asked a question or said something that deserves a response, "
            f"address it first — fully. Don't skip it.\n"
            f"STEP 2: Present this summary and ask if it sounds right:\n\n{summary}\n\n"
            f"STEP 3: After presenting the summary, briefly surface 2-3 capabilities they might not know about. "
            f"Match your tone to their age tier (check the personality skill). Examples:\n"
            f"- They can text a photo of their food and you'll break down the macros\n"
            f"- If they want a meal idea, just ask\n"
            f"- If they need a workout swap or modification, you've got them\n"
            f"Keep the whole message tight. Ask 'sound right?' or 'does that look good?' at the end."
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
                f"Do NOT greet the user — you already said hello in your first message.\n\n"
                f"The user just said: \"{incoming_message}\"\n"
                f"You extracted and stored: {acknowledged_fields}.\n\n"
                f"STEP 1: Read what they said carefully. Did they ask a question? Make a comment? "
                f"Say something that deserves a response beyond just acknowledgment? If yes, answer it fully first.\n"
                f"STEP 2: Briefly acknowledge the data they shared (one sentence).\n"
                f"STEP 3: Ask about the next thing you need: {next_field[1]}.\n"
                f"One message. Keep it natural. Never skip what they said to rush to the next question."
            )
        else:
            instruction = (
                f"Do NOT greet the user — you already said hello in your first message.\n\n"
                f"The user said: \"{incoming_message}\"\n"
                f"This didn't contain the data you needed — but before moving on, read it carefully.\n\n"
                f"STEP 1: Respond fully to what they said. If they asked a question, answer it completely. "
                f"If they made a comment, address it. Never ignore what they wrote.\n"
                f"STEP 2: Then naturally transition to asking about: {next_field[1]}.\n"
                f"One message, brief. The user should feel heard before you ask the next question."
            )

        text = _generate(system_prompt, instruction)
        send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
        logger.info(f"Onboarding — asked about {next_field[0]} for {user_row.name} ({len(missing_after)} fields remaining)")
        return False


def _finalize_onboarding_profile(user_row):
    """
    Copy profile fields to their confirmed counterparts at onboarding completion.
    Runs once inside _complete_onboarding before commit. No DB session needed —
    caller passes the already-open user_row object.
    """
    # confirmed_workout_time: copy directly from workout_time (never re-extract
    # from conversation — risk of grabbing coach's own message times)
    if user_row.workout_time and not user_row.confirmed_workout_time:
        user_row.confirmed_workout_time = user_row.workout_time

    # confirmed_training_days: only copy if workout_days contains specific days
    # (letters). If it's a count like "4" or range "3-5", leave NULL and let
    # the inference system fill it in over time.
    if user_row.workout_days and not user_row.confirmed_training_days:
        import re
        if re.search(r'[a-z]', user_row.workout_days.lower()):
            user_row.confirmed_training_days = user_row.workout_days

    # confirmed_training_split: prefer explicit current_split from onboarding conversation,
    # fall back to deriving from workout_days count
    if not user_row.confirmed_training_split:
        if user_row.current_split and user_row.current_split != "none":
            user_row.confirmed_training_split = user_row.current_split
        elif user_row.workout_days:
            try:
                import re
                nums = re.findall(r'\d+', user_row.workout_days)
                if nums:
                    count = int(nums[0])
                    if count <= 3:
                        user_row.confirmed_training_split = f"{count}x/week"
                    elif count <= 4:
                        user_row.confirmed_training_split = "upper/lower or PPL 4x"
                    elif count <= 5:
                        user_row.confirmed_training_split = "PPL or 5x/week"
                    else:
                        user_row.confirmed_training_split = f"{count}x/week"
            except Exception:
                pass


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

        # Copy profile fields to confirmed counterparts
        _finalize_onboarding_profile(user_row)

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
