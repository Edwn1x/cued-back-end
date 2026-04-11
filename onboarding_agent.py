"""
Onboarding Agent — Cued
========================
Triggered immediately after a new user signs up.
Sends a 3-4 message welcome sequence that proves the coach read their profile,
sets expectations, asks a follow-up question, and previews tomorrow.

Messages are spaced out to feel like a real conversation, not a dump.
If the user replies at any point, the sequence stops and normal coaching takes over.
"""

import os
import time
import logging
import threading
from datetime import datetime

from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, COACH_MODEL, MAX_RESPONSE_TOKENS
from sms import send_sms
from macro_calculator import get_or_compute_targets

logger = logging.getLogger("cued.onboarding")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Load the onboarding and personality skills
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")

def load_skill(skill_name):
    """Load a skill's SKILL.md content."""
    path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    try:
        with open(path, "r") as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"Skill not found: {skill_name}")
        return ""


def build_user_summary(user):
    """Build a concise user summary from their profile for the onboarding prompt."""
    parts = [
        f"Name: {user.name}",
        f"Age: {user.age}" if user.age else None,
        f"Gender: {user.gender}" if user.gender else None,
        f"Occupation: {user.occupation}" if user.occupation else None,
        f"Height: {user.height_ft}'{user.height_in}\"" if user.height_ft else None,
        f"Weight: {user.weight_lbs} lbs" if user.weight_lbs else None,
        f"Body fat: {user.body_fat_pct}%" if user.body_fat_pct else None,
        f"Goals: {user.goal}" if user.goal else None,
        f"Biggest obstacle: {user.biggest_obstacle}" if user.biggest_obstacle else None,
        f"Experience: {user.experience}" if user.experience else None,
        f"Equipment: {user.equipment}" if user.equipment else None,
        f"Injuries: {user.injuries}" if user.injuries else None,
        f"Diet: {user.diet}" if user.diet else None,
        f"Food restrictions: {user.restrictions}" if user.restrictions else None,
        f"Cooking situation: {user.cooking_situation}" if user.cooking_situation else None,
        f"Meals per day: {user.meals_per_day}" if user.meals_per_day else None,
        f"Workout days: {user.workout_days}" if user.workout_days else None,
        f"Workout time: {user.workout_time}" if user.workout_time else None,
        f"Wake time: {user.wake_time}" if user.wake_time else None,
        f"Bedtime: {user.sleep_time}" if user.sleep_time else None,
        f"Sleep quality: {user.sleep_quality}" if user.sleep_quality else None,
        f"Stress level: {user.stress_level}" if user.stress_level else None,
        f"Activity level: {user.activity_level}" if user.activity_level else None,
        f"Wearable: {user.wearable}" if user.wearable and user.wearable != "none" else None,
        f"Prior coaching: {user.prior_coaching}" if user.prior_coaching else None,
        f"Motivation: {user.motivation}" if user.motivation else None,
    ]
    return "\n".join([p for p in parts if p])


def _pick_clarification_question(user) -> tuple[str, str]:
    """
    Pick the single most important clarifying question to ask during onboarding,
    based on what would most materially change the coaching plan.
    Returns (prompt_for_claude, topic_tag).
    Priority order: goal ambiguity > injury specifics > sleep issues > food situation.
    """
    goals = [g.strip() for g in (user.goal or "").split(",") if g.strip()]
    wants_fat_loss = "fat_loss" in goals
    wants_muscle = "muscle_building" in goals or "strength" in goals

    # 1. Ambiguous goal (recomp vs cut) — affects every calorie and macro number
    if wants_fat_loss and wants_muscle:
        return (
            "Generate Message 3 from the onboarding sequence: ask whether the user wants to prioritize "
            "cutting fat or building muscle, since they selected both. Frame it practically — "
            "explain that this changes their calorie target and that you need to know which direction "
            "to set up. One question only, casual, one sentence.",
            "recomp_vs_cut"
        )

    # 2. Injury specifics — affects every workout
    if user.injuries and len(user.injuries) > 5:
        return (
            f"Generate Message 3 from the onboarding sequence: the user listed an injury ({user.injuries}). "
            "Ask one specific follow-up — which movements it affects, whether it's currently active or old, "
            "and whether they've seen anyone for it. One question only, casual.",
            "injury_specifics"
        )

    # 3. Poor sleep — affects readiness and recovery
    if user.sleep_quality in ("poor", "terrible"):
        return (
            "Generate Message 3 from the onboarding sequence: the user reported poor sleep quality. "
            "Ask one specific follow-up — is it falling asleep, staying asleep, or waking too early? "
            "This affects recovery and morning energy planning. One question only, casual.",
            "sleep_issue"
        )

    # 4. Food situation — affects every meal suggestion
    cooking_situation = getattr(user, 'cooking_situation', 'mix') or 'mix'
    if cooking_situation in ("cook_myself", "cook_family"):
        return (
            "Generate Message 3 from the onboarding sequence: ask about what food they actually have. "
            "Something like: 'What'd you grab from the store this week?' or 'Snap a pic of your fridge and I'll build meals from what you've got.' "
            "Keep it casual — one sentence. Mention you can work with a fridge photo if they want.",
            "food_situation"
        )
    elif cooking_situation == "mostly_eat_out":
        return (
            "Generate Message 3 from the onboarding sequence: ask about where they actually eat. "
            "Something like: 'What restaurants do you usually hit?' or 'What's near your work for lunch?' "
            "One sentence, casual.",
            "food_situation"
        )
    elif cooking_situation == "dining_hall":
        return (
            "Generate Message 3 from the onboarding sequence: ask about their dining hall situation. "
            "Something like: 'What does your dining hall usually have?' or 'Any options you actually like there?' "
            "One sentence.",
            "food_situation"
        )
    else:
        return (
            "Generate Message 3 from the onboarding sequence: ask about their real food situation. "
            "Something like: 'What's your go-to when you're cooking vs eating out?' or 'What restaurants are usually in the mix?' "
            "One sentence, casual.",
            "food_situation"
        )


def generate_onboarding_message(user, message_number, user_summary):
    """Generate a specific onboarding message using Claude with skills."""
    
    personality_skill = load_skill("personality")
    onboarding_skill = load_skill("onboarding")
    
    current_hour = datetime.now().hour
    is_late = current_hour >= 22 or current_hour < 6
    
    system_prompt = f"""
{personality_skill}

---

{onboarding_skill}

---

## USER PROFILE
{user_summary}

## CURRENT CONTEXT
Current time: {datetime.now().strftime('%I:%M %p')}
Late night (after 10pm): {'Yes — compress to welcome only, tell them you start tomorrow, say goodnight.' if is_late else 'No'}
Message number in sequence: {message_number} of 4
"""

    clarification_prompt, clarification_topic = _pick_clarification_question(user)

    message_instructions = {
        1: "Generate Message 1 from the onboarding sequence: the immediate welcome. Reference something specific from their profile. Make it feel personal, not automated. Keep the JARVIS tone but slightly warmer since this is first contact. 2-3 sentences max.",
        2: "Generate Message 2 from the onboarding sequence: set expectations for how the coaching works. Tell them what to expect tomorrow and how to interact (W for workout, M for meal swap, etc). Keep it brief and practical. 2-3 sentences.",
        3: clarification_prompt,
        4: "Generate Message 4 from the onboarding sequence: preview tomorrow. Build anticipation for their first coaching day. Mention their wake time if available. 1-2 sentences. End with a goodnight."
    }
    
    response = client.messages.create(
        model=COACH_MODEL,
        max_tokens=MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": message_instructions.get(message_number, message_instructions[1])
        }]
    )

    text = response.content[0].text
    if message_number == 3:
        return text, clarification_topic
    return text, None


def check_user_replied(user_id):
    """Check if the user has sent any messages since signup. If so, stop the sequence."""
    from models import get_session, Message
    session = get_session()
    try:
        incoming = session.query(Message).filter_by(
            user_id=user_id,
            direction="incoming"
        ).count()
        return incoming > 0
    finally:
        session.close()


def run_onboarding_sequence(user):
    """
    Run the full onboarding sequence for a new user.
    Sends 3-4 messages spaced out over ~20 minutes.
    Stops if the user replies at any point.
    
    This runs in a background thread so it doesn't block the signup response.
    """
    user_summary = build_user_summary(user)
    current_hour = datetime.now().hour
    is_late = current_hour >= 22 or current_hour < 6
    
    logger.info(f"Starting onboarding for {user.name} (ID: {user.id})")
    
    # Message 1 — immediate welcome
    try:
        msg1, _ = generate_onboarding_message(user, 1, user_summary)
        send_sms(user.phone, msg1, user_id=user.id, message_type="onboarding")
        logger.info(f"Onboarding msg 1 sent to {user.name}")
    except Exception as e:
        logger.error(f"Onboarding msg 1 failed for {user.name}: {e}")
        return
    
    # If it's late night, stop after message 1
    if is_late:
        logger.info(f"Late night signup for {user.name} — onboarding compressed to msg 1 only")
        return
    
    # Message 2 — 5 minutes later
    time.sleep(300)  # 5 minutes
    if check_user_replied(user.id):
        logger.info(f"User {user.name} replied — stopping onboarding sequence")
        return

    try:
        msg2, _ = generate_onboarding_message(user, 2, user_summary)
        send_sms(user.phone, msg2, user_id=user.id, message_type="onboarding")
        logger.info(f"Onboarding msg 2 sent to {user.name}")
    except Exception as e:
        logger.error(f"Onboarding msg 2 failed for {user.name}: {e}")
        return

    # Targets explanation — sent ~1 minute after msg 2, before the food question
    time.sleep(60)
    if check_user_replied(user.id):
        logger.info(f"User {user.name} replied — stopping onboarding sequence")
        return

    try:
        from models import get_session
        session = get_session()
        try:
            macro_result = get_or_compute_targets(user, session)
            # Mark targets as explained
            user_row = session.query(__import__('models').User).get(user.id)
            if user_row:
                user_row.targets_explained = True
                session.commit()
        finally:
            session.close()

        # Build the targets message
        targets_msg = macro_result.explanation
        if macro_result.is_ambiguous:
            targets_msg += f" — {macro_result.ambiguity_note}"

        send_sms(user.phone, targets_msg, user_id=user.id, message_type="targets_explanation")
        logger.info(f"Targets explanation sent to {user.name}: {macro_result.calorie_target} cal, {macro_result.protein_target}g protein")
    except Exception as e:
        logger.error(f"Targets explanation failed for {user.name}: {e}")
        # Non-fatal — continue sequence

    # Message 3 — food question (1 min after targets explanation)
    time.sleep(60)
    if check_user_replied(user.id):
        logger.info(f"User {user.name} replied — stopping onboarding sequence")
        return
    
    try:
        msg3, clarification_topic = generate_onboarding_message(user, 3, user_summary)
        send_sms(user.phone, msg3, user_id=user.id, message_type="onboarding")
        logger.info(f"Onboarding msg 3 sent to {user.name} (topic: {clarification_topic})")

        # Tag the pending clarification so the system knows what to listen for
        if clarification_topic:
            from models import get_session, User as UserModel
            session = get_session()
            try:
                user_row = session.query(UserModel).get(user.id)
                if user_row:
                    user_row.pending_clarification_topic = clarification_topic
                    session.commit()
            finally:
                session.close()
    except Exception as e:
        logger.error(f"Onboarding msg 3 failed for {user.name}: {e}")
        return

    # Message 4 — 10 more minutes
    time.sleep(600)
    if check_user_replied(user.id):
        logger.info(f"User {user.name} replied — stopping onboarding sequence")
        return

    try:
        msg4, _ = generate_onboarding_message(user, 4, user_summary)
        send_sms(user.phone, msg4, user_id=user.id, message_type="onboarding")
        logger.info(f"Onboarding msg 4 sent to {user.name}")
    except Exception as e:
        logger.error(f"Onboarding msg 4 failed for {user.name}: {e}")


def start_onboarding(user):
    """
    Entry point — called from app.py after successful signup.
    Runs the onboarding sequence in a background thread.
    """
    thread = threading.Thread(
        target=run_onboarding_sequence,
        args=(user,),
        daemon=True
    )
    thread.start()
    logger.info(f"Onboarding thread started for {user.name}")
