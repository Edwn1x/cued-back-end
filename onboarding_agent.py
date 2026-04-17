"""
Onboarding Agent — Cued
========================
Triggered immediately after a new user signs up.

Architecture: Send ONE welcome message immediately, then stop.
Everything after that is driven by the user replying — handled via the normal
webhook flow, with the onboarding skill loaded for early exchanges.

We track onboarding state on the user record:
  onboarding_step = 0: not started
  onboarding_step = 1: welcome sent, waiting for first reply
  onboarding_step = 2: clarification question sent, waiting for answer
  onboarding_step = 3: tools question sent, waiting for answer
  onboarding_step = 4: complete — hand off to normal coaching

The webhook calls handle_onboarding_reply() on every incoming message
while onboarding_step < 4. Once complete, normal coaching takes over.
"""

import os
import logging
import threading

from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, COACH_MODEL, MAX_RESPONSE_TOKENS
from sms import send_sms

logger = logging.getLogger("cued.onboarding")
client = Anthropic(api_key=ANTHROPIC_API_KEY)

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")


def load_skill(skill_name: str) -> str:
    path = os.path.join(SKILLS_DIR, skill_name, "SKILL.md")
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        logger.warning(f"Skill not found: {skill_name}")
        return ""


def _build_system_prompt(user, user_summary: str) -> str:
    return f"""{load_skill("personality")}

---

{load_skill("onboarding")}

---

## USER PROFILE
{user_summary}
"""


def _build_user_summary(user) -> str:
    parts = [
        f"Name: {user.name}",
        f"Age: {user.age}" if user.age else None,
        f"Gender: {user.gender}" if user.gender and user.gender != "prefer_not_to_say" else None,
        f"Occupation: {user.occupation}" if user.occupation else None,
        f"Height: {user.height_ft}'{user.height_in}\"" if user.height_ft else None,
        f"Weight: {user.weight_lbs} lbs" if user.weight_lbs else None,
        f"Goals: {user.goal}" if user.goal else None,
        f"Biggest obstacle: {user.biggest_obstacle}" if user.biggest_obstacle else None,
        f"Experience: {user.experience}" if user.experience else None,
        f"Equipment: {user.equipment}" if user.equipment else None,
        f"Injuries: {user.injuries}" if user.injuries else None,
        f"Diet: {user.diet}" if user.diet else None,
        f"Cooking situation: {user.cooking_situation}" if user.cooking_situation else None,
        f"Workout days: {user.workout_days}" if user.workout_days else None,
        f"Workout time: {user.workout_time}" if user.workout_time else None,
        f"Wake time: {user.wake_time}" if user.wake_time else None,
        f"Sleep quality: {user.sleep_quality}" if user.sleep_quality else None,
        f"Motivation: {user.motivation}" if user.motivation else None,
    ]
    return "\n".join(p for p in parts if p)


def _pick_clarification_question(user) -> tuple[str, str]:
    """
    Return (instruction_for_claude, topic_tag) for the single most important
    clarifying question. Priority: goal ambiguity > injury > sleep > food.
    """
    goals = [g.strip() for g in (user.goal or "").split(",") if g.strip()]
    wants_fat_loss = "fat_loss" in goals
    wants_muscle = "muscle_building" in goals or "strength" in goals

    if wants_fat_loss and wants_muscle:
        return (
            "The user selected both fat loss and muscle building. Ask them ONE question: "
            "do they want to prioritize cutting fat or building muscle right now? "
            "Explain briefly that this changes their calorie setup. "
            "One sentence only. Casual. Do not ask anything else.",
            "recomp_vs_cut"
        )

    if user.injuries and len(user.injuries) > 5:
        return (
            f"The user listed an injury: {user.injuries}. "
            "Ask ONE follow-up: is it currently active or old? Which movements bother it? "
            "One question only. Casual, one sentence.",
            "injury_specifics"
        )

    if user.sleep_quality in ("poor", "terrible"):
        return (
            "The user reported poor sleep. Ask ONE follow-up: "
            "is it falling asleep, staying asleep, or waking too early? "
            "One question only. One sentence. Casual.",
            "sleep_issue"
        )

    cooking = (user.cooking_situation or "mix")
    if cooking in ("cook_myself", "cook_family"):
        return (
            "Ask what food they actually have at home right now — "
            "grocery haul this week, or offer to work from a fridge photo. "
            "One sentence. Casual.",
            "food_situation"
        )
    elif cooking == "mostly_eat_out":
        return (
            "Ask what restaurants they usually go to, or what's near where they work or live. "
            "One sentence. Casual.",
            "food_situation"
        )
    elif cooking == "dining_hall":
        return (
            "Ask what their dining hall usually has — what options they actually like there. "
            "One sentence.",
            "food_situation"
        )
    else:
        return (
            "Ask what their go-to meals look like — whether they're cooking or eating out most days. "
            "One sentence. Casual.",
            "food_situation"
        )


def _generate(system_prompt: str, instruction: str) -> str:
    response = client.messages.create(
        model=COACH_MODEL,
        max_tokens=MAX_RESPONSE_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": instruction}],
    )
    return response.content[0].text


def _send_welcome(user, user_summary: str):
    """Send the single welcome message. Called once at signup."""
    system_prompt = _build_system_prompt(user, user_summary)
    instruction = (
        "Send the first message to this new user. "
        "Reference ONE specific detail from their profile that shows you actually read it — "
        "their goal, their injury, their obstacle, whatever is most striking. "
        "Be warm but don't explain how the service works. "
        "Don't ask any questions. Don't tell them what's coming next. "
        "Just make them feel like they already have a coach who knows them. "
        "One message only. 1-2 sentences max."
    )
    text = _generate(system_prompt, instruction)
    send_sms(user.phone, text, user_id=user.id, message_type="onboarding")
    logger.info(f"Onboarding welcome sent to {user.name}")


def handle_onboarding_reply(user, incoming_message: str) -> bool:
    """
    Called from the webhook when a message arrives and onboarding_step < 3.
    Generates and sends the next onboarding message.
    Returns True if onboarding is now complete (hand off to normal coaching).
    """
    from models import get_session, User as UserModel

    session = get_session()
    try:
        user_row = session.get(UserModel, user.id)
        step = user_row.onboarding_step or 1
    finally:
        session.close()

    user_summary = _build_user_summary(user)
    system_prompt = _build_system_prompt(user, user_summary)

    if step == 1:
        # First reply — acknowledge user's message then ask the clarification question
        clarification_instruction, topic = _pick_clarification_question(user)
        full_instruction = (
            f"The user just replied to your welcome message with: \"{incoming_message}\"\n\n"
            f"First, briefly acknowledge what they said (one sentence). "
            f"Then ask this clarification question:\n{clarification_instruction}"
        )
        text = _generate(system_prompt, full_instruction)
        send_sms(user.phone, text, user_id=user.id, message_type="onboarding")
        logger.info(f"Onboarding clarification sent to {user.name} (topic: {topic})")

        session = get_session()
        try:
            user_row = session.get(UserModel, user.id)
            user_row.onboarding_step = 2
            user_row.pending_clarification_topic = topic
            session.commit()
        finally:
            session.close()
        return False

    elif step == 2:
        # Second reply — store their answer, advance to tools question
        session = get_session()
        try:
            user_row = session.get(UserModel, user.id)
            if not user_row.pending_clarification_answer:
                user_row.pending_clarification_answer = incoming_message.strip()

            # Store confirmed decision based on clarification topic
            if user_row.pending_clarification_topic == "recomp_vs_cut":
                answer_lower = incoming_message.strip().lower()
                if any(w in answer_lower for w in ["cut", "fat", "lean", "lose", "shred"]):
                    user_row.confirmed_goal_priority = "cutting"
                elif any(w in answer_lower for w in ["build", "muscle", "bulk", "size", "gain", "strength", "strong"]):
                    user_row.confirmed_goal_priority = "building"

            user_row.onboarding_step = 3
            session.commit()
        finally:
            session.close()

        instruction = (
            "Ask the user ONE question: what fitness apps or wearables do they currently use? "
            "Examples: Strava, MyFitnessPal, Apple Watch, Whoop, Oura, Fitbit, Garmin. "
            "Explain briefly that Cued replaces tracking apps but works alongside devices that capture data like runs or HRV. "
            "One message, casual, 1-2 sentences max."
        )
        text = _generate(system_prompt, instruction)
        send_sms(user.phone, text, user_id=user.id, message_type="onboarding")
        logger.info(f"Onboarding tools question sent to {user.name}")
        return False

    elif step == 3:
        # Third reply — store tools answer, acknowledge, mark complete
        session = get_session()
        try:
            user_row = session.get(UserModel, user.id)
            user_row.existing_tools = incoming_message.strip()

            answer_lower = incoming_message.strip().lower()
            if any(w in answer_lower for w in ["none", "nothing", "no apps", "don't use", "nope", "no"]):
                user_row.tools_decision = "none"
            elif any(w in answer_lower for w in ["myfitnesspal", "mfp", "cronometer", "loseit", "lose it"]):
                user_row.tools_decision = None  # coach will clarify migrate vs coexist in conversation
            else:
                user_row.tools_decision = "coexist"  # devices like Whoop/Apple Watch default to coexist

            user_row.onboarding_step = 4
            session.commit()
        finally:
            session.close()

        instruction = (
            f"The user just told you their current apps/tools: '{incoming_message.strip()}'. "
            "Acknowledge in ONE sentence. If they mentioned a tracking app like MyFitnessPal, "
            "say something like 'You can keep using MFP and send me your daily totals, or we can migrate "
            "and I'll track directly — your call, we can sort that out as we go.' "
            "If they mentioned devices like Whoop or Apple Watch, say something like 'Keep using those "
            "for the data — I'll process it when you share it with me.' "
            "If they said none, acknowledge casually. "
            "ONE message. No follow-up questions."
        )
        text = _generate(system_prompt, instruction)
        send_sms(user.phone, text, user_id=user.id, message_type="onboarding")
        logger.info(f"Onboarding complete for {user.name}")
        return True

    # step >= 4: already complete
    return True


def start_onboarding(user):
    """
    Entry point — called from app.py after successful signup.
    Sends the welcome message in a background thread, then stops.
    All subsequent onboarding is driven by the user replying.
    """
    user_summary = _build_user_summary(user)

    def _run():
        try:
            _send_welcome(user, user_summary)
            # Set onboarding_step to 1 after welcome is sent
            from models import get_session, User as UserModel
            session = get_session()
            try:
                user_row = session.get(UserModel, user.id)
                if user_row and not user_row.onboarding_step:
                    user_row.onboarding_step = 1
                    session.commit()
            finally:
                session.close()
        except Exception as e:
            logger.error(f"Onboarding welcome failed for {user.name}: {e}")

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    logger.info(f"Onboarding thread started for {user.name}")
