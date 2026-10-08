"""
Onboarding Agent — Cued
========================
Getting to know a new friend over text (founder, 2026-09-11) — with the fixes from
user 47 (2026-09-30: 27 coach turns, no workout; "168 cm" never parsed; "i got
everything i need" ×3; a card asked for ×5 and called "glitching"; the activation
text read as "you're set up").

The objective facts come from the signup form (signup_stats.py: height, weight, days,
time, diet, apps, steps) — the coach never asks for a number the form holds. Each
exchange here:
1. Parse the stats in CODE first — metric or imperial (168cm / 50kg / 5'6 / 120 lbs)
   — then the extractor for everything else, given the coach's previous message.
2. Store what was found.
3. Reply as the friend (prompts/identity.md): engage the specific thing they said;
   if — and only if — what they said gives a natural reason, weave in ONE question
   that would teach us one of the still-unknown fields. Never a list BY DEFAULT.
   The big ask and the two-field bundle are kept for when they're appropriate
   (they ask for the list, the conversation has run long, one or two left) — see
   _intake_mode(). The model can never send a card and never claims one is coming.
4. Nothing still unknown → ONE code summary (real numbers) and completion in the
   SAME message — no "sound right?" round trip; corrections go to the coach's tools.
   Then the first card (workouts/card_setup.py).
5. They ask for a workout and the card-critical fields (height/weight, days,
   injuries) are in → complete right then; the rest is learned during coaching.
The hook says what's happening ("ur in … gonna get to know u a bit first, then ur
first workout") so nobody mistakes it for being set up already.
"""

import os
import json
import logging
import re
import threading
from datetime import datetime, timezone, timedelta

from anthropic import Anthropic
import config
from config import ANTHROPIC_API_KEY, COACH_MODEL
from sms import send_sms
from profile_page import profile_url
from macro_calculator import calculate_targets
from workouts.templates import day_label as _day_label
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

# Fields the first card and the targets cannot do without. When they ask for a
# workout and these are in, onboarding completes right then (the rest is learned
# during coaching); when they're not, the reply asks for exactly these.
CARD_CRITICAL = {"height_weight", "workout_days", "injuries", "split_days"}

# The hook. One template now: who this is, that they're in, what happens next, and
# a low-effort question. The old A/B openers ("did you know the average person sets
# the same goal 3 years in a row…") read as a finished setup — user 47 thought she
# was live and texted "can I get a workout card?" five times.
HOOK_TEMPLATES = [
    {"id": "hook_setup",
     "text": "hey {name}, it's cued. ur in. gonna get to know u a bit over the next few texts, "
             "then ur first workout. how's ur day going"},
]
HOOK_ACTIVATED_TEXT = ("hey {name}, it's cued. ur spot's open. gonna get to know u a bit over the next "
                       "few texts, then ur first workout. how's ur day going")

GOAL_LABELS = {
    "fat_loss": "losing fat",
    "muscle_building": "building muscle",
    "fat_loss,muscle_building": "recomp",
    "muscle_building,fat_loss": "recomp",
    "general_fitness": "getting healthier",
    "endurance": "building endurance",
    "strength": "getting stronger",
}


def _goal_words(goal_str: str, table: dict, default: str) -> str:
    """A goal value as words. The form stores a comma list ("fat_loss,muscle_building,
    strength"); the table knows the single goals and the recomp pair, so: known pair
    first, each remaining goal mapped, joined with ' + '. Live 2026-10-05 (user 48): the
    summary read "fat loss,muscle building,strength"."""
    raw = (goal_str or "").strip()
    if not raw:
        return default
    if raw in table:
        return table[raw]
    parts = [g.strip() for g in raw.split(",") if g.strip()]
    out: list[str] = []
    rest = list(parts)
    pair = {"fat_loss", "muscle_building"}
    if pair <= set(rest):
        out.append(table.get("fat_loss,muscle_building", "recomp"))
        rest = [g for g in rest if g not in pair]
    out.extend(table.get(g, g.replace("_", " ")) for g in rest)
    return " + ".join(out) if out else default


def _goal_label(goal_str: str) -> str:
    return _goal_words(goal_str, GOAL_LABELS, "your goal")


def _goal_phrase(goal_str: str) -> str:
    return _goal_words(goal_str, GOAL_PHRASES, "general fitness")


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
        # A split label without its days can't become a card (user 43's bro split →
        # full_body). Ask for the grouping when the label leaves it open.
        if (user.current_split in ("bro_split", "bro", "custom") and not (getattr(user, "split_days", None) or [])
                and len(getattr(user, "custom_templates", None) or {}) < 2):
            missing.append(("split_days", "which days they group together and in what order — e.g. chest+bis / back+tris / legs+shoulders, or chest / back / shoulders / arms / legs"))
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
        (f"Their days, in order: " + " → ".join(_day_label(d) for d in user.split_days)
         if getattr(user, "split_days", None) else None),
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
- You cannot send a workout, a card, a plan, numbers, or a link — code sends the first
  card the moment the basics are in (their stats, training days, anything that hurts).
  If they ask for a workout / card / plan, say it's coming once those are in,
  in your own words, as the friend. Never say a card is loading, glitching, should pop
  up, or already went — none of that is true. Never write a workout out as text.
- 1–3 short sentences. No `---` separators. No greeting — you already said hey.
- Decide on your ONE question (or none) BEFORE you start writing, then write the reply
  once, as a single paragraph. Never draft-then-revise inside the message, never show an
  edit, never add a second paragraph.
"""


def coach_turn_text(user_id: int, after_message_id: int) -> str:
    """Every outbound body sent after `after_message_id`, joined — the coach's whole
    turn (a reaction bubble + the summary, say) for the memory extractor."""
    from models import get_session, Message
    session = get_session()
    try:
        rows = (session.query(Message.body)
                .filter(Message.user_id == user_id, Message.direction == "out", Message.id > after_message_id)
                .order_by(Message.id).all())
    finally:
        session.close()
    return "\n".join((r[0] or "").strip() for r in rows if r[0])


def latest_message_id(user_id: int) -> int:
    from models import get_session, Message
    from sqlalchemy import func
    session = get_session()
    try:
        return session.query(func.max(Message.id)).filter(Message.user_id == user_id).scalar() or 0
    finally:
        session.close()


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
  "height_cm": number or null (ONLY when they gave height in cm or metres — "168 cm" → 168, "1.68m" → 168; never convert to feet yourself),
  "weight_kg": number or null (ONLY when they gave weight in kg — "50 kg" → 50; never convert to lbs yourself),
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
  "food_logger": "myfitnesspal" or "mynetdiary" or "cronometer" or "loseit" or "macrofactor" or "other" or null,
  "avg_steps": integer (daily step count) or null,
  "current_split": "ppl" or "upper_lower" or "full_body" or "bro_split" or "custom" or "none" or null,
  "split_days": ["chest and biceps", "back and triceps", "legs and shoulders"] (their training days IN THE ORDER they run them, one entry per day, in their own words) or null,
  "year": "freshman" or "sophomore" or "junior" or "senior" or "grad" or "transfer" or null,
  "meal_plan_status": "on_meal_plan" or "no_meal_plan" or null
}}

year / meal_plan_status rules (Berkeley context — bonus facts, only when clearly stated):
- "I'm a junior" → year="junior"; "first year" / "freshman" → "freshman"; "grad student" → "grad"
- "I don't have a meal plan" / "no dining hall pass" / "not on the meal plan" → meal_plan_status="no_meal_plan"
- "I'm on the meal plan" / "I have swipes" / "dining hall pass" → meal_plan_status="on_meal_plan"
- Eating AT a dining hall once says nothing about meal_plan_status → null

food_logger rules (the FOOD-logging app they currently use, canonical id):
- "mfp" / "my fitness pal" / "MyFitnessPal" → "myfitnesspal"
- "mynetdiary" / "my net diary" / "net diary" / "net dairy" (a common typo) → "mynetdiary"
- "cronometer" → "cronometer"; "lose it" → "loseit"; "macrofactor" → "macrofactor"
- some other calorie/food app named or implied ("I have an app I count calories in") → "other"
- a wearable or workout app only (Strava, Apple Watch, Nike Run Club) → null

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

split_days rules (the days themselves — code builds their workout cards from this, so a stated split that isn't captured here gets them the WRONG card):
- Whenever they list what they train on each day, give every day as its own entry, in order, in their words: "chest and biceps, back and triceps and legs and shoulders" → ["chest and biceps", "back and triceps", "legs and shoulders"] (and current_split="bro_split")
- "push pull legs" → ["push", "pull", "legs"]; "upper lower" → ["upper", "lower"]; "chest, back, shoulders, arms, legs" → one entry each
- "chest/tris, back/bis, legs" → ["chest and triceps", "back and biceps", "legs"]
- A label alone ("bro split", "ppl") with no days listed → split_days=null
- A single day they did today ("hit chest today") is NOT their split → null

Examples:
"I'm 5'7 and 145 lbs" → {{"height_ft": 5, "height_in": 7, "weight_lbs": 145, ...rest null}}
"I weight 50 kg and 168 cm for height" → {{"height_cm": 168, "weight_kg": 50, ...rest null}}  (metric stays metric; code converts)
"168 and 50" (after being asked height and weight) → {{"height_cm": 168, "weight_kg": 50, ...rest null}}  (a 3-digit height is cm)
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
"Oh yeah I have an app, my net dairy" → {{"existing_tools": "mynetdiary", "tools_decision": "acknowledged", "food_logger": "mynetdiary", ...rest null}}
"I count calories in mfp" → {{"existing_tools": "myfitnesspal", "tools_decision": "acknowledged", "food_logger": "myfitnesspal", ...rest null}}
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

        # Metric → the imperial columns (live 2026-09-30, user 47: "50 kg and 168 cm"
        # twice; the extractor had no cm field, height stayed null, onboarding never
        # closed). Code converts; the model never does.
        from signup_stats import cm_to_ft_in, kg_to_lbs
        if data.get("height_cm") and not data.get("height_ft"):
            ft, inch = cm_to_ft_in(data["height_cm"])
            if ft:
                data["height_ft"], data["height_in"] = ft, inch
        if data.get("weight_kg") and not data.get("weight_lbs"):
            lbs = kg_to_lbs(data["weight_kg"])
            if lbs:
                data["weight_lbs"] = lbs

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
        if isinstance(data.get("split_days"), list) and data["split_days"]:
            from workouts.routine import split_days_from_phrases, _split_for
            keys = split_days_from_phrases([str(d) for d in data["split_days"]])
            if keys and keys != (user.split_days or None):
                user.split_days = keys
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(user, "split_days")
                changed = True
                if not user.current_split or user.current_split in ("none", "custom"):
                    user.current_split = _split_for(keys) or user.current_split
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
        if data.get("food_logger"):
            # Logger bridge: the food app they still use → coexist from day one, so the
            # coach never treats an empty day as an unlogged day (food_logger.py).
            from food_logger import normalize_app
            _app = normalize_app(data["food_logger"])
            if _app:
                _set("food_logger", _app)
                _set("food_logger_status", "coexist")
                from datetime import datetime as _dt, timezone as _tz
                _set("food_logger_since", _dt.now(_tz.utc).replace(tzinfo=None))
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
        "the targets (they just got them). End with their profile link on its own short line, "
        f"exactly this URL and nothing else about it: {profile_url(user_row)} . No question at the end.\n\n"
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
            notes.append(f"u said {asked}{unit}; {nearest}{unit} is as {'low' if asked < lo else 'high'} as i'll go "
                         f"for ur stats (i'd have said {rj['computed']}{unit})")
        u.calorie_target_computed = r["computed"]["calories"]
        u.protein_target_computed = r["computed"]["protein"]
        u.targets_source = "user"
        session.commit()
    finally:
        session.close()
    if notes:
        logger.info("TARGETS_USER_CLAMPED user=%s %s", user_id, "; ".join(notes))
    return "; ".join(notes) or None


GOAL_PHRASES = {
    "fat_loss": "cutting",
    "muscle_building": "building muscle",
    "fat_loss,muscle_building": "recomp",
    "muscle_building,fat_loss": "recomp",
    "general_fitness": "general fitness",
    "endurance": "endurance",
    "strength": "getting stronger",
}


def _clock(hhmm: str | None) -> str:
    """'09:30' → '9:30', '00:00' → '12', '14:00' → '2'. A description stays as-is."""
    t = (hhmm or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if not m:
        return t
    h, mm = int(m.group(1)) % 12 or 12, m.group(2)
    return f"{h}" if mm == "00" else f"{h}:{mm}"


def _build_confirmation_summary(user, clamp_note: str | None = None) -> str:
    """The one bubble that closes onboarding, in the friend's voice — identity.md's own
    example: "ok so 5'0 137, training evenings, up at 8:30 down by 11:30 … 1450 cal,
    137g protein". Code-authored: every number here is real (live, user 27: the model
    wrote "2300 cal and 150g protein" — numbers it cannot set). States only what they
    gave — a fact they didn't give is a trust break ("20 years old" recited when age
    was never asked). Wake/sleep stay in on purpose: a swapped 2am bedtime (user 27)
    is only catchable here. Ends "say if anything's off" — corrections go to the coach."""
    targets = calculate_targets(user)
    nutrition_only = _is_nutrition_only(user)

    who = []
    if user.height_ft:
        who.append(f"{user.height_ft}'{user.height_in or 0}")
    if user.weight_lbs:
        who.append(f"{int(user.weight_lbs)}")
    parts = [" ".join(who)] if who else []
    if not nutrition_only and user.workout_days:
        days = str(user.workout_days).strip()
        when = ""
        wt = (user.workout_time or "").strip().lower()
        if wt:
            when = {"08:00": " mornings", "14:00": " afternoons", "18:00": " evenings",
                    "morning": " mornings", "afternoon": " afternoons", "evening": " evenings"}.get(wt, f" at {_clock(wt)}")
        if days.replace("-", "").replace("+", "").isdigit():
            parts.append(f"training {days} days a week{when}")
        else:
            parts.append(f"training {days.replace(',', '/')}{when}")
    if user.wake_time or user.sleep_time:
        bits = []
        if user.wake_time:
            bits.append(f"up at {_clock(user.wake_time)}")
        if user.sleep_time:
            bits.append(f"down by {_clock(user.sleep_time)}")
        parts.append(" ".join(bits))
    parts.append(_goal_phrase(user.goal))
    if not nutrition_only:
        try:
            from workouts.routine import describe_routine
            if describe_routine(getattr(user, "custom_templates", None)):
                parts.append("ur own routine's on ur cards")
            elif getattr(user, "split_days", None):
                parts.append("split is " + " / ".join(_day_label(d) for d in user.split_days))
        except Exception:  # noqa: BLE001
            pass
    first = "ok so " + ", ".join(p for p in parts if p)

    if clamp_note:
        targets_bit = f"{clamp_note}. so {user.calorie_target} cal, {user.protein_target}g protein a day"
    elif getattr(user, "targets_source", None) == "user" and user.calorie_target and user.protein_target:
        targets_bit = (f"{user.calorie_target} cal, {user.protein_target}g protein a day, ur pick "
                       f"(i'd have said {targets['calories']}/{targets['protein']}g)")
    else:
        targets_bit = f"{targets['calories']} cal, {targets['protein']}g protein a day"
    return f"{first}. {targets_bit}. say if anything's off"


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


# ─── Stats in code: metric or imperial, before the model sees the message ─────
_HEIGHT_CM_RE = re.compile(r"\b(1\d\d|2[0-4]\d)\s*(?:cm|cms|centimet(?:er|re)s?)\b", re.I)
_HEIGHT_M_RE = re.compile(r"\b([12])[.,](\d{1,2})\s*(?:m|meters?|metres?)\b", re.I)
_HEIGHT_FTIN_RE = re.compile(
    r"\b([4-7])\s*(?:'|’|′|ft\.?|feet|foot)\s*(\d{1,2})?\s*(?:\"|”|″|''|in\.?|inches)?(?![\w'])", re.I)
_WEIGHT_KG_RE = re.compile(r"\b(\d{2,3}(?:[.,]\d)?)\s*(?:kg|kgs|kilos?|kilograms?)\b", re.I)
_WEIGHT_LB_RE = re.compile(r"\b(\d{2,3}(?:\.\d)?)\s*(?:lbs?|pounds?)\b", re.I)
_DAYS_N_RE = re.compile(r"\b([1-7])\s*(?:days?|x|times)\b(?:\s*(?:a|per|/|every)\s*(?:week|wk))?", re.I)
_DAY_NAMES = (("mon", r"\bmon(?:day)?s?\b"), ("tue", r"\btue(?:s|sday)?s?\b"), ("wed", r"\bwed(?:nesday)?s?\b"),
              ("thu", r"\bthu(?:r|rs|rsday)?s?\b"), ("fri", r"\bfri(?:day)?s?\b"), ("sat", r"\bsat(?:urday)?s?\b"),
              ("sun", r"\bsun(?:day)?s?\b"))
_NO_INJURY_RE = re.compile(
    r"\bno injur\w*\b|\bnothing hurts?\b|\bnothing(?:'s| is)? (?:hurt|injured|wrong)\b|"
    r"\b(?:no|none|nothing|nah|nope)\b[^.!?\n]{0,20}\b(?:hurt\w*|injur\w*|pain)\b|"
    r"\b(?:injur\w*|pain)[^.!?\n]{0,12}\b(?:none|no|nothing)\b", re.I)


def parse_stats(message: str) -> dict:
    """Deterministic read of the basics: height (cm / m / ft-in), weight (kg / lbs, or
    a bare number next to a height), training days ("3 days", "3x a week", "mon tue
    thu"), and an explicit no-injury statement. Code beats the model on anything
    here; what it doesn't find is left to the extractor (which now knows cm/kg too).
    Live 2026-09-30 (user 47): "50 kg and 168 cm" twice, height never stored."""
    from signup_stats import cm_to_ft_in, kg_to_lbs
    text = (message or "").strip()
    out: dict = {}
    if not text:
        return out
    metric_height = False
    m = _HEIGHT_CM_RE.search(text)
    if m:
        ft, inch = cm_to_ft_in(int(m.group(1)))
        if ft:
            out["height_ft"], out["height_in"], metric_height = ft, inch, True
    else:
        m = _HEIGHT_M_RE.search(text)
        if m:
            ft, inch = cm_to_ft_in(int(m.group(1)) * 100 + int(m.group(2).ljust(2, "0")))
            if ft:
                out["height_ft"], out["height_in"], metric_height = ft, inch, True
        else:
            m = _HEIGHT_FTIN_RE.search(text)
            if m and not re.match(r"\s*(?:days?|x|times|hrs?|hours?|a week)", text[m.end():], re.I):
                out["height_ft"], out["height_in"] = int(m.group(1)), int(m.group(2) or 0)
    height_span = m.span() if (m and "height_ft" in out) else None

    w = _WEIGHT_KG_RE.search(text)
    if w:
        lbs = kg_to_lbs(w.group(1))
        if lbs:
            out["weight_lbs"] = lbs
    else:
        w = _WEIGHT_LB_RE.search(text)
        if w:
            try:
                lbs = float(w.group(1))
                if 60 <= lbs <= 600:
                    out["weight_lbs"] = lbs
            except ValueError:
                pass
        elif height_span:
            # "5'6 and 120" / "168cm 50" — a bare number beside a height is the weight,
            # in the same system as the height.
            rest = text[:height_span[0]] + " " + text[height_span[1]:]
            for n in re.findall(r"\b(\d{2,3}(?:\.\d)?)\b", rest):
                v = float(n)
                if metric_height and 35 <= v <= 200:
                    out["weight_lbs"] = kg_to_lbs(v)
                    break
                if not metric_height and 80 <= v <= 400:
                    out["weight_lbs"] = v
                    break

    days = [key for key, pat in _DAY_NAMES if re.search(pat, text, re.I)]
    if len(days) >= 2:
        out["workout_days"] = ",".join(days)
    else:
        d = _DAYS_N_RE.search(text)
        if d:
            out["workout_days"] = d.group(1)

    if _NO_INJURY_RE.search(text):
        out["injuries"] = "none"
    return {k: v for k, v in out.items() if v is not None}


# They want the workout. With the card-critical fields in, that completes onboarding
# right then (the rest is learned during coaching); without them, the reply asks for
# exactly those — the friend's one list, and never a fake card.
_WANTS_WORKOUT = re.compile(
    r"\b(work ?out|card|plan|routine|program|exercises?|leg day|push day|pull day|arm day|chest day|"
    r"back day|lift(?:ing)?|train(?:ing)? (?:now|today|rn)|what should i do|at (?:the )?(?:rsf|gym))\b",
    re.IGNORECASE)


def _wants_workout(message: str) -> bool:
    return bool(_WANTS_WORKOUT.search(message or ""))


def _is_bare_answer(message: str, code_found: dict) -> bool:
    """A short stats-only reply ("168cm and 50kg", "no injuries") at the moment onboarding
    closes needs no reaction bubble — the summary is the reply. Only CODE-parsed stats
    count: a sentence the extractor read ("I don't eat mushrooms…") still gets the friend."""
    m = (message or "").strip()
    return bool(code_found) and "?" not in m and len(m) <= 48


def _build_card_ask(user, incoming_message: str, system_prompt: str, card_missing: list) -> str:
    """They asked for a workout before the card-critical basics are in: react, be honest
    that the card comes once they're in, and ask for exactly those in one breath.
    The ONE time a list is right that isn't _intake_mode's call."""
    items = ", ".join(f[1].split(" — ")[0] for f in card_missing)
    instruction = (
        f"{user.name} just texted you: \"{incoming_message}\"\n\n"
        f"They want their workout. You can't send it yet — code sends their first card the "
        f"moment these are in: {items}. Reply as the friend in one message: react to what they "
        f"said, say the card's coming once u have those (your words), and ask for them in one "
        f"breath — only these, nothing already known. Never say a card is loading, glitching, "
        f"should pop up, or already went. Never write a workout out as text. 2-3 sentences, "
        f"no greeting. {_NOT_DONE_LINE}"
    )
    return _generate(system_prompt, instruction, user_id=user.id)


def _build_completion_reaction(user, incoming_message: str, system_prompt: str, *, early: bool) -> str:
    """The friend's bubble right before the code summary closes onboarding: react to /
    answer what they said, no question, no numbers (the summary has the numbers)."""
    instruction = (
        f"{user.name} just texted you: \"{incoming_message}\"\n\n"
        f"Reply as the friend in ONE short bubble, 1-2 sentences: react to the specific thing "
        f"they said, or answer their question, fully. Code is about to send their numbers and "
        f"then their first workout card right after your bubble"
        + (" (they asked for it — say it's coming, one clause)" if early else "")
        + ". So: no question, no numbers, no summary, no 'locked in'. No greeting."
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

    hook = HOOK_TEMPLATES[0]
    text = (HOOK_ACTIVATED_TEXT if reason == "waitlist_activate" else hook["text"]).format(
        name=name, goal_label=_goal_label(goal))
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


def start_onboarding(user, reason: str = "start_onboarding"):
    """
    Entry point — called from app.py after signup, /activate-sms, admin activation.
    Sends the hook in a background thread (send_onboarding_hook does the work).
    """
    def _run():
        try:
            send_onboarding_hook(user.id, reason=reason)
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


# ─── Burst / continuation guard ───────────────────────────────────────────────
# Live 2026-10-05 (user 48): "Lwk a combo of both" / "I'm either walking somewhere or in
# my room" 7s apart → two turns → "do u cook in ur room or dining hall" asked at :48 AND
# :59. Same for "cooked how" (×2). The second turn's inbound had already been SEEN by the
# first reply (history is read at generation time), or added nothing — either way it is
# a continuation of their thought, not a fresh answer, and the question that is out there
# stands. Code decides; the model is told to react in one line or say nothing.
FOLLOWUP_MESSAGE_TYPE = "onboarding_followup"   # not counted as an intake turn (_coach_turns)


def _continuation_state(user_id: int, found_any: dict, incoming_message: str = "") -> tuple[bool, str | None, str]:
    """(is_continuation, prev_coach_body, why). why = 'covered' when their latest message
    was stored BEFORE the coach's last line went out — that reply was generated with it in
    history, so it has been seen (and the question in it stands). A message that arrived
    after the coach's line is a real reply, however short, and gets a normal turn. Any
    fact the covered message carried was already extracted and stored by the caller."""
    if not config.ONBOARDING_CONTINUATION_GUARD_ENABLED:
        return False, None, ""
    from models import get_session, Message
    session = get_session()
    try:
        last_out = (session.query(Message)
                    .filter(Message.user_id == user_id, Message.direction == "out")
                    .order_by(Message.created_at.desc(), Message.id.desc()).first())
        last_in = (session.query(Message)
                   .filter(Message.user_id == user_id, Message.direction == "in")
                   .order_by(Message.created_at.desc(), Message.id.desc()).first())
        if not last_out or not last_out.created_at:
            return False, None, ""
        prev = last_out.body
        out_at = last_out.created_at
        # The stored latest inbound must BE the message being processed (the webhook stores
        # before buffering; a flush's combined body ends with the newest text) — otherwise
        # this is a turn with no stored inbound (tests, replays) and the guard stays out.
        if (last_in is not None and last_in.created_at and last_in.created_at <= out_at
                and (last_in.body or "").strip() and (incoming_message or "").strip().endswith((last_in.body or "").strip())):
            return True, prev, "covered"
        return False, prev, ""
    finally:
        session.close()


def _build_continuation_reply(user, incoming_message: str, system_prompt: str, prev_coach: str | None) -> str:
    instruction = (
        f"{user.name} just added to what they were saying: \"{incoming_message}\"\n\n"
        f"You already replied to their previous text a moment ago"
        + (f" — your line was: \"{prev_coach}\"" if prev_coach else "")
        + ". That question (if you asked one) STANDS; they haven't answered it yet. Do NOT ask "
        "anything this message — not that question again, not a rephrasing of it, not a new one. "
        "Either react to the new bit in ONE short line (no question, no greeting), or, if there is "
        "nothing worth adding, reply with exactly [silent]. One line at most."
    )
    return _generate(system_prompt, instruction, user_id=user.id)


# ─── Wake/sleep: take the estimate, never ask a third time ────────────────────
_WAKE_ASK_RE = re.compile(r"\b(sleep|sleeping|asleep|wake|waking|get up|getting up|crash|bed|bedtime|up at|up til|up until|"
                          r"schedule|nocturnal|night owl|late)\b", re.I)
_QUALITATIVE_RE = re.compile(
    r"\b(yeah|yea|yep|yes|ya|yup|like that|pretty much|basically|kinda|kind of|sorta|sort of|hella|late|"
    r"cooked|nocturnal|random|all over|whenever|depends|idk|dunno|not sure|no idea|varies)\b", re.I)
_LATE_WORDS_RE = re.compile(r"\b(late|cooked|nocturnal|night owl|all[- ]?nighter|3 ?am|4 ?am|2 ?am|noon)\b", re.I)
_PROPOSED_AMPM_RE = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", re.I)
_PROPOSED_UP_RE = re.compile(r"\bup\s+(?:at|by|around|like|til|until)\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:am|pm))", re.I)
_PROPOSED_WORDS_RE = re.compile(r"\b(noon|midnight)\b", re.I)
LATE_DEFAULT = ("11:00", "02:00")     # (wake, sleep) for "late / cooked / nocturnal"
PLAIN_DEFAULT = ("08:00", "23:00")


def _wake_sleep_asks(user_id: int) -> int:
    """How many coach onboarding lines so far were about wake/sleep."""
    from models import get_session, Message
    session = get_session()
    try:
        rows = (session.query(Message.body)
                .filter(Message.user_id == user_id, Message.direction == "out",
                        Message.message_type.in_(("onboarding", BIG_ASK_MESSAGE_TYPE, FOLLOWUP_MESSAGE_TYPE))).all())
        return sum(1 for (b,) in rows if b and _WAKE_ASK_RE.search(b))
    finally:
        session.close()


def _times_proposed(coach_text: str) -> tuple[str | None, str | None]:
    """(wake, sleep) HH:MM the coach's own question floated: "like 3am and up at 11 or
    all over the place" → ("11:00", "03:00"); "like 3am up at noon" → ("12:00", "03:00")."""
    t = coach_text or ""
    wake = sleep = None
    for m in _PROPOSED_AMPM_RE.finditer(t):
        h, mm, ap = int(m.group(1)), m.group(2) or "00", m.group(3).lower()
        if not (1 <= h <= 12):
            continue
        h24 = (h % 12) + (12 if ap == "pm" else 0)
        if ap == "am" and h24 <= 5:          # 1–5am is a bedtime
            sleep = sleep or f"{h24:02d}:{mm}"
        elif 6 <= h24 <= 14:                 # 6am–2pm is a wake
            wake = wake or f"{h24:02d}:{mm}"
        elif h24 >= 21:                      # 9pm–midnight is a bedtime
            sleep = sleep or f"{h24:02d}:{mm}"
    for m in _PROPOSED_WORDS_RE.finditer(t):
        if m.group(1).lower() == "noon":
            wake = wake or "12:00"
        else:
            sleep = sleep or "00:00"
    m = _PROPOSED_UP_RE.search(t)
    if m and not wake:
        h = int(m.group(1))
        if 5 <= h <= 12:
            wake = f"{h:02d}:{m.group(2) or '00'}"
        elif 1 <= h <= 2:                    # "up at 1" after a late night
            wake = f"{h + 12:02d}:{m.group(2) or '00'}"
    return wake, sleep


def _maybe_estimate_wake_sleep(user_row, incoming_message: str, prev_coach: str | None) -> dict | None:
    """When wake/sleep is still unknown after extraction and the reply was qualitative
    (no clock in it): take the coach's own proposed times if the last line floated some;
    otherwise, once the field has been asked ONBOARDING_ESTIMATE_AFTER_ASKS times, a
    descriptor default ("late"/"cooked" → up 11 / down 2; else up 8 / down 11). Returns the
    fields stored, or None. Live 2026-10-05 (user 48): "yeah like staying up hella late and
    waking up late too" → asked AGAIN; the coach had already said "like 3am and up at 11"."""
    if not config.ONBOARDING_WAKE_SLEEP_ESTIMATE_ENABLED:
        return None
    if user_row.wake_time and user_row.sleep_time:
        return None
    msg = (incoming_message or "").strip()
    if not msg or re.search(r"\d", msg) or not _QUALITATIVE_RE.search(msg):
        return None
    if not prev_coach or not _WAKE_ASK_RE.search(prev_coach):
        return None
    wake, sleep = _times_proposed(prev_coach)
    source = "proposal"
    if not (wake and sleep):
        asks = _wake_sleep_asks(user_row.id)
        if asks < config.ONBOARDING_ESTIMATE_AFTER_ASKS:
            return None
        late = bool(_LATE_WORDS_RE.search(msg) or _LATE_WORDS_RE.search(prev_coach))
        dw, ds = LATE_DEFAULT if late else PLAIN_DEFAULT
        wake, sleep, source = wake or dw, sleep or ds, "default"
    data = {}
    if not user_row.wake_time:
        data["wake_time"] = wake
    if not user_row.sleep_time:
        data["sleep_time"] = sleep
    _store_extracted_data(user_row.id, data)
    logger.info("ONBOARDING_WAKE_SLEEP_ESTIMATED user=%s source=%s wake=%s sleep=%s", user_row.id, source, wake, sleep)
    return data


def handle_onboarding_reply(user, incoming_message: str) -> bool:
    """
    Called from webhook on every message while onboarding_step < 3.

    Flow:
      step 1 — hook sent. First reply: record first_reply_at, extract data, reply as
               the friend (one woven question at most), advance to step 2.
      step 2 — getting to know them. Stats parsed in code, then the extractor; reply
               as the friend. Nothing still unknown — or they ask for a workout with
               the card-critical basics in — → the summary + completion in the same
               message, then the first card.
      step 3 — complete (set by _complete_onboarding).

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

    # Stats in code first — metric or imperial, deterministic (live 2026-09-30,
    # user 47: "50 kg and 168 cm" twice, height never stored). Then the extractor
    # for anything else, given the coach's previous message (not a "last asked
    # field" — the question is woven, not scheduled) so a bare number or yes/no
    # maps to what was asked.
    code_found = parse_stats(incoming_message)
    if code_found:
        _store_extracted_data(user_row.id, code_found)
        logger.info("ONBOARDING_STATS_IN_CODE user=%s found=%s", user_row.id, sorted(code_found))
        session = get_session()
        try:
            user_row = session.get(UserModel, user.id)
        finally:
            session.close()
    prev_coach = _last_coach_message(user_row.id)
    extracted = _extract_data_from_message(incoming_message, user_row,
                                           last_coach_message=prev_coach)
    for k in code_found:                      # code beats the model on what it parsed
        extracted.pop(k, None)
    if code_found.get("height_ft"):
        extracted.pop("height_cm", None)
    if code_found.get("weight_lbs"):
        extracted.pop("weight_kg", None)
    non_null = {k: v for k, v in extracted.items() if v is not None}
    found_any = dict(code_found, **non_null)
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

    # A message that IS a day list ("chest and biceps, back and triceps, legs and
    # shoulders") is their split whether or not the extractor caught it. Live
    # 2026-09-22 (user 43): this exact message survived only as "bro_split" → the
    # card fell to full_body. Code parses; nothing is guessed from partial matches.
    try:
        if not (user_row.split_days or []):
            from workouts.routine import maybe_capture_split_days
            r = maybe_capture_split_days(user_row.id, incoming_message, source="onboarding")
            if r:
                logger.info("SPLIT_DAYS_ONBOARDING user=%s result=%s", user_row.id, r)
                session = get_session()
                try:
                    user_row = session.get(UserModel, user.id)
                finally:
                    session.close()
    except Exception as e:  # noqa: BLE001 — never block the reply on it
        logger.warning("SPLIT_DAYS_ONBOARDING_FAILED user=%s err=%s", user_row.id, e)

    # A stated lift ("i bench 135", "squat 185 for 5") is the first card's calibration.
    # Onboarding has no tools, so code catches the stating forms (never goals).
    try:
        from workouts.calibrate import maybe_capture_stated_anchors
        r = maybe_capture_stated_anchors(user_row.id, incoming_message, source="onboarding")
        if r and r.get("saved"):
            logger.info("LIFT_ANCHORS_ONBOARDING user=%s saved=%s", user_row.id, r["saved"])
    except Exception as e:  # noqa: BLE001 — never block the reply on it
        logger.warning("LIFT_ANCHORS_ONBOARDING_FAILED user=%s err=%s", user_row.id, e)

    # An explicit "remind me / ping me" is a promise: onboarding has no tools, so a
    # small extraction sets (or corrects) the reminder in code and the prompt shows it.
    try:
        from reminders import maybe_capture_onboarding_reminder
        maybe_capture_onboarding_reminder(user_row.id, incoming_message, _conversation_so_far(user_row.id))
    except Exception as e:  # noqa: BLE001 — never block the reply on it
        logger.warning("REMINDER_ONBOARDING_CAPTURE_FAILED user=%s err=%s", user_row.id, e)

    # Wake/sleep: a qualitative answer to a wake/sleep ask takes the coach's proposed
    # times (or, after enough asks, a descriptor default) instead of a third ask.
    estimated = None
    try:
        estimated = _maybe_estimate_wake_sleep(user_row, incoming_message, prev_coach)
        if estimated:
            found_any = dict(found_any, **estimated)
            session = get_session()
            try:
                user_row = session.get(UserModel, user.id)
            finally:
                session.close()
    except Exception as e:  # noqa: BLE001 — never block the reply on it
        logger.warning("ONBOARDING_WAKE_SLEEP_ESTIMATE_FAILED user=%s err=%s", user_row.id, e)

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

    # ── Done — or they want the workout and the card-critical basics are in ──
    # The summary and completion happen in the SAME message (founder, 2026-10-01):
    # no "sound right?" round trip. Corrections go to the coach's tools.
    wants_workout = _wants_workout(incoming_message)
    card_missing = [f for f in missing_after if f[0] in CARD_CRITICAL]
    early = bool(missing_after) and wants_workout and not card_missing
    if not missing_after or early:
        if not _is_bare_answer(incoming_message, code_found) or wants_workout:
            try:
                text = _build_completion_reaction(user_row, incoming_message, system_prompt, early=early)
            except Exception as e:  # noqa: BLE001 — the summary still goes out
                logger.warning("ONBOARDING_COMPLETION_REACTION_FAILED user=%s err=%s", user_row.id, e)
                text = ""
            if text and text.strip():
                send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
        if early:
            logger.info("ONBOARDING_EARLY_EXIT user=%s wants_workout=True learned_later=%s",
                        user_row.id, [f[0] for f in missing_after])
        return _complete_onboarding(user_row, incoming_message)

    # ── They want the workout but the card can't be built yet → ask for exactly that ──
    if wants_workout and card_missing:
        text = _build_card_ask(user_row, incoming_message, system_prompt, card_missing)
        send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
        logger.info("ONBOARDING_REPLY mode=card_ask user=%s still_unknown=%s card_missing=%s",
                    user_row.id, [f[0] for f in missing_after], [f[0] for f in card_missing])
        return False

    # ── Still getting to know them ──────────────────────────────────────────
    # Friend reply by default (one woven question max). The big ask and the
    # two-field bundle are kept for when a list is the right move — see
    # _intake_mode(): they asked for it, the conversation has run long with most
    # fields unknown, or one/two are left to close out.
    is_cont, prev_body, why = _continuation_state(user_row.id, found_any, incoming_message)
    if is_cont:
        text = _build_continuation_reply(user_row, incoming_message, system_prompt, prev_body)
        if not text or text.strip().lower() in ("[silent]", "silent"):
            logger.info("ONBOARDING_CONTINUATION user=%s why=%s sent=silent", user_row.id, why)
            return False
        send_sms(user_row.phone, text, user_id=user_row.id, message_type=FOLLOWUP_MESSAGE_TYPE)
        logger.info("ONBOARDING_CONTINUATION user=%s why=%s sent=line", user_row.id, why)
        return False

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
    """Finalize onboarding: bound any self-stated targets, compute the rest, store
    confirmed decisions, send the ONE summary bubble (code-authored, every number
    real), then the first card (card setup) and the rundown."""
    from models import get_session, User as UserModel, Message

    clamp_note = _reconcile_user_targets(user.id)   # "staying under 2000 cals" said mid-chat → bounded
    session = get_session()
    try:
        user = session.get(UserModel, user.id)
        session.expunge(user)
    finally:
        session.close()
    targets = calculate_targets(user)

    session = get_session()
    try:
        user_row = session.get(UserModel, user.id)

        if getattr(user_row, "targets_source", None) == "user" and user_row.calorie_target and user_row.protein_target:
            # They named numbers during the conversation (bounded above) — keep their pick.
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
        first_out = (session.query(Message.created_at)
                     .filter(Message.user_id == user_row.id, Message.direction == "out",
                             Message.message_type.in_(("onboarding", BIG_ASK_MESSAGE_TYPE)))
                     .order_by(Message.id).first())
        minutes = (round((datetime.now(timezone.utc).replace(tzinfo=None) - first_out[0]).total_seconds() / 60, 1)
                   if first_out and first_out[0] else None)
        still = [f[0] for f in _get_missing_fields(user_row)]
        logger.info(f"Onboarding complete for {user_row.name} — {targets['calories']} cal, {targets['protein']}g protein, "
                    f"bmr={targets['bmr']} ({targets.get('bmr_formula', 'mifflin')}), tdee={targets['tdee']}, "
                    f"goal_pct={targets.get('goal_pct')}, limits={targets.get('goal_limits')}, "
                    f"source={user_row.targets_source}, branch={user_row.coaching_branch}, "
                    f"turns={_coach_turns(user_row.id)}, minutes={minutes}, learned_later={still}")

        text = _build_confirmation_summary(user_row, clamp_note=clamp_note)
        send_sms(user_row.phone, text, user_id=user_row.id, message_type="onboarding")
        system_prompt = _build_system_prompt(user_row)
        rundown_user = user_row

    finally:
        session.close()

    # Second: the card setup step (workouts/card_setup.py) — extension framing, their
    # first card, the tour — for iMessage users. Before the rundown: the card is the
    # point, the rundown can wait its beat. The water offer then comes by its sweep
    # (~HEARTBEAT_ACTIVE_CONVO_MINUTES after the conversation goes quiet): one
    # code-answered question on the floor at a time. Flag off → the water offer at
    # kickoff as before (water_offer.py, one line, once, answered in code).
    if config.CARD_SETUP_ENABLED:
        try:
            from workouts.card_setup import run_onboarding_setup
            logger.info("CARD_SETUP_KICKOFF user=%s result=%s", user.id, run_onboarding_setup(user.id))
        except Exception as e:  # noqa: BLE001
            logger.warning("CARD_SETUP_KICKOFF_FAILED user=%s err=%s", user.id, e)
    elif config.WATER_OFFER_ENABLED:
        try:
            from water_offer import send_offer as _water_offer
            _water_offer(user.id, source="kickoff")
        except Exception as e:  # noqa: BLE001
            logger.warning("WATER_OFFER_KICKOFF_FAILED user=%s err=%s", user.id, e)

    # Then, a beat later: how to use me — from capabilities.py for THIS user, with
    # the profile link. Best-effort; the summary and the card already went out.
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
