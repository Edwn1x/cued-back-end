"""
Onboarding Agent — Cued
========================
Dynamic data collection through conversation. Adapts tone based on
experience level and biggest obstacle from signup.

Tracks which data points have been collected, not step numbers. Each exchange:
1. Parse user's message for any data points (Haiku extractor, given the coach's
   previous message for context)
2. Store what was found
3. Reply as the friend (prompts/identity.md): engage the specific thing they said;
   if — and only if — what they said gives a natural reason, weave in ONE
   question that would teach us one of the still-unknown fields. Never a list
   BY DEFAULT — the intake isn't a form, it's stuff you learn by caring about
   their actual day (founder, 2026-09-11). The big ask ("drop me the basics in
   one text") and the two-field bundle are KEPT for when they're appropriate
   (founder, same day): the user asks for the list, or the conversation has run
   long with most fields still unknown, or one/two fields are left to close out.
   See _intake_mode().
4. If all collected → calculate targets, present summary, confirm

The coach knows experience, goal, and obstacle from signup, which shapes HOW it
talks (tone, depth of explanation). It can search the web mid-reply (a class, a
campus place, a restaurant) — capped per reply, every query logged.
"""

import os
import json
import logging
import random
import re
import threading
from datetime import datetime, timezone, timedelta

from anthropic import Anthropic
import config
from config import ANTHROPIC_API_KEY, COACH_MODEL
from sms import send_sms
from profile_page import profile_url
from macro_calculator import calculate_targets
from cost_tracking import track as track_usage
from llm_client import make_client

logger = logging.getLogger("cued.onboarding")
client = make_client()

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")


def load_skill(skill_name: str) -> str:
    path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    try:
        with open(path) as f:
            content = f.read()
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                content = content[end + 3:].lstrip("\n")
        return content
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

# Training-specific fields — skipped for nutrition-only users
TRAINING_FIELDS = {"workout_days", "workout_time", "current_split", "injuries"}

# Hook templates for A/B testing — one is randomly assigned per new user
# Template variables: {name}, {goal_label}
# Rules: zero data collection, every template ends with a low-effort question
HOOK_TEMPLATES = [
    {
        "id": "hook_a_name",
        "text": "yo wsp, I'm your cued coach. before anything — you want to give me a name? I go by whatever you want",
    },
    {
        "id": "hook_b_casual",
        "text": "hey I'm your cued coach, how's your day going?",
    },
    {
        "id": "hook_c_fact",
        "text": "hey it's your cued coach. did you know the average person sets the same fitness goal 3 years in a row without hitting it? yeah that's not gonna be you. what's the main thing you're trying to change?",
    },
    {
        "id": "hook_d_direct",
        "text": "hey I'm your cued coach. you signed up so I know you're serious — that's already more than most people do. what made you decide to go for it?",
    },
    {
        "id": "hook_e_personalized",
        "text": "yo {name}, I'm your cued coach. just saw you signed up — what made you pull the trigger?",
    },
]

# Goal label map for use in hook templates and summaries
GOAL_LABELS = {
    "fat_loss": "losing fat",
    "muscle_building": "building muscle",
    "fat_loss,muscle_building": "recomp",
    "muscle_building,fat_loss": "recomp",
    "general_fitness": "getting healthier",
    "endurance": "building endurance",
    "strength": "getting stronger",
}


def _goal_label(goal_str: str) -> str:
    return GOAL_LABELS.get(goal_str or "", (goal_str or "your goal").replace("_", " "))


def _determine_coaching_branch(user) -> str:
    """Return 'training_nutrition' or 'nutrition_only' based on the user's goal."""
    training_goals = {"muscle_building", "strength", "fat_loss", "general_fitness", "endurance"}
    goal_parts = set((user.goal or "").replace(" ", "").split(","))
    if goal_parts & training_goals:
        return "training_nutrition"
    return "nutrition_only"


def _is_nutrition_only(user) -> bool:
    """True when goal indicates no gym component. Used during collection before coaching_branch is set."""
    return _determine_coaching_branch(user) == "nutrition_only"


def _is_berkeley_student(user) -> bool:
    """Heuristic: True if the user appears to be a Berkeley student."""
    occ = (user.occupation or "").lower()
    if any(sig in occ for sig in ["uc berkeley", "berkeley", "cal student", "ucb"]):
        return True
    if user.year in ("freshman", "sophomore", "junior", "senior", "transfer"):
        return True
    if getattr(user, "which_gym", None) in ("rsf", "dorm"):
        return True
    return False


def _get_missing_fields(user) -> list:
    """Return list of (field_name, question_context) for fields still null."""
    missing = []
    nutrition_only = _is_nutrition_only(user)

    if not user.height_ft or not user.weight_lbs:
        missing.append(("height_weight", "height and weight"))
    if not user.occupation:
        missing.append(("occupation", "what they do — student, desk job, physical work, etc."))
    if not user.activity_level or user.activity_level == "lightly_active":
        missing.append(("activity_level", "how active they are outside the gym — sedentary desk life, walking around campus, on their feet all day"))
    if user.avg_steps is None:
        missing.append(("avg_steps", "average daily step count — if they don't know, tell them: iPhone Health app (Browse > Activity > Steps), or Google Fit / Samsung Health on Android. Most phones track this automatically."))
    if not nutrition_only:
        if not user.workout_days:
            missing.append(("workout_days", "how many days per week they can train"))
        if not user.workout_time:
            missing.append(("workout_time", "what time they prefer to work out"))
        if user.current_split is None:
            if user.experience == "none":
                _auto_fill_current_split_none(user)
            else:
                missing.append(("current_split", "whether they already have a workout routine they follow — ask neutrally: 'Do you already have a routine, or do you want me to build one?' If they have one, ask what the split is (PPL, upper/lower, full body, bro split, etc.)"))
    if not user.cooking_situation:
        missing.append(("cooking_situation", "food situation — do they cook at home, eat at a dining hall, mostly eat out, or a mix"))
    # Known once EITHER typed column is filled: a person who names what they won't
    # eat has told you their diet. Live 2026-09-22 (user 42): "I don't eat mushrooms
    # tofu and raw fish" fit no diet label → null → onboarding parked on `diet` while
    # the coach said "i think i got everything i need on u now".
    if not user.diet and not (getattr(user, "restrictions", None) or "").strip():
        missing.append(("diet", "dietary preferences or restrictions — vegetarian, vegan, allergies, halal, or no restrictions"))
    if not nutrition_only and user.injuries is None:
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


def _still_unknown(user) -> list[tuple[str, str]]:
    """(field, how a friend would come to know it) for every field still null —
    phrased as the THING, not a question, so the model asks for a reason."""
    return _get_missing_fields(user)


def _now_local(tz_str: str | None) -> datetime:
    """Wall clock in the user's timezone (one seam so evals can pin the hour —
    a 4am run otherwise makes every reply about the hour)."""
    from zoneinfo import ZoneInfo
    try:
        return datetime.now(ZoneInfo(tz_str or "America/Los_Angeles"))
    except Exception:  # bad tz string on the row — never block onboarding on it
        return datetime.now(ZoneInfo("America/Los_Angeles"))


ONBOARDING_HISTORY_LIMIT = 30  # messages of this conversation shown to the model


def _conversation_so_far(user_id: int, limit: int = ONBOARDING_HISTORY_LIMIT) -> str:
    """The onboarding conversation, oldest first, as 'them:' / 'you:' lines. Until
    2026-09-11 the onboarding model saw ONLY the current message + extracted fields —
    no memory of the previous turn. Live: it couldn't answer "how'd you know?" (it had
    no idea what it had said) and lost "quiz at 4pm" two exchanges later. A friend
    remembers what you said two texts ago."""
    from models import get_session, Message
    session = get_session()
    try:
        rows = (session.query(Message)
                .filter(Message.user_id == user_id)
                .order_by(Message.id.desc()).limit(limit).all())
    finally:
        session.close()
    lines = []
    for m in reversed(rows):
        who = "you" if m.direction == "out" else "them"
        body = (m.body or "").strip().replace("\n", " / ")
        if body:
            lines.append(f"{who}: {body[:400]}")
    return "\n".join(lines)


def _build_system_prompt(user) -> str:
    """Build the system prompt for onboarding exchanges: the shared identity,
    the safety rules, what we know, what's still unknown, and how this first
    conversation works (no intake block — one woven question at most)."""
    from agent_loop import identity_prompt
    identity = identity_prompt()
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
        f"Occupation: {user.occupation}" if user.occupation else None,
        f"Activity: {user.activity_level}" if user.activity_level else None,
        f"Avg steps: {user.avg_steps}" if user.avg_steps else None,
        f"Workout days: {user.workout_days}" if user.workout_days else None,
        f"Workout time: {user.workout_time}" if user.workout_time else None,
        f"Current split: {user.current_split}" if user.current_split else None,
        f"Diet: {user.diet}" if user.diet else None,
        f"Won't eat / restrictions: {user.restrictions}" if getattr(user, "restrictions", None) else None,
        f"Cooking: {user.cooking_situation}" if user.cooking_situation else None,
        (f"Their own daily targets: {user.calorie_target or '?'} cal / {user.protein_target or '?'}g protein"
         if getattr(user, "targets_source", None) == "user" and (user.calorie_target or user.protein_target) else None),
        f"Injuries: {user.injuries}" if user.injuries else None,
        f"Wake time: {user.wake_time}" if user.wake_time else None,
        f"Sleep time: {user.sleep_time}" if user.sleep_time else None,
        f"Existing tools: {user.existing_tools}" if user.existing_tools else None,
    ]
    profile = "\n".join(p for p in profile_parts if p)
    try:
        from workouts.routine import describe_routine
        rd = describe_routine(getattr(user, "custom_templates", None))
        if rd:
            profile += "\n\nTheir own routine (already on their workout cards — never ask for it again):\n" + rd
    except Exception as e:  # noqa: BLE001
        logger.warning("ROUTINE_PROFILE_LINE_FAILED user=%s err=%s", getattr(user, "id", None), e)
    try:
        from reminders import active_reminders, describe, _tz
        rows = active_reminders(user.id) if getattr(user, "id", None) else []
        if rows:
            tz = _tz(user.user_timezone)
            profile += ("\n\nReminders you've set (code sends these at that time — you can say "
                        "you'll ping them; never promise one that isn't listed here):\n"
                        + "\n".join(f"- {describe(r, tz)}" for r in rows))
    except Exception as e:  # noqa: BLE001
        logger.warning("REMINDER_PROFILE_LINE_FAILED user=%s err=%s", getattr(user, "id", None), e)

    now_local = _now_local(user.user_timezone)
    now_line = (now_local.strftime("%A, %b %-d, %Y, %-I:%M%p")
                .replace("AM", "am").replace("PM", "pm"))

    unknown = _still_unknown(user)
    if unknown:
        unknown_block = "\n".join(f"- {desc}" for _f, desc in unknown)
    else:
        unknown_block = "- nothing — you have what you need"

    history = _conversation_so_far(user.id) if getattr(user, "id", None) else ""
    history_block = history or "(nothing yet — you just said hey)"

    return f"""{identity}

---

{safety}

---

## RIGHT NOW
It's {now_line} in Berkeley. A friend knows what day it is — never guess the day or
the time of day; read it here.

## EXPERIENCE CALIBRATION
{experience_context}

## WHAT YOU KNOW ABOUT THEM (from signup + what they've told you)
{profile}

## STILL UNKNOWN (things you'd learn by caring about their day — never by listing them)
{unknown_block}

## THE CONVERSATION SO FAR (oldest first — you remember all of it)
{history_block}

## HOW THIS FIRST CONVERSATION WORKS
- You just met. You're getting to know a new friend, and along the way you'll end up
  knowing the things above. The intake isn't a form; it's stuff you learn by caring
  about their actual day.
- Every reply engages the specific thing they just said FIRST — the class, the place,
  the food, the feeling. Have a take on it. Be curious about it.
- Then, ONLY if what they said gives you a natural reason, ask about ONE thing from
  STILL UNKNOWN — the way a friend asks it, for a reason. "you got food at the house or
  is it dining hall today" is the food question; "you gonna hit the gym after or is
  today a wash" gets training days and time. If there's no natural reason, don't force
  one — just be the friend. The next message will give you one.
- At most ONE question per message — one thing asked, or none. A second question
  joined onto the first ("— and speaking of, …", "also …", "oh and …") is still a second
  question even with one question mark: pick ONE, drop the other. Never a list of
  questions. Never "a few things I need from you." Never a numbered or comma-separated
  set of things to answer.
- If they NAMED something specific — a class, a campus place, a restaurant, an event —
  look it up (web_search) before you reply and use ONE detail from what you find, in
  your own words, no links. A course number ("70", "cs70", "61b", "data 8") or a campus
  place is ALWAYS a search — where that class is in the semester right now (this
  semester's schedule, not memory) is the detail. That one detail is what makes you
  sound like you're there.
- If they ask you something, answer it first, fully, then be a friend about the rest.
  "how'd you know?" / "i already told you" → check THE CONVERSATION SO FAR and answer
  from it; never claim you don't have something that's written there.
- If they hand you several things at once, react to them like a person would — don't
  read a checklist back.
- Never mention fields, profiles, forms, plans you're "building," or what you "need."
- 1–3 short sentences. No `---` separators. No greeting — you already said hey.
- Decide on your ONE question (or none) BEFORE you start writing, then write the reply
  once, as a single paragraph. Never draft-then-revise inside the message, never show an
  edit, never add a second paragraph.
"""


def _last_coach_message(user_id: int) -> str | None:
    """The coach's most recent outbound text — what the user is replying to. With
    the woven-question intake there is no 'last asked field' to hand the extractor,
    so it reads the actual question instead (a bare '5' means workout days only
    if that's what was just asked)."""
    from models import get_session, Message
    session = get_session()
    try:
        row = (session.query(Message)
               .filter(Message.user_id == user_id, Message.direction == "out")
               .order_by(Message.created_at.desc(), Message.id.desc())
               .first())
        return row.body if row else None
    finally:
        session.close()


def _extract_data_from_message(user_message: str, user, last_asked_field: str = None,
                               last_coach_message: str = None) -> dict:
    """
    Use a lightweight AI call to extract any data points from the user's message.
    Returns a dict of field names to values.
    """
    missing = _get_missing_fields(user)
    if not missing:
        return {}

    missing_list = ", ".join(f[0] for f in missing)

    context_hint = ""
    if last_coach_message:
        context_hint = f"""IMPORTANT CONTEXT: The coach's previous message (what the user is replying to) was:
\"{last_coach_message.strip()[:600]}\"
If that message asked about something, the user's reply is MOST LIKELY answering it — map a
bare number or a bare yes/no to whatever was just asked (\"5\" after \"how many days can you
train\" is workout_days=\"5\", not height). If it didn't ask anything, only extract what the
user clearly volunteered.

"""
    elif last_asked_field:
        context_hint = f"""IMPORTANT CONTEXT: The coach just asked the user about: {last_asked_field}
The user's response is MOST LIKELY answering that question. Prioritize mapping their answer to that field unless the message clearly refers to something else.

For example:
- If the coach asked about workout_days and user says "5" → workout_days="5", NOT height_ft=5
- If the coach asked about workout_time and user says "5 or 6" → workout_time="17:00", NOT height or workout_days
- If the coach asked about diet and user says "no" → diet="omnivore" (no restrictions)
- If the coach asked about injuries and user says "no" → injuries="none"
- If the coach asked about existing_tools and user says "nah" → existing_tools="none"

"""

    prompt = f"""{context_hint}Extract any fitness coaching profile data from this user message. Only extract what the user CLEARLY stated ABOUT THEMSELVES AS A PATTERN.

AN ANECDOTE IS NOT A FACT. "we got malatang after", "went for pizza in sf", "had crossroads for lunch" say NOTHING about cooking_situation or diet — they are one meal, not how the person eats.
AN ASPIRATION IS NOT THE CURRENT PATTERN. "I work out after everything's done but I wanna be more of an early bird" → workout_time is the EVENING (what they do now), NOT morning (what they wish). Every field here describes how they live today; wishes and goals belong to the coach, not to these fields. Live bug: this exact message stored workout_time=08:00. Only a statement about their usual pattern counts: "I mostly cook", "I'm on the dining hall plan", "I eat out most days". Likewise one workout is not workout_days, one late night is not sleep_time, and never fill diet="omnivore" unless they were asked about restrictions and said they have none — OR they named specific foods they don't eat / said "no allergies" / described what they eat with no restriction identity (a person listing dislikes HAS told you their diet: omnivore, with the dislikes in food_dislikes). When in doubt, null — a wrong field here steers every meal suggestion for months; a null just gets asked about later.

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
  "food_dislikes": "comma-separated foods they say they don't/won't eat (mushrooms, tofu, raw fish)" or null,
  "experience": "none" (never trained) | "beginner" (under 6 months) | "intermediate" (6 months–2 years) | "advanced" (2+ years) or null — ONLY from an explicit statement about how long they've trained ("been lifting 3 years", "never really trained"); a detailed routine is NOT a statement,
  "goal": "fat_loss" | "muscle_building" | "fat_loss,muscle_building" (recomp) | "strength" | "endurance" | "general_fitness" or null — ONLY when they say what they're going for ("tryna cut", "want to put on size", "training for a half"),
  "calorie_target": integer or null (ONLY a daily calorie number THEY say they aim for or track to — "staying under 2000 cals" → 2000; never a number the coach said),
  "protein_target": integer or null (same rule — grams of protein per day THEY track to),
  "cooking_situation": "cook_myself, dining_hall, mostly_eat_out, mix" or null,
  "injuries": "description of injuries" or "none" or null,
  "wake_time": "HH:MM in 24h format" or null,
  "wake_time_alt": "HH:MM in 24h format" or null,
  "wake_days_alt": "comma-separated day abbreviations that use the alt wake time, e.g. 'mon,wed,fri'" or null,
  "sleep_time": "HH:MM in 24h format" or null,
  "existing_tools": "comma separated app/device names" or "none" or null,
  "tools_decision": "integrate" or "acknowledged" or "none" or null,
  "avg_steps": integer (daily step count) or null,
  "current_split": "ppl" or "upper_lower" or "full_body" or "bro_split" or "custom" or "none" or null,
  "year": "freshman" or "sophomore" or "junior" or "senior" or "grad" or "transfer" or null,
  "meal_plan_status": "on_meal_plan" or "no_meal_plan" or null
}}

year / meal_plan_status rules (Berkeley context — bonus facts, only when clearly stated):
- "I'm a junior" → year="junior"; "first year" / "freshman" → "freshman"; "grad student" → "grad"
- "I don't have a meal plan" / "no dining hall pass" / "not on the meal plan" → meal_plan_status="no_meal_plan"
- "I'm on the meal plan" / "I have swipes" / "dining hall pass" → meal_plan_status="on_meal_plan"
- Eating AT a dining hall once says nothing about meal_plan_status → null

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
"I have no allergies and eat mostly chicken and protein pasta" → {{"diet": "omnivore", ...rest null}}
"I don't eat mushrooms tofu and raw fish" → {{"diet": "omnivore", "food_dislikes": "mushrooms, tofu, raw fish", ...rest null}}
"I count my macros staying under 2000 cals and 155 grams of protein" → {{"calorie_target": 2000, "protein_target": 155, ...rest null}}
"been lifting like 3 years, tryna cut for summer" → {{"experience": "advanced", "goal": "fat_loss", ...rest null}}
"I do push pull legs" (no statement about how long) → {{"current_split": "ppl", ...rest null}}  (experience stays null)
"I use Strava and Apple Watch" → {{"existing_tools": "strava,apple_watch", "tools_decision": "acknowledged", ...rest null}}
"Does Nike Run Club count?" → {{"existing_tools": "nike_run_club", "tools_decision": "acknowledged", ...rest null}}
"nah I don't use anything" → {{"existing_tools": "none", "tools_decision": "none", ...rest null}}
"idk" → {{all null}}

Short answer rules:
- "No", "nah", "nope", "none", "I don't think so" when asked about injuries → injuries="none"
- "No", "nah", "nope", "none" when asked about diet/restrictions → diet="omnivore"
- "I don't eat X and Y" / "no X" (specific foods) → food_dislikes="X, Y" AND diet="omnivore" — dislikes are not a diet identity
- "no allergies" → diet="omnivore"
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
- LATE SCHEDULES: a bedtime after midnight is STILL sleep_time, even though its clock
  number is small. "I go to sleep like 2-5am and wake up 11am-2pm" → sleep_time="03:00",
  wake_time="12:00" (midpoints). NEVER assign the smaller clock number to wake_time —
  read which one they said they fall asleep at. Live bug: a 2am bedtime stored as wake.
- A range ("11am-2pm") → its midpoint in 24h ("12:30" → round to "12:00" or "13:00")

Activity level — always extract something if the user described their daily movement. Use a short, plain-English phrase. Examples:
- "desk job, mostly sitting" → "sedentary — desk job, mostly sitting"
- "walk to class, mostly sitting" → "lightly active — walks to class, mostly sedentary"
- "mix of walking and sitting, some movement" → "lightly active — mix of walking and sitting"
- "8k+ steps, plays basketball, walks a lot" → "active — 8-10k steps, basketball"
- "physical job, on feet all day" → "very active — on feet all day"
- Never return null if the user described their activity, even vaguely"""

    try:
        response = client.messages.create(
            model=config.ONBOARDING_EXTRACTOR_MODEL,
            # 1000: same sizing class as extract_and_store_decisions — a fully
            # populated field set + fences needs real headroom; truncation
            # discards the extraction.
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        track_usage(getattr(user, "id", None),
                    "onboarding.extract_data_from_message",
                    config.ONBOARDING_EXTRACTOR_MODEL, response)
        # ALL text blocks, not content[0]: Sonnet thinks by default, so the first block
        # is often a ThinkingBlock (no .text) — the live test caught the crash before it
        # shipped. Then take the outermost {...} in case the model wrapped it in prose.
        from agent_loop import _join_text
        text = _join_text(response.content).replace("```json", "").replace("```", "").strip()
        if "{" in text and "}" in text:
            text = text[text.index("{"):text.rindex("}") + 1]
        return json.loads(text)
    except Exception as e:
        logger.error(f"Onboarding data extraction failed: {e}")
        return {}


def _store_extracted_data(user_id: int, data: dict):
    """Write extracted fields to the user record.

    During onboarding the LATEST clear statement wins: a new non-null value
    overwrites an earlier one. Live bug (2026-09-11, user 27): an early
    over-inference (cooking_situation=mostly_eat_out from an anecdote) was made
    permanent by first-write-wins, and the user's explicit "I mostly cook" 30s
    later was silently dropped. The summary/confirmation step is the final check.
    Once onboarding is complete (step >= 3) nothing here runs — coaching-time
    corrections go through the coach's tools."""
    from models import get_session, User

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return
        if (user.onboarding_step or 0) >= 3:
            return

        changed = False

        def _set(attr, value):
            nonlocal changed
            if value is not None and getattr(user, attr) != value:
                setattr(user, attr, value)
                changed = True

        _EXPERIENCE = {"none", "beginner", "intermediate", "advanced"}
        _GOALS = {"fat_loss", "muscle_building", "fat_loss,muscle_building", "muscle_building,fat_loss",
                  "strength", "endurance", "general_fitness"}
        if data.get("experience") in _EXPERIENCE:
            _set("experience", data["experience"])
        if isinstance(data.get("goal"), str) and data["goal"].strip().lower() in _GOALS:
            _set("goal", data["goal"].strip().lower())
        for key in ("height_ft", "height_in", "weight_lbs", "occupation", "diet",
                    "cooking_situation", "injuries", "wake_time", "wake_time_alt",
                    "wake_days_alt", "sleep_time", "existing_tools", "tools_decision",
                    "activity_level", "current_split", "year", "meal_plan_status"):
            if key in data and data.get(key) is not None:
                val = data[key]
                if key == "weight_lbs" and not val:
                    continue
                _set(key, val)
        if data.get("workout_days"):
            _set("workout_days", str(data["workout_days"]))
        if data.get("workout_time"):
            wt = data["workout_time"]
            time_map = {"morning": "08:00", "afternoon": "14:00", "evening": "18:00"}
            if isinstance(wt, str) and wt.lower() in time_map:
                wt = time_map[wt.lower()]
            _set("workout_time", wt)
        if data.get("avg_steps") is not None:
            _set("avg_steps", int(data["avg_steps"]))
        # Foods they won't eat → the typed restrictions column (the coach prompt and
        # the nutrition agent both read it; memory is told NOT to store these).
        if data.get("food_dislikes"):
            from memory import _append_to_restrictions
            items = [s.strip() for s in str(data["food_dislikes"]).split(",") if s.strip()]
            merged = _append_to_restrictions(user.restrictions, [f"won't eat {i}" for i in items])
            if merged != (user.restrictions or ""):
                user.restrictions = merged
                changed = True
        # Targets THEY track to. Stored raw with source=user; the summary step bounds
        # them (±15% of computed) once height/weight/goal are all known. Never
        # overwrites a code-computed pair.
        if getattr(user, "targets_source", None) != "computed":
            for key, attr in (("calorie_target", "calorie_target"), ("protein_target", "protein_target")):
                val = data.get(key)
                if val is None:
                    continue
                try:
                    val = int(round(float(val)))
                except (TypeError, ValueError):
                    continue
                if val > 0 and getattr(user, attr) != val:
                    setattr(user, attr, val)
                    user.targets_source = "user"
                    changed = True

        if changed:
            session.commit()
            logger.info(f"Stored onboarding data for {user.name}: "
                        f"{ {k: v for k, v in data.items() if v is not None} }")
    finally:
        session.close()


def _send_capability_rundown(user_row, system_prompt: str) -> bool:
    """The 'oh and quick rundown of how i work' bubble. Content comes from the
    registry (only what's ON for this user, ranked by their profile); the model
    writes it in the friend's voice. Six lines max, no list formatting."""
    if not config.ONBOARDING_RUNDOWN_ENABLED:
        return False
    from capabilities import rundown_context
    ctx = rundown_context(user_row)
    instruction = (
        "You just sent the 'locked in' message. Now send ONE more bubble, a beat later, "
        "that tells them how to actually use you — like a friend adding 'oh and'. Open with "
        "something like 'oh and quick rundown of how i work'. Use ONLY what's below; do not "
        "mention anything else you can do. No bullet points, no numbered list, no headers, "
        "no bold — plain sentences, 4 to 6 short lines total, their words. Do not repeat "
        "the targets or the profile link (they just got both). No question at the end.\n\n"
        f"{ctx}"
    )
    if config.ONBOARDING_RUNDOWN_DELAY_S > 0:
        import time
        time.sleep(config.ONBOARDING_RUNDOWN_DELAY_S)
    text = _generate(system_prompt, instruction, user_id=user_row.id)
    if not text:
        return False
    send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
    logger.info("ONBOARDING_RUNDOWN_SENT user=%s chars=%d", user_row.id, len(text))
    return True


def _extract_target_request(message: str, user) -> dict:
    """{"calories": int|None, "protein": int|None} — the numbers the user ASKED FOR as
    targets in this message, or {} if they didn't name any. Extractor model, JSON
    only. A number that is a fact (weight, age, days) is NOT a target."""
    prompt = (
        "The user is reacting to proposed daily targets during onboarding. Return ONLY JSON: "
        '{"calories": <int or null>, "protein": <int or null>, "maintenance": <int or null>} with '
        "the values they are ASKING FOR as their targets, and `maintenance` ONLY when they cite a "
        "maintenance/TDEE number from their own app or prior tracking. null when they didn't name "
        "one. Examples:\n"
        '"how about 2200 and we up the protein to like 150g?" → {"calories": 2200, "protein": 150, "maintenance": null}\n'
        '"can we do 2k" → {"calories": 2000, "protein": null, "maintenance": null}\n'
        '"150 protein sounds better" → {"calories": null, "protein": 150, "maintenance": null}\n'
        '"my app says i maintain at like 2200 so 1700 makes more sense" → {"calories": 1700, "protein": null, "maintenance": 2200}\n'
        '"mynetdiary has my tdee at 2150" → {"calories": null, "protein": null, "maintenance": 2150}\n'
        '"thats too much food" → {"calories": null, "protein": null, "maintenance": null}\n'
        '"actually im 145 lbs not 139" → {"calories": null, "protein": null, "maintenance": null}\n\n'
        f'Message: "{message}"'
    )
    try:
        response = client.messages.create(model=config.ONBOARDING_EXTRACTOR_MODEL, max_tokens=200,
                                          messages=[{"role": "user", "content": prompt}])
        track_usage(getattr(user, "id", None), "onboarding.extract_target_request",
                    config.ONBOARDING_EXTRACTOR_MODEL, response)
        from agent_loop import _join_text
        text = _join_text(response.content).replace("```json", "").replace("```", "").strip()
        if "{" in text and "}" in text:
            text = text[text.index("{"):text.rindex("}") + 1]
        data = json.loads(text)
        out = {}
        for k in ("calories", "protein", "maintenance"):
            v = data.get(k) if isinstance(data, dict) else None
            if isinstance(v, (int, float)) and v > 0:
                out[k] = int(v)
        return out
    except Exception as e:  # noqa: BLE001 — a failed parse just means "no request"
        logger.warning("TARGET_REQUEST_EXTRACT_FAILED user=%s err=%s", getattr(user, "id", None), e)
        return {}


_STATIC_PROMPT_END = "## RIGHT NOW"


def _cacheable_system(system_prompt: str):
    """Split the onboarding system prompt into a cached static head (identity +
    safety — identical on every turn for every user) and the per-turn tail (clock,
    profile, unknowns, history). Live 2026-09-22 (user 42): 11 turns × ~16.5k input
    tokens with cache_read=0 on every one — the static head is ~80% of that."""
    head, sep, tail = system_prompt.partition(_STATIC_PROMPT_END)
    if not sep or len(head) < 1024:
        return system_prompt  # no marker (tests / odd callers) → plain string, as before
    return [
        {"type": "text", "text": head, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": sep + tail},
    ]


def _generate(system_prompt: str, instruction: str, user_id: int = None) -> str:
    """One onboarding reply. The model may search the web mid-reply (server-side
    tool, capped per reply by WEB_SEARCH_MAX_USES) when something specific came up
    — a class, a campus place, a restaurant. pause_turn (the server tool hit its
    iteration limit) is resumed by re-sending the content; every query is logged
    with the user id (WEB_SEARCH_QUERY); ALL text blocks are joined (search splits
    the reply across several). Not cached at the prompt level: each caller's
    system prompt carries the user's current onboarding state."""
    from agent_loop import _join_text
    from agent_tools import log_web_search_queries

    tools = None
    if config.WEB_SEARCH_TOOL_ENABLED:
        from agent_tools import WEB_SEARCH_TOOL
        tools = [WEB_SEARCH_TOOL]

    messages = [{"role": "user", "content": instruction}]
    last_text = ""
    for _ in range(config.AGENT_LOOP_MAX_TOOL_ITERS):
        # Adaptive thinking at low effort, like the coach loop: with thinking OFF,
        # Opus 4.8 writes its reasoning into the visible reply ("So Nau's up early
        # (or hasn't slept).") and narrates searches. The ceiling is the loop's
        # (thinking + search + reply share it), not the SMS reply cap.
        # Effort MEDIUM (the coach loop runs low): at low the model emits a second
        # question and then corrects itself IN the visible reply ("Wait - that's
        # two questions. Let me fix it:"). Onboarding is one conversation per user;
        # the extra thinking is cheap and the first impression is the product.
        kwargs = dict(model=COACH_MODEL, max_tokens=config.AGENT_LOOP_MAX_TOKENS,
                      thinking={"type": "adaptive"}, output_config={"effort": "medium"},
                      system=_cacheable_system(system_prompt), messages=messages)
        if tools:
            kwargs["tools"] = tools
        response = client.messages.create(**kwargs)
        track_usage(user_id, "onboarding.generate", COACH_MODEL, response)
        log_web_search_queries(user_id, response.content, "onboarding.generate")

        stop = getattr(response, "stop_reason", None)
        if stop == "pause_turn":
            messages.append({"role": "assistant", "content": response.content})
            continue
        text = _join_text(response.content)
        if text:
            last_text = text
        if stop == "max_tokens":
            logger.warning("ONBOARDING_TRUNCATED user=%s stop=max_tokens max_tokens=%d",
                           user_id, config.AGENT_LOOP_MAX_TOKENS)
        return last_text
    logger.warning("ONBOARDING_MAX_ITERS user=%s — returning best-effort reply", user_id)
    return last_text


def _reconcile_user_targets(user_id: int) -> str | None:
    """Targets the user stated mid-conversation ("staying under 2000 cals") were
    stored raw with targets_source='user'. Now that the profile is complete, bound
    them like the adjust turn does (±15% of computed): inside the band they stand;
    outside, the nearest end of the band is written and the summary says so.
    Returns the clamp note for the summary, or None when nothing was clamped."""
    from models import get_session, User as UserModel
    from macro_calculator import apply_target_override, override_bounds
    session = get_session()
    try:
        u = session.get(UserModel, user_id)
        if not u or getattr(u, "targets_source", None) != "user":
            return None
        asked_cal, asked_pro = u.calorie_target, u.protein_target
    finally:
        session.close()
    if not asked_cal and not asked_pro:
        return None
    r = apply_target_override(user_id, calories=asked_cal, protein=asked_pro, note="stated during onboarding")
    rejected = r.get("rejected") or {}
    if not rejected:
        return None
    notes = []
    session = get_session()
    try:
        u = session.get(UserModel, user_id)
        for field, rj in rejected.items():
            if "min" not in rj:
                continue
            lo, hi, asked = rj["min"], rj["max"], rj["asked"]
            nearest = lo if asked < lo else hi
            if field == "calories":
                u.calorie_target = nearest
            else:
                u.protein_target = nearest
            unit = " cal" if field == "calories" else "g protein"
            notes.append(f"you said {asked}{unit}; {nearest}{unit} is as {'low' if asked < lo else 'high'} as I'll go "
                         f"for your stats (I'd have set {rj['computed']}{unit})")
        u.calorie_target_computed = r["computed"]["calories"]
        u.protein_target_computed = r["computed"]["protein"]
        u.targets_source = "user"
        session.commit()
    finally:
        session.close()
    if notes:
        logger.info("TARGETS_USER_CLAMPED user=%s %s", user_id, "; ".join(notes))
    return "; ".join(notes) or None


def _build_confirmation_summary(user, clamp_note: str | None = None) -> str:
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

    # wake/sleep are in the summary on purpose: they drive when the coach is allowed
    # to text. Live (user 27): a 2am bedtime was stored as the WAKE time and the old
    # summary didn't show it, so the one place the user could catch it was blind.
    sleep_bit = ""
    if user.wake_time or user.sleep_time:
        sleep_bit = (f" Up around {user.wake_time or '?'}, asleep around {user.sleep_time or '?'}"
                     f" — that's when I'll know to leave you alone.")
    if clamp_note:
        targets_bit = (f"On targets: {clamp_note}. So {user.calorie_target} cal and "
                       f"{user.protein_target}g protein daily. ")
    elif getattr(user, "targets_source", None) == "user" and user.calorie_target and user.protein_target:
        targets_bit = (f"You picked {user.calorie_target} cal and {user.protein_target}g protein daily "
                       f"(I'd have set {targets['calories']}/{targets['protein']}g). ")
    else:
        targets_bit = f"I'm setting you at {targets['calories']} cal and {targets['protein']}g protein daily. "
    # Only state fields the user actually gave — a fact they didn't give is a trust
    # break (live: "20 years old" recited when age was never asked). Weight as an int,
    # never 137.0.
    who = f"{height_str}, {int(user.weight_lbs)} lbs" if user.weight_lbs else height_str
    if user.age:
        who += f", {user.age}"
    exp_map = {"none": "just starting out", "beginner": "under 6 months of training",
               "intermediate": "6 months to 2 years of training", "advanced": "2+ years of training"}
    exp_bit = f", {exp_map[user.experience]}" if getattr(user, "experience", None) in exp_map else ""
    routine_bit = ""
    try:
        from workouts.routine import describe_routine
        if describe_routine(getattr(user, "custom_templates", None)):
            routine_bit = " Your own routine is on your workout cards. "
    except Exception:  # noqa: BLE001
        routine_bit = ""
    return (
        f"Here's what I'm working with: {who}. "
        f"Goal is {goal_label}{exp_bit}. Training {user.workout_days} days/week around {user.workout_time}."
        f"{routine_bit}"
        f"{sleep_bit} "
        f"{targets_bit}"
        f"Sound right?"
    )


# Live 2026-09-22 (user 42): with `diet` still unknown the bundle reply ended "i think
# i got everything i need on u now" — and the next inbound re-asked it. Stated in
# every intake builder so the model can't close a conversation code hasn't closed.
_NOT_DONE_LINE = ("You are NOT done getting to know them yet — never say you're done, that "
                  "you've got all you need, or that you're 'off their back'; the conversation "
                  "closes only when nothing is still unknown, and code decides that, not you. ")


def _build_friend_reply(user, incoming_message: str, system_prompt: str,
                        missing_fields: list) -> str:
    """The onboarding reply: engage the specific thing they said; if there's a
    natural reason, weave in ONE question from STILL UNKNOWN. This replaces both
    the eight-question big ask and the two-field gap bundling — a friend never
    sends either."""
    unknown = ", ".join(f[1].split(" — ")[0] for f in missing_fields) or "nothing"
    instruction = (
        f"{user.name} just texted you: \"{incoming_message}\"\n\n"
        f"Reply as the friend. React to the specific thing they said — have a take, be "
        f"curious about it. If they named a class (a course number always counts), a campus "
        f"place, a restaurant, or an event, search it first and use one detail from this "
        f"semester. If (and only if) what they said gives "
        f"you a natural reason, work in ONE question that would tell you one of these you "
        f"still don't know: {unknown}. If there's no natural reason, don't force one. One "
        f"message, one paragraph, no greeting, ONE question at most — pick it before you "
        f"write, and don't join a second one on with 'and speaking of' / 'also' / 'oh and'. "
        f"Never a second paragraph, never a visible edit. {_NOT_DONE_LINE}"
    )
    return _generate(system_prompt, instruction, user_id=user.id)


# ─── When is a list appropriate? (founder: keep the big ask + bundle for that) ──
# Default is the friend reply. These are the exceptions, all code-decided:
BIG_ASK_AFTER_TURNS = 6    # coach replies so far, with >= BIG_ASK_MIN_UNKNOWN still unknown
BIG_ASK_MIN_UNKNOWN = 3
BUNDLE_AFTER_TURNS = 4     # coach replies so far, with <= 2 unknown → close it out in one ask
_ASKS_FOR_THE_LIST = re.compile(
    r"\b(what (do|else do|all do) (you|u) need|what (info|information|details?) (do you|do u|you|u) (need|want)"
    r"|what should i (send|tell|give) (you|u)|just (ask|tell) me (what|everything)"
    r"|(send|give) me the (list|questions)|what else (do you|do u|you|u) (need|want)"
    r"|ask me (the|your) questions|hit me with (the|your) questions|what('s| is) the (list|form))\b",
    re.IGNORECASE,
)


BIG_ASK_MESSAGE_TYPE = "onboarding_bigask"  # the one-text ask, marked so it can't repeat


def _big_ask_sent(user_id: int) -> bool:
    """Has the one-text big ask already gone out this onboarding? Live 2026-09-22
    (user 42): three consecutive big asks, each opening "alr real talk, just drop me
    the basics in one text" — the list is a one-time move, not a mode."""
    from models import get_session, Message
    session = get_session()
    try:
        return bool(session.query(Message.id)
                    .filter(Message.user_id == user_id, Message.direction == "out",
                            Message.message_type == BIG_ASK_MESSAGE_TYPE).first())
    finally:
        session.close()


def _coach_turns(user_id: int) -> int:
    """Conversational turns so far = coach onboarding replies already sent, hook
    excluded. NOT inbound rows: people text in bursts ("Nah I lwk got plans" /
    "pizza in sf" / "at this Tonys place" / "apparently its really good?") and the
    buffer folds a burst into ONE turn — counting rows fired the big ask on the
    founder's second exchange (live, 2026-09-11 14:13 UTC)."""
    from models import get_session, Message
    session = get_session()
    try:
        outs = (session.query(Message)
                .filter(Message.user_id == user_id, Message.direction == "out",
                        Message.message_type.in_(("onboarding", BIG_ASK_MESSAGE_TYPE))).count())
        return max(0, outs - 1)  # the hook is not a reply
    finally:
        session.close()


def _intake_mode(incoming_message: str, missing_fields: list, turns: int,
                 big_ask_sent: bool = False) -> str:
    """'friend' (default) | 'big_ask' | 'bundle'.
    big_ask — they asked for the list, or the coach has already replied BIG_ASK_AFTER_TURNS+
              times with BIG_ASK_MIN_UNKNOWN+ fields still unknown (a friend would say
              "alr real talk, let me just get the basics" rather than fish forever).
              ONCE per onboarding unless they ask again: after it's been sent, what's
              still missing comes back through the friend reply / the bundle.
    bundle  — one or two fields left after BUNDLE_AFTER_TURNS+ turns (or they asked):
              close it out in one natural ask instead of stretching two more replies.
    Anything else is the friend reply."""
    n = len(missing_fields)
    if n == 0:
        return "friend"  # nothing to ask for — never a list
    asked = bool(_ASKS_FOR_THE_LIST.search(incoming_message or ""))
    if n <= 2 and n > 0 and (asked or turns >= BUNDLE_AFTER_TURNS):
        return "bundle"
    if asked:
        return "big_ask"
    if big_ask_sent:
        return "friend"
    if turns >= BIG_ASK_AFTER_TURNS and n >= BIG_ASK_MIN_UNKNOWN:
        return "big_ask"
    return "friend"


def _build_big_ask_message(user, incoming_message: str, system_prompt: str, missing_fields: list) -> str:
    """The one-text ask for everything still unknown — used only when _intake_mode
    says it's appropriate (they asked for it, or the conversation has run long).
    Same friend voice: react to what they said first, then one natural ask."""
    fields_hint = ", ".join(f[1].split(" — ")[0] for f in missing_fields)
    instruction = (
        f"{user.name} just texted you: \"{incoming_message}\"\n\n"
        f"STEP 1 (required): react to the specific thing they said, like a friend. If they "
        f"asked what you need, that's your cue — no apology, no preamble.\n\n"
        f"STEP 2: ask them to drop the basics in ONE text: {fields_hint}. Frame it the way "
        f"a friend would, in your own words — not a stock line. Name what to cover in plain "
        f"words, not a numbered list. ONLY the things listed here: anything they already told "
        f"you is not on this list, so don't re-ask it. If THE CONVERSATION SO FAR shows you "
        f"already asked for 'the basics' once, do not reopen with that same line — just name "
        f"the specific bits still missing. 3-4 sentences max. No greeting. "
        f"This is the ONE time a list of things is okay; make it feel like one ask. {_NOT_DONE_LINE}"
    )
    return _generate(system_prompt, instruction, user_id=user.id)


def _bundle_gap_questions(missing_fields: list, user, incoming_message: str, system_prompt: str) -> str:
    """Close out the last one or two unknowns in a single natural ask — used only
    when _intake_mode says so (late in the conversation, or they asked)."""
    gap_descriptions = [f[1].split(" — ")[0] for f in missing_fields[:2]]
    gaps_str = " and ".join(gap_descriptions)
    instruction = (
        f"{user.name} just texted you: \"{incoming_message}\"\n\n"
        f"STEP 1: react to what they said like a friend (answer any question fully).\n"
        f"STEP 2: you're basically done getting to know them — ask about {gaps_str} in one "
        f"short, natural line ('last thing' energy), both in one breath if there are two. "
        f"Not a form. 1-2 sentences. No greeting. {_NOT_DONE_LINE}"
    )
    return _generate(system_prompt, instruction, user_id=user.id)


def send_onboarding_hook(user_id: int, *, reason: str = "signup") -> bool:
    """Send the hook (first coaching text) NOW, synchronously, and move the user to
    step 1. Idempotent: a user already past step 0 gets nothing. Registers them
    with Photon first (flag-gated, idempotent) so the hook can go blue; on the
    shared-pool consent gate send_sms falls over to SMS with the opt-in link.
    Returns True when a hook went out."""
    from models import get_session, User as UserModel
    session = get_session()
    try:
        user = session.get(UserModel, user_id)
        if not user:
            return False
        if (user.onboarding_step or 0) >= 1:
            return False
        name, goal, phone = user.name, user.goal, user.phone
    finally:
        session.close()

    try:
        import photon
        photon.provision_user(user_id)
    except Exception as e:  # never block onboarding on Photon
        logger.warning(f"PHOTON_PROVISION_SKIPPED user={user_id} err={e}")

    hook = random.choice(HOOK_TEMPLATES)
    text = hook["text"].format(name=name, goal_label=_goal_label(goal))
    send_sms(phone, text, user_id=user_id, message_type="onboarding")

    session = get_session()
    try:
        user = session.get(UserModel, user_id)
        if user:
            user.onboarding_step = 1
            user.onboarding_hook_template = hook["id"]
            session.commit()
    finally:
        session.close()
    logger.info("ONBOARDING_HOOK_SENT user=%s template=%s reason=%s", user_id, hook["id"], reason)
    return True


def start_onboarding(user):
    """
    Entry point — called from app.py after signup, /activate-sms, admin activation.
    Sends the hook in a background thread (send_onboarding_hook does the work).
    """
    def _run():
        try:
            send_onboarding_hook(user.id, reason="start_onboarding")
        except Exception as e:
            logger.error(f"Onboarding start failed for {user.name}: {e}")

    threading.Thread(target=_run, daemon=True).start()


# Waitlist (2026-09-19): the one line a pending user gets when they text the line —
# the "hey cued" opt-in tap from the site, or a curious SMS. Code-owned, sent once
# from _process_inbound; the model never sees a waitlist user.
WAITLIST_HOLD_TEXT = "hey {name} — you're on the list. i'll text you right here the second your spot opens."


def waitlist_hold_text(name: str | None) -> str:
    first = (name or "").strip().split(" ")[0]
    return WAITLIST_HOLD_TEXT.format(name=first) if first else WAITLIST_HOLD_TEXT.replace("hey {name} — ", "hey — ")


def awaiting_channel_choice(user) -> bool:
    """A user whose hook was DEFERRED at signup: provisioned (has a link), asked
    for iMessage, no hook yet, and nothing sent or received on any channel.
    Never a pending waitlister: their first text is held, not hooked."""
    from models import get_session, Message
    if user.waitlist_status == "pending":
        return False
    if (user.onboarding_step or 0) >= 1 or not user.photon_user_id:
        return False
    if (user.preferred_channel or "sms") != "imessage":
        return False
    session = get_session()
    try:
        return session.query(Message.id).filter(Message.user_id == user.id,
                                                 Message.direction == "out").first() is None
    finally:
        session.close()


def send_fallback_hooks() -> int:
    """Scheduler job: users who signed up with a link but neither texted the line
    nor tapped "no iPhone" within ONBOARDING_HOOK_FALLBACK_MINUTES get the hook by
    SMS (send_sms hits the consent gate → falls over with the opt-in link). Each
    user is picked up once: after this their outbound rows exist."""
    from models import get_session, User as UserModel, Message
    cutoff = (datetime.now(timezone.utc).replace(tzinfo=None)
              - timedelta(minutes=config.ONBOARDING_HOOK_FALLBACK_MINUTES))
    session = get_session()
    try:
        has_out = (session.query(Message.id)
                   .filter(Message.user_id == UserModel.id, Message.direction == "out").exists())
        ids = [u.id for u in (session.query(UserModel)
                              .filter(UserModel.onboarding_step == 0,
                                      UserModel.waitlist_status.is_(None),  # never a pending waitlister
                                      UserModel.photon_user_id.isnot(None),
                                      UserModel.preferred_channel == "imessage",
                                      UserModel.created_at < cutoff,
                                      ~has_out)
                              .all())]
    finally:
        session.close()
    sent = 0
    for uid in ids:
        try:
            if send_onboarding_hook(uid, reason="fallback_no_choice"):
                sent += 1
                logger.info("ONBOARDING_HOOK_FALLBACK user=%s — no channel choice in %s min, hook by SMS with the link",
                            uid, config.ONBOARDING_HOOK_FALLBACK_MINUTES)
        except Exception as e:  # noqa: BLE001 — one bad user must not stop the sweep
            logger.error("ONBOARDING_HOOK_FALLBACK_FAILED user=%s err=%s", uid, e)
    return sent


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
    Called from webhook on every message while onboarding_step < 3.

    Flow:
      step 1 — hook sent. First reply: record first_reply_at, extract data, reply as
               the friend (one woven question at most), advance to step 2.
      step 2 — getting to know them. Extract from every message; reply as the friend;
               when nothing is still unknown, present the summary; on "sound right?"
               confirmation, complete.
      step 3 — complete (set by _complete_onboarding).

    Returns True if onboarding is now complete.
    """
    import re as _re
    from models import get_session, User as UserModel

    session = get_session()
    try:
        user_row = session.get(UserModel, user.id)
        if not user_row:
            return False
    finally:
        session.close()

    # Record time-to-first-reply for A/B analysis
    if not user_row.first_reply_at:
        session = get_session()
        try:
            user_row = session.get(UserModel, user.id)
            if user_row and not user_row.first_reply_at:
                user_row.first_reply_at = datetime.now(timezone.utc)
                session.commit()
        finally:
            session.close()
        # Re-fetch
        session = get_session()
        try:
            user_row = session.get(UserModel, user.id)
        finally:
            session.close()

    # Auto-fill current_split="none" if user explicitly indicated no training
    if user_row.current_split is None:
        _maybe_auto_fill_no_training(user_row, incoming_message)
        session = get_session()
        try:
            user_row = session.get(UserModel, user.id)
        finally:
            session.close()

    # Always try to extract data from whatever they sent. The coach's previous
    # message (not a "last asked field" — the question is woven, not scheduled)
    # tells the extractor what a bare number or yes/no is answering.
    prev_coach = _last_coach_message(user_row.id)
    extracted = _extract_data_from_message(incoming_message, user_row,
                                           last_coach_message=prev_coach)
    non_null = {k: v for k, v in extracted.items() if v is not None}
    if non_null:
        _store_extracted_data(user_row.id, extracted)
        session = get_session()
        try:
            user_row = session.get(UserModel, user.id)
        finally:
            session.close()

    # A pasted routine (several "3x10" lines) becomes their own card templates — the
    # extractor only keeps "ppl"; the exercises would be lost once history scrolls.
    try:
        from workouts.routine import looks_like_routine, save_routine
        if looks_like_routine(incoming_message):
            r = save_routine(user_row.id, incoming_message, source="onboarding")
            logger.info("ROUTINE_ONBOARDING user=%s result=%s", user_row.id, r)
            session = get_session()
            try:
                user_row = session.get(UserModel, user.id)
            finally:
                session.close()
    except Exception as e:  # noqa: BLE001 — never block the reply on it
        logger.warning("ROUTINE_ONBOARDING_FAILED user=%s err=%s", user_row.id, e)

    # An explicit "remind me / ping me" is a promise: onboarding has no tools, so a
    # small extraction sets (or corrects) the reminder in code and the prompt shows it.
    try:
        from reminders import maybe_capture_onboarding_reminder
        maybe_capture_onboarding_reminder(user_row.id, incoming_message, _conversation_so_far(user_row.id))
    except Exception as e:  # noqa: BLE001 — never block the reply on it
        logger.warning("REMINDER_ONBOARDING_CAPTURE_FAILED user=%s err=%s", user_row.id, e)

    missing_after = _get_missing_fields(user_row)
    system_prompt = _build_system_prompt(user_row)

    # ── First reply to the hook: they're in conversation mode now → step 2 ──
    if (user_row.onboarding_step or 0) == 1:
        session = get_session()
        try:
            u = session.get(UserModel, user.id)
            if u:
                u.onboarding_step = 2
                session.commit()
        finally:
            session.close()

    # ── Nothing still unknown — summary / confirmation flow ─────────────────
    # Whether the summary has been shown is read off the conversation (the coach's
    # previous message ends in "sound right?"), not a flag: the first time the
    # last field lands we PRESENT the summary; only a reply TO the summary can
    # confirm it. (Previously "ok so i'm 5'10" could complete onboarding unseen.)
    if not missing_after:
        summary_shown = bool(prev_coach) and "sound right" in prev_coach.lower()
        if not summary_shown:
            clamp_note = _reconcile_user_targets(user_row.id)
            session = get_session()
            try:
                user_row = session.get(UserModel, user.id)
            finally:
                session.close()
            summary = _build_confirmation_summary(user_row, clamp_note=clamp_note)
            instruction = (
                f"You've got everything you need. {user_row.name} just said: \"{incoming_message}\"\n\n"
                f"STEP 1: React to what they said like a friend would (answer any question fully).\n"
                f"STEP 2: Present this summary and ask if it sounds right:\n\n{summary}\n\n"
                f"Keep it tight. One message. End with 'sound right?'"
            )
            text = _generate(system_prompt, instruction, user_id=user_row.id)
            send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
            logger.info(f"Onboarding confirmation presented to {user_row.name}")
            return False

        confirmation_keywords = [
            "yeah", "yes", "yep", "sounds good", "looks good", "correct",
            "that's right", "perfect", "ok", "sure", "let's go", "lets go",
            "good", "right", "yea", "ya", "bet", "fs",
        ]
        msg_lower = incoming_message.lower().strip()
        is_confirmed = any(kw in msg_lower for kw in confirmation_keywords)

        # A question inside the confirmation must be ANSWERED, not skipped by the
        # completion branch. Live (user 27): "Ok bet ... Why didn't you just go with
        # that in the first place" had no '?' and no matching wh-phrase → completed
        # silently. Any wh-word opener counts.
        has_question = (
            "?" in incoming_message
            or bool(_re.search(r'\b(should i|can i|do i|will i|is it|what (should|do|can|is|are|about)'
                               r'|how (do|can|should|long|much|many|come)|when (should|do|can|will)'
                               r'|why (do|did|didn\'?t|don\'?t|should|is|are|not|would|wouldn\'?t)|wait[, ])\b', msg_lower))
        )

        if is_confirmed and has_question:
            summary = _build_confirmation_summary(user_row)
            instruction = (
                f"Do NOT greet the user — you already said hello earlier.\n\n"
                f"The user confirmed their plan but also asked a question: \"{incoming_message}\"\n\n"
                f"STEP 1: Answer their question directly and completely.\n"
                f"STEP 2: Briefly acknowledge the plan is confirmed.\n"
                f"STEP 3: Present this summary:\n\n{summary}\n\n"
                f"Keep it tight. One message. End with 'sound right?' or similar."
            )
            text = _generate(system_prompt, instruction, user_id=user_row.id)
            send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
            return _complete_onboarding(user_row, incoming_message)

        if is_confirmed:
            return _complete_onboarding(user_row, incoming_message)

        # Not confirmed — user wants to adjust. If they named a number, the bounded
        # override runs HERE in code (±15% of computed; macro_calculator.apply_
        # target_override) and the model is told the outcome — it never invents one.
        summary = _build_confirmation_summary(user_row)
        override_note = ""
        asked = _extract_target_request(incoming_message, user_row) if _re.search(r"\d", incoming_message) else {}
        if asked:
            from macro_calculator import apply_target_override
            r = apply_target_override(user_row.id, calories=asked.get("calories"),
                                      protein=asked.get("protein"), note=incoming_message[:120],
                                      maintenance=asked.get("maintenance"))
            session = get_session()
            try:
                user_row = session.get(UserModel, user.id)
            finally:
                session.close()
            summary = _build_confirmation_summary(user_row)
            lines = []
            m = r.get("maintenance")
            if m and m.get("accepted"):
                lines.append(f"- maintenance: they reported {m['reported']} from their own tracking → "
                             f"NOTED (computed estimate was {m['computed_tdee']}); the calorie band now "
                             f"centres on {m['basis_calories']}. Say their number is what we'll go on.")
            elif m and "min" in m:
                lines.append(f"- maintenance: they reported {m['reported']} → too far from the computed "
                             f"{m['computed_tdee']} to use (would accept {m['min']}–{m['max']}); say "
                             f"you're going on the estimate for now and the biweekly weigh-in cycle "
                             f"will correct it from real data.")
            for field, val in r.get("accepted", {}).items():
                lines.append(f"- {field}: they asked for {val} → ACCEPTED and now set (computed was "
                             f"{r['computed'][field]}). It's their pick; say so, and that you'd have "
                             f"gone {r['computed'][field]}.")
            for field, rj in r.get("rejected", {}).items():
                if "min" in rj:
                    lines.append(f"- {field}: they asked for {rj['asked']} → NOT allowed (band is "
                                 f"{rj['min']}–{rj['max']} around the computed {rj['computed']}). Offer "
                                 f"the nearest end of the band and say why; do not state any other number.")
            if lines:
                override_note = ("TARGET REQUEST HANDLED IN CODE:\n" + "\n".join(lines) +
                                 "\nCurrent targets: " + f"{r['current']['calories']} cal / "
                                 f"{r['current']['protein']}g.\n\n")
        instruction = (
            f"Do NOT greet the user — you already said hello earlier.\n\n"
            f"You showed them this summary:\n\n{summary}\n\nThey replied: \"{incoming_message}\"\n\n"
            f"{override_note}"
            f"Address their concern like a friend. The calorie and protein numbers are COMPUTED "
            f"from their stats and goal — you can explain them (recomp = maintenance; their steps "
            f"and training; protein holds muscle). They CAN pick a number within 15% of the "
            f"computed one — if they want it different but didn't name a number, invite one "
            f"(\"what feels doable?\"). Never invent or announce a number yourself; only code sets "
            f"them. If they corrected a FACT (height, weight, days, times), acknowledge it; it'll be "
            f"fixed. Do NOT repeat the whole summary again — they just read it. End with the "
            f"current numbers in one short line and a short close like 'lock it in?'. "
            f"One message, brief."
        )
        text = _generate(system_prompt, instruction, user_id=user_row.id)
        send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
        return False

    # ── Still getting to know them ──────────────────────────────────────────
    # Friend reply by default (one woven question max). The big ask and the
    # two-field bundle are kept for when a list is the right move — see
    # _intake_mode(): they asked for it, the conversation has run long with most
    # fields unknown, or one/two are left to close out.
    mode = _intake_mode(incoming_message, missing_after, _coach_turns(user_row.id),
                        big_ask_sent=_big_ask_sent(user_row.id))
    out_type = "onboarding"
    if mode == "big_ask":
        text = _build_big_ask_message(user_row, incoming_message, system_prompt, missing_after)
        out_type = BIG_ASK_MESSAGE_TYPE
    elif mode == "bundle":
        text = _bundle_gap_questions(missing_after, user_row, incoming_message, system_prompt)
    else:
        text = _build_friend_reply(user_row, incoming_message, system_prompt, missing_after)
    send_sms(user_row.phone, text, user_id=user_row.id, message_type=out_type)
    remaining_names = [f[0] for f in missing_after]
    logger.info(f"ONBOARDING_REPLY mode={mode} user={user_row.id} still_unknown={remaining_names}")
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

        if getattr(user_row, "targets_source", None) == "user" and user_row.calorie_target and user_row.protein_target:
            # They picked (within the band) during the adjust turn — keep their numbers.
            targets = dict(targets, calories=user_row.calorie_target, protein=user_row.protein_target)
        else:
            user_row.calorie_target = targets["calories"]
            user_row.protein_target = targets["protein"]
            user_row.targets_source = "computed"
        user_row.calorie_target_computed = calculate_targets(user)["calories"]
        user_row.protein_target_computed = calculate_targets(user)["protein"]
        user_row.confirmed_goal_priority = targets.get("goal_label", user_row.goal)
        user_row.coaching_branch = _determine_coaching_branch(user_row)
        user_row.onboarding_step = 3  # complete

        # Copy profile fields to confirmed counterparts
        _finalize_onboarding_profile(user_row)

        session.commit()
        logger.info(f"Onboarding complete for {user_row.name} — {targets['calories']} cal, {targets['protein']}g protein, bmr={targets['bmr']} ({targets.get('bmr_formula', 'mifflin')}), tdee={targets['tdee']}, goal_pct={targets.get('goal_pct')}, limits={targets.get('goal_limits')}, source={user_row.targets_source}, branch={user_row.coaching_branch}")

        try:
            schedule_user(user_row)
        except Exception as e:
            logger.error(f"Scheduling failed for {user_row.name}: {e}")

        profile_link = profile_url(user_row)
        system_prompt = _build_system_prompt(user_row)
        instruction = (
            f"The user just confirmed their plan. Onboarding is complete.\n"
            f"Targets: {targets['calories']} cal, {targets['protein']}g protein daily.\n"
            f"Profile link: {profile_link}\n\n"
            f"Send ONE brief message that:\n"
            f"1. Confirms everything is locked in\n"
            f"2. Tells them when they'll hear from you next (based on their wake_time: {user_row.wake_time})\n"
            f"3. Gives them their profile link naturally — e.g. 'you can check your profile at {profile_link}'\n"
            f"4. Feels like the starting gun — they now have a coach\n"
            f"No explanations. No feature previews. Just confidence. Don't open with their name."
        )
        text = _generate(system_prompt, instruction, user_id=user_row.id)
        send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
        rundown_user = user_row

    finally:
        session.close()

    # Second bubble, a beat later: how to use me — from capabilities.py for THIS
    # user. Best-effort; the kickoff already went out and completion is committed.
    try:
        _send_capability_rundown(rundown_user, system_prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("ONBOARDING_RUNDOWN_FAILED user=%s err=%s", user.id, e)

    # The onboarding conversation is the richest life-context the coach will ever
    # get about this person (their classes, where they eat, who they went to SF
    # with) and until now NONE of it survived into coaching: onboarding turns ran
    # no memory extraction, and the coach loop's history window rolls past a
    # bursty onboarding within a day. Digest it NOW (force past the quiet gate) so
    # RECENT LIFE CONTEXT carries it forward. Background; never blocks the kickoff.
    if config.EPISODIC_ENABLED:
        def _digest():
            try:
                from episodic import digest_user
                res = digest_user(user.id, force=True)
                logger.info("ONBOARDING_DIGEST user=%s result=%s", user.id, res)
            except Exception as e:  # noqa: BLE001
                logger.warning("ONBOARDING_DIGEST_FAILED user=%s err=%s", user.id, e)
        threading.Thread(target=_digest, daemon=True).start()

    return True
