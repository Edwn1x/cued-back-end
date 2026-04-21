import os
import logging
import threading
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS
from models import init_db, get_session, User, Message, Workout, DailyLog, confirm_workout_today, is_workout_confirmed_today, resolve_pending_clarification, maybe_infer_training_days
from sms import send_sms, log_incoming, get_twiml_response
from coach import get_coach_response, parse_workout_log
from scheduler import start_scheduler, schedule_user
import config
from onboarding_agent import start_onboarding, handle_onboarding_reply
from admin_dashboard import ADMIN_HTML
from engagement_tracker import reset_unanswered
from tone_analyzer import maybe_update_style
from message_buffer import buffer_message

# ─── Setup ──────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
CORS(app, origins=config.ALLOWED_ORIGINS)

# Initialize DB on startup
init_db()


# ─── Decision Extractor ──────────────────────────────
def extract_and_store_decisions(user_id: int, user_message: str, coach_response: str):
    """After each exchange, check if any decisions or profile data were confirmed and store them."""
    import anthropic
    import json

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""Analyze this SMS coaching exchange and extract any CONFIRMED settings, decisions, or profile data that should be stored permanently.

Extract a value if EITHER:
1. The user explicitly stated it in this exchange, OR
2. The coach is treating it as a settled fact (referencing it confidently without asking the user to confirm)

For example:
- Coach says "you're at 1050/2200 cal" → calorie_target is 2200
- Coach says "we're targeting 145g protein" → protein_target is 145
- Coach mentions "at 146lbs" → weight_lbs is 146
- User says "I'm 5'6"" → height_ft is 5, height_in is 6
- User says "I'm 20" → age is 20
- User says "lets do cutting" → goal_priority is "cutting"
- Coach says "since you're cutting" → goal_priority is "cutting"
- User says "I walk around campus" → activity_level is "lightly_active"
- User describes their meals/eating habits → food_context captures the summary
- User says "I usually wake up at 7" → wake_time is "07:00"
- User says "I try to sleep by 11" → sleep_time is "23:00"

User said: "{user_message}"
Coach said: "{coach_response}"

Return ONLY valid JSON with these fields (use null for anything not mentioned or confirmed):
{{
  "age": number or null,
  "goal_priority": "cutting" or "building" or "recomp" or null,
  "calorie_target": number or null,
  "protein_target": number or null,
  "training_split": "ppl" or "upper_lower" or "full_body" or "bro_split" or null,
  "workout_time": "HH:MM" or null,
  "training_days": "mon,tue,wed..." or null,
  "height_ft": number or null,
  "height_in": number or null,
  "weight_lbs": number or null,
  "activity_level": "sedentary" or "lightly_active" or "active" or "very_active" or null,
  "wake_time": "HH:MM" or null,
  "sleep_time": "HH:MM" or null,
  "food_context": "brief description of what they eat/cook/order, or null"
}}

If nothing can be extracted, return all null."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        if "}" in text:
            text = text[:text.rindex("}") + 1]
        data = json.loads(text)

        session = get_session()
        try:
            user = session.get(User, user_id)
            if not user:
                return

            changed = False
            if data.get("age") and not user.age:
                user.age = data["age"]
                changed = True
            if data.get("goal_priority") and not user.confirmed_goal_priority:
                user.confirmed_goal_priority = data["goal_priority"]
                changed = True
            if data.get("calorie_target") and not user.calorie_target:
                user.calorie_target = data["calorie_target"]
                changed = True
            if data.get("protein_target") and not user.protein_target:
                user.protein_target = data["protein_target"]
                changed = True
            if data.get("training_split") and not user.confirmed_training_split:
                user.confirmed_training_split = data["training_split"]
                changed = True
            if data.get("workout_time") and not user.confirmed_workout_time:
                user.confirmed_workout_time = data["workout_time"]
                changed = True
            if data.get("training_days") and not user.confirmed_training_days:
                user.confirmed_training_days = data["training_days"]
                changed = True
            if data.get("height_ft") and not user.height_ft:
                user.height_ft = data["height_ft"]
                changed = True
            if data.get("height_in") is not None and user.height_in is None:
                user.height_in = data["height_in"]
                changed = True
            if data.get("weight_lbs") and not user.weight_lbs:
                user.weight_lbs = data["weight_lbs"]
                changed = True
            if data.get("activity_level") and not user.activity_level:
                user.activity_level = data["activity_level"]
                changed = True
            if data.get("wake_time") and not user.wake_time:
                user.wake_time = data["wake_time"]
                changed = True
            if data.get("sleep_time") and not user.sleep_time:
                user.sleep_time = data["sleep_time"]
                changed = True
            if data.get("food_context") and not user.food_context:
                user.food_context = data["food_context"]
                changed = True

            if changed:
                session.commit()
                logger.info(f"Stored profile data for {user.name}: {data}")
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Decision extraction failed for user {user_id}: {e}")


# ─── Memory Extractor ────────────────────────────────
def extract_and_store_memory(user_id: int, user_message: str, coach_response: str):
    """
    After each exchange, extract any meaningful personal details the user revealed
    and append them to the user's memory field. This creates a permanent record
    of everything the coach should remember about this person.
    """
    import anthropic
    import json

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return
        existing_memory = user.memory or ""
        user_name = user.name
    finally:
        session.close()

    prompt = f"""You are extracting memorable facts about a fitness coaching client from a text message exchange. Your job is to identify any NEW personal details the user revealed that the coach should remember permanently.

Extract things like:
- Preferences (loves deadlifts, hates running, prefers morning workouts)
- Life events (sister's wedding June 15, starting new job, midterms next week)
- Injuries or physical notes (left shoulder tweaked, bad knees, tight hips)
- PRs or progress milestones (bench went from 135 to 185, squatted 225 for first time)
- Relationships (training partner named Alex, girlfriend is vegan)
- Emotional states worth remembering (stressed about finals, motivated after seeing progress)
- Food preferences and habits (eats at Crossroads dining hall, hates cilantro, lactose intolerant)
- Schedule details (Tuesdays are busy, gym closes at 10pm, travels for work monthly)
- Goals and motivations (wants to look good for wedding, training for hiking trip)
- Anything else that makes the user specific and real

Existing memory (DO NOT repeat facts already in here):
{existing_memory if existing_memory else "(no existing memory)"}

User said: "{user_message}"
Coach said: "{coach_response}"

Return ONLY valid JSON:
{{"new_facts": ["fact 1", "fact 2", ...]}}

Rules:
- Each fact should be ONE concise bullet, written as a statement about the user
- Do NOT extract temporary states ("is tired today" — only extract if it's a recurring pattern)
- Do NOT extract information the coach said unless the user confirmed it
- Do NOT repeat anything in existing memory, even if reworded
- If nothing new and meaningful was revealed, return: {{"new_facts": []}}

Examples:
User: "I'm 5'7 146, mostly cook at home, sometimes Chipotle"
→ {{"new_facts": ["5'7 and 146lbs", "Cooks at home most of the time, occasionally Chipotle"]}}

User: "My shoulder has been bugging me since that bench PR"
→ {{"new_facts": ["Shoulder started bothering them after recent bench PR"]}}

User: "yeah sounds good"
→ {{"new_facts": []}}"""

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
        new_facts = data.get("new_facts", [])

        if not new_facts:
            return

        session = get_session()
        try:
            user = session.get(User, user_id)
            if not user:
                return

            from datetime import datetime
            timestamp = datetime.now().strftime("%b %d")
            new_entries = "\n".join(f"- [{timestamp}] {fact}" for fact in new_facts)

            if user.memory:
                user.memory = f"{user.memory}\n{new_entries}"
            else:
                user.memory = new_entries

            session.commit()
            logger.info(f"Added {len(new_facts)} memory entries for {user_name}")
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Memory extraction failed for user {user_id}: {e}")


# ─── Coaching Summarizer ─────────────────────────────
def maybe_update_coaching_summary(user_id: int):
    """
    If the user has enough messages, generate a rolling summary of older
    conversations and store it. Runs periodically — every 10 new messages.
    """
    import anthropic

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return

        total_msgs = session.query(Message).filter(Message.user_id == user_id).count()

        if total_msgs < 20:
            return

        if total_msgs % 10 != 0:
            return

        older_messages = (
            session.query(Message)
            .filter(Message.user_id == user_id)
            .order_by(Message.created_at.asc())
            .limit(total_msgs - 10)
            .all()
        )

        if not older_messages:
            return

        conversation_text = "\n".join(
            f"[{m.created_at.strftime('%b %d %I:%M %p')}] {'Coach' if m.direction == 'out' else user.name}: {m.body}"
            for m in older_messages
        )

        existing_summary = user.coaching_summary or "(no prior summary)"
        user_name = user.name
    finally:
        session.close()

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""You are maintaining a rolling summary of an SMS coaching relationship. Your job is to compress old conversation history into a structured summary of what's been discussed, decided, and tried.

The user's permanent personal details (preferences, stats, life events) are stored separately in a memory system — do NOT duplicate those here. Focus only on the COACHING ARC: what topics were discussed, what decisions were made, what workouts happened, what adjustments were tried, what the user struggled with or succeeded at.

Prior summary:
{existing_summary}

Older conversation to summarize:
{conversation_text}

Return a structured summary under these headers. Keep it tight — each section 1-4 bullets max. Only include sections that have content.

## Coaching Decisions
(e.g., "Set calories at 2200 for cut", "Decided full body 2x/week over PPL")

## Workouts Completed
(e.g., "Apr 15: First full body session — squat, bench, row", "Apr 17: Upper body — bench hit 155x8")

## Adjustments Made
(e.g., "Dropped lateral raises after shoulder discomfort", "Added 15-min walk to rest days")

## Recent Themes
(e.g., "User has been asking about macros breakdown", "Focus recently on form over weight")

## Open Items
(e.g., "User hasn't confirmed training time yet", "Considering whether to add cardio")

Keep under 400 words total. This replaces the prior summary — include important past context from it if still relevant."""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        summary = response.content[0].text.strip()

        session = get_session()
        try:
            user = session.get(User, user_id)
            if user:
                user.coaching_summary = summary
                session.commit()
                logger.info(f"Updated coaching summary for {user_name} ({total_msgs} messages)")
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Summary generation failed for user {user_id}: {e}")


# ─── Buffered Message Processor ─────────────────────
def process_buffered_message(user_id: int, combined_body: str, message_type: str, image_url: dict = None):
    """Called by the message buffer after the delay expires. Processes the combined message and sends a response."""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            return

        # If user is still in onboarding, route to onboarding handler
        if (user.onboarding_step or 0) < 2:
            handle_onboarding_reply(user, combined_body)
            return

        # Get AI coaching response (routed through orchestrator)
        from orchestrator import route_message
        response_text = route_message(user, combined_body, message_type, image_data=image_url)

        # Send the response
        send_sms(user.phone, response_text, user_id=user.id, message_type=message_type)

        # Extract and store any confirmed decisions (runs in background, doesn't block)
        threading.Thread(
            target=extract_and_store_decisions,
            args=(user.id, combined_body, response_text),
            daemon=True,
        ).start()

        # Extract and store user memory (runs in background, doesn't block)
        threading.Thread(
            target=extract_and_store_memory,
            args=(user.id, combined_body, response_text),
            daemon=True,
        ).start()

        # Update coaching summary periodically (runs in background, doesn't block)
        threading.Thread(
            target=maybe_update_coaching_summary,
            args=(user.id,),
            daemon=True,
        ).start()

    except Exception as e:
        logger.error(f"Error processing buffered message for user {user_id}: {e}", exc_info=True)
        try:
            user = session.query(User).filter(User.id == user_id).first()
            if user:
                send_sms(user.phone, "Something went wrong on my end — I'll be back shortly.", user_id=user.id)
        except Exception:
            pass
    finally:
        session.close()


# ─── Goodnight Detection ─────────────────────────────
def _is_training_day_confirmation(body: str) -> bool:
    """
    Detect when a user is confirming they're training today, even without
    logging a workout. Catches replies like "yeah hitting legs" or "going to gym".
    Intentionally narrow — false positives would wrongly flag rest days.
    """
    body_lower = body.lower().strip()
    training_signals = [
        "hitting", "going to gym", "going to the gym", "heading to gym",
        "heading to the gym", "training today", "working out today",
        "gym today", "yeah gym", "yep gym", "lifting today",
        "legs today", "chest today", "back today", "arms today", "shoulders today",
        "push today", "pull today", "upper today", "lower today",
        "got a workout", "doing a workout", "getting a workout",
    ]
    return any(signal in body_lower for signal in training_signals)


def is_goodnight_signal(body: str) -> bool:
    """Detect if the user is signaling end-of-conversation."""
    body_lower = body.lower().strip()
    goodnight_phrases = [
        "goodnight", "good night", "gn", "night",
        "going to sleep", "going to bed", "gonna sleep", "gonna go to bed",
        "gts", "ttyt", "talk tomorrow", "ttyl",
        "bye", "byw", "peace out",
        "ima sleep", "ima gts", "ima go to bed",
        "heading to bed", "off to bed", "crashing now",
    ]
    if body_lower in goodnight_phrases:
        return True
    if len(body_lower) < 40:
        for phrase in goodnight_phrases:
            if phrase in body_lower:
                return True
    return False


# ─── Twilio Webhook (incoming SMS) ──────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming SMS from Twilio. Buffers messages before processing."""
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()

    # Check for MMS image
    num_media = int(request.form.get("NumMedia", 0))
    image_url = None
    image_data = None
    if num_media > 0:
        image_url = request.form.get("MediaUrl0")
        logger.info(f"MMS image received from {from_number}: {image_url}")
        if image_url:
            import requests as http_requests
            import base64
            img_response = http_requests.get(
                image_url,
                auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
            )
            if img_response.status_code == 200:
                content_type = img_response.headers.get("Content-Type", "image/jpeg")
                image_data = {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": content_type,
                        "data": base64.b64encode(img_response.content).decode("utf-8")
                    }
                }
            else:
                logger.error(f"Failed to download Twilio image: {img_response.status_code}")

    logger.info(f"Incoming SMS from {from_number}: {body}")

    session = get_session()
    try:
        user = session.query(User).filter(User.phone == from_number).first()

        if not user:
            logger.warning(f"Unknown number: {from_number}")
            return get_twiml_response(
                "Hey! You're not signed up for Cued yet. "
                "Visit cued.fit to get started."
            ), 200, {"Content-Type": "text/xml"}

        # Clear quiet_until if it's passed or if user is texting us
        if user.quiet_until:
            from datetime import datetime
            if datetime.now() >= user.quiet_until or body.strip():
                user.quiet_until = None
                session.commit()

        # Log the incoming message immediately
        log_incoming(user.id, body)

        # Reset engagement decay counter on any reply
        reset_unanswered(user.id)

        # Update mirroring style
        maybe_update_style(user.id)

        # Resolve pending clarification
        resolve_pending_clarification(user.id, body)

        # Classify the message
        message_type = classify_message(body, has_image=image_url is not None)

        # Track workout intent
        if message_type == "workout_log":
            parsed = parse_workout_log(user, body)
            if parsed:
                workout = Workout(
                    user_id=user.id,
                    workout_type="logged",
                    exercises=parsed.get("exercises", []),
                    user_notes=body,
                    completed=True,
                )
                session.add(workout)
                session.commit()
            confirm_workout_today(user.id)
            threading.Thread(target=maybe_infer_training_days, args=(user.id,), daemon=True).start()

        if message_type == "workout_request":
            confirm_workout_today(user.id)
            threading.Thread(target=maybe_infer_training_days, args=(user.id,), daemon=True).start()

        # Catch training-day confirmations that don't look like workout logs —
        # e.g. "yeah hitting legs today" in reply to the morning briefing.
        # Only fires if today's workout hasn't been confirmed yet.
        if message_type == "freeform" and not is_workout_confirmed_today(user.id):
            if _is_training_day_confirmation(body):
                confirm_workout_today(user.id)
                logger.info(f"Training day confirmed via freeform reply for {user.name}")
                threading.Thread(
                    target=maybe_infer_training_days,
                    args=(user.id,),
                    daemon=True,
                ).start()

        # Check for goodnight signal — handle immediately, skip buffer
        # Never trigger during onboarding — user is answering questions, not signing off
        if is_goodnight_signal(body) and (user.onboarding_step or 0) >= 2:
            from datetime import datetime, timedelta
            wake_time = user.wake_time or "07:00"
            wake_h, wake_m = map(int, wake_time.split(":"))
            next_wake = (datetime.now() + timedelta(days=1)).replace(
                hour=wake_h, minute=wake_m, second=0, microsecond=0
            )
            user.quiet_until = next_wake
            session.commit()

            import random
            response = random.choice([
                "Night. Get some real sleep.",
                "Sleep well. Talk tomorrow.",
                "Night, rest up.",
                "Get some rest. Hit me up in the morning.",
            ])
            send_sms(user.phone, response, user_id=user.id, message_type="goodnight")
            # Cancel any pending buffer so it doesn't flush after goodnight
            from message_buffer import cancel_buffer
            cancel_buffer(from_number)
            return get_twiml_response(), 200, {"Content-Type": "text/xml"}

        # Shorter buffer during onboarding — user is actively engaged
        if (user.onboarding_step or 0) < 2:
            buffer_delay = (25, 35)
        else:
            buffer_delay = None  # use default 90-150s

        # Buffer the message — AI call and SMS response happen after the delay
        buffer_message(
            phone=from_number,
            body=body,
            user_id=user.id,
            message_type=message_type,
            image_url=image_data,
            process_callback=process_buffered_message,
            delay_override=buffer_delay,
        )

        # Return empty TwiML immediately — response comes later via the buffer
        return get_twiml_response(), 200, {"Content-Type": "text/xml"}

    except Exception as e:
        logger.error(f"Error handling SMS from {from_number}: {e}", exc_info=True)
        return get_twiml_response("Something went wrong on my end — I'll be back shortly."), 200, {"Content-Type": "text/xml"}
    finally:
        session.close()


def classify_message(body: str, has_image: bool = False) -> str:
    """Simple heuristic to classify incoming message type."""
    body_lower = body.lower().strip()

    # Image-based classification
    if has_image:
        if any(kw in body_lower for kw in ["food", "ate", "eating", "lunch", "dinner", "breakfast", "meal", "snack"]):
            return "food_photo"
        if any(kw in body_lower for kw in ["progress", "physique", "body", "mirror", "before", "after"]):
            return "progress_photo"
        if any(kw in body_lower for kw in ["form", "check", "technique", "posture"]):
            return "form_check"
        return "food_photo"  # Default assumption for images — most common use case

    if body_lower in ("w", "workout", "send workout"):
        return "workout_request"
    if body_lower in ("m", "menu", "options", "swap"):
        return "meal_swap"
    if body_lower in ("1", "2", "3", "4", "5"):
        return "rating"
    if any(kw in body_lower for kw in ["hit", "lifted", "set", "reps", "bench", "squat", "deadlift", "press"]):
        return "workout_log"
    return "freeform"


# ─── User Signup Endpoint ───────────────────────────
@app.route("/signup", methods=["GET"])
def signup_form():
    """Simple HTML signup form."""
    return render_template_string(SIGNUP_HTML)


def safe_int(val, default=None):
    try: return int(val) if val else default
    except (ValueError, TypeError): return default

def safe_float(val, default=None):
    try: return float(val) if val else default
    except (ValueError, TypeError): return default


@app.route("/signup", methods=["POST"])
def signup_submit():
    """Handle new user signup. Accepts form-encoded data (old form) or JSON (chat overlay)."""
    session = get_session()
    try:
        # Support both form submissions and JSON from the chat overlay
        if request.is_json:
            d = request.get_json(silent=True) or {}
            def get(key, default=""):
                return d.get(key, default)
        else:
            def get(key, default=""):
                return request.form.get(key, default)

        # Normalize phone
        phone = (get("phone") or "").strip()
        if not phone.startswith("+"):
            phone = "+1" + phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")

        # Check SMS consent — JSON sends boolean true, form sends "on"/"skip"
        raw_consent = get("sms_consent")
        if request.is_json:
            sms_consent = raw_consent is True or raw_consent == "on"
            sms_skipped = raw_consent is False or raw_consent == "skip"
        else:
            sms_consent = raw_consent == "on"
            sms_skipped = raw_consent == "skip"

        if not sms_consent and not sms_skipped:
            return jsonify({"status": "error", "message": "You must agree to receive SMS messages to use Cued."})

        # Duplicate check
        existing = session.query(User).filter(User.phone == phone).first()
        if existing:
            return jsonify({"status": "exists", "message": f"{existing.name} is already signed up!"})

        # Goals: chat sends array, form sends comma-joined string
        goal_raw = get("goal", "general_fitness")
        if isinstance(goal_raw, list):
            goal_str = ",".join(goal_raw)
        else:
            goal_str = goal_raw or "general_fitness"

        user = User(
            phone=phone,
            name=(get("name") or "").strip(),
            age=safe_int(get("age")),
            gender=get("gender") or "prefer_not_to_say",
            experience=get("experience") or "none",
            goal=goal_str,
            biggest_obstacle=get("biggest_obstacle") or None,
            equipment=get("equipment") or "full_gym",
        )
        session.add(user)
        session.commit()

        if sms_consent:
            start_onboarding(user)

        logger.info(f"New user signed up: {user.name} ({user.phone}) | SMS consent: {sms_consent} | source: {'json' if request.is_json else 'form'}")
        return jsonify({"status": "ok", "message": f"Welcome {user.name}!", "name": user.name})

    except Exception as e:
        logger.error(f"Signup error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()


# ─── Activate SMS (for users who skipped consent) ───
@app.route("/activate-sms", methods=["POST"])
def activate_sms():
    """Activate SMS coaching for a user who signed up without consent."""
    session = get_session()
    try:
        phone = request.form.get("phone", "").strip()
        if not phone.startswith("+"):
            phone = "+1" + phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")

        user = session.query(User).filter(User.phone == phone).first()
        if not user:
            return jsonify({"status": "error", "message": "User not found."})

        schedule_user(user)
        start_onboarding(user)

        logger.info(f"SMS activated for existing user: {user.name} ({user.phone})")
        return jsonify({"status": "ok", "name": user.name})

    except Exception as e:
        logger.error(f"Activate SMS error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()


# ─── Admin Dashboard ────────────────────────────────
@app.route("/admin")
def admin():
    """Metrics dashboard for tracking beta performance."""
    from datetime import datetime, timedelta, timezone
    import pytz
    pst = pytz.timezone("America/Los_Angeles")
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        def as_utc(dt):
            if dt is None:
                return None
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt

        def fmt_pst(dt):
            if not dt:
                return "—"
            dt = as_utc(dt)
            return dt.astimezone(pst).strftime("%b %d, %I:%M %p")
 
        # ── USER STATS ──
        all_users = session.query(User).all()
        total_users = len(all_users)
        active_users = sum(1 for u in all_users if u.active)
 
        # ── MESSAGE STATS ──
        all_messages = session.query(Message).all()
        total_sent = sum(1 for m in all_messages if m.direction == "out")
        total_received = sum(1 for m in all_messages if m.direction == "in")
        response_rate = round((total_received / total_sent * 100) if total_sent > 0 else 0)

        # ── TODAY'S ACTIVITY ──
        today_active = 0
        for u in all_users:
            user_msgs_today = [m for m in all_messages
                               if m.user_id == u.id
                               and m.direction == "in"
                               and as_utc(m.created_at) >= today_start]
            if user_msgs_today:
                today_active += 1

        # ── RETENTION COHORTS ──
        d1_responded = 0
        for u in all_users:
            user_incoming = [m for m in all_messages if m.user_id == u.id and m.direction == "in"]
            if user_incoming:
                d1_responded += 1
        d1_rate = round((d1_responded / total_users * 100) if total_users > 0 else 0)

        def active_in_window(days):
            cutoff = now - timedelta(days=days)
            count = 0
            eligible = 0
            for u in all_users:
                if u.created_at and as_utc(u.created_at) <= cutoff:
                    eligible += 1
                    user_msgs = [m for m in all_messages
                                 if m.user_id == u.id
                                 and m.direction == "in"
                                 and as_utc(m.created_at) >= cutoff]
                    if user_msgs:
                        count += 1
                elif not u.created_at:
                    eligible += 1
            return count, eligible

        d7_active, d7_eligible = active_in_window(7)
        d14_active, d14_eligible = active_in_window(14)
        d30_active, d30_eligible = active_in_window(30)
        d7_rate = round((d7_active / d7_eligible * 100) if d7_eligible > 0 else 0)
        d14_rate = round((d14_active / d14_eligible * 100) if d14_eligible > 0 else 0)
        d30_rate = round((d30_active / d30_eligible * 100) if d30_eligible > 0 else 0)

        # ── RATINGS ──
        rating_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        total_rating_sum = 0
        total_ratings = 0
        for m in all_messages:
            if m.direction == "in" and m.body and m.body.strip() in ["1", "2", "3", "4", "5"]:
                r = int(m.body.strip())
                rating_counts[r] += 1
                total_rating_sum += r
                total_ratings += 1
        avg_rating = round(total_rating_sum / total_ratings, 1) if total_ratings > 0 else 0
        max_rating_count = max(rating_counts.values()) if any(rating_counts.values()) else 1

        # ── MEAL STATS ──
        from models import Meal, WeightLog
        all_meals = session.query(Meal).all()
        total_meals = len(all_meals)
        meals_today = sum(1 for m in all_meals if as_utc(m.eaten_at) >= today_start)
        avg_meal_calories = round(
            sum(m.calories or 0 for m in all_meals) / total_meals
        ) if total_meals > 0 else 0
        total_weight_logs = session.query(WeightLog).count()

        # ── USER TABLE DATA ──
        users_data = []
        for u in all_users:
            user_msgs = [m for m in all_messages if m.user_id == u.id]
            user_incoming = [m for m in user_msgs if m.direction == "in"]
            msg_count = len(user_msgs)

            if user_incoming:
                last_msg = max(user_incoming, key=lambda m: m.created_at)
                last_active = fmt_pst(last_msg.created_at)
                days_inactive = (now - as_utc(last_msg.created_at)).days if last_msg.created_at else 99
            else:
                last_active = "Never"
                days_inactive = 99

            workout_count = session.query(Workout).filter(Workout.user_id == u.id).count()
            meal_count = sum(1 for m in all_meals if m.user_id == u.id)

            user_ratings = [int(m.body.strip()) for m in user_incoming
                            if m.body and m.body.strip() in ["1", "2", "3", "4", "5"]]
            user_avg = round(sum(user_ratings) / len(user_ratings), 1) if user_ratings else "—"

            signed_up = fmt_pst(u.created_at) if u.created_at else "—"

            user_sent = sum(1 for m in user_msgs if m.direction == "out")
            user_received = len(user_incoming)
            cost_usd = round((user_sent + user_received) * 0.015 + user_sent * 0.006, 2)
            created_ts = int(u.created_at.timestamp()) if u.created_at else 0

            users_data.append({
                "id": u.id,
                "name": u.name,
                "phone": u.phone[-4:] if u.phone else "—",
                "signed_up": signed_up,
                "last_active": last_active,
                "msg_count": msg_count,
                "meal_count": meal_count,
                "workout_count": workout_count,
                "avg_rating": user_avg,
                "days_inactive": days_inactive,
                "onboarding_step": u.onboarding_step or 0,
                "cost_usd": cost_usd,
                "created_ts": created_ts,
            })

        # ── RECENT MESSAGES ──
        recent = sorted(all_messages, key=lambda m: m.created_at if m.created_at else now, reverse=True)[:50]
        user_map = {u.id: u.name for u in all_users}
        recent_messages_data = [{
            "time": fmt_pst(m.created_at),
            "user_name": user_map.get(m.user_id, "Unknown"),
            "user_id": m.user_id,
            "direction": m.direction,
            "body": m.body or "",
            "message_type": m.message_type or "—",
        } for m in recent]

        # ── RECENT MEALS ──
        recent_meal_rows = sorted(all_meals, key=lambda m: m.eaten_at if m.eaten_at else now, reverse=True)[:40]
        recent_meals_data = [{
            "user_id": m.user_id,
            "user_name": user_map.get(m.user_id, "Unknown"),
            "eaten_at": fmt_pst(m.eaten_at),
            "description": m.description or "",
            "calories": m.calories or 0,
            "protein_g": m.protein_g or 0,
            "source": m.source or "text",
            "confidence": m.confidence or "medium",
        } for m in recent_meal_rows]

        # ── AGENT PIPELINE STATS ──
        route_nutrition = sum(1 for m in all_messages if m.direction == "out" and m.message_type and "nutrition" in m.message_type)
        route_training = sum(1 for m in all_messages if m.direction == "out" and m.message_type and "training" in m.message_type)
        route_readiness = sum(1 for m in all_messages if m.direction == "out" and m.message_type and "readiness" in m.message_type)
        route_legacy = total_sent - route_nutrition - route_training - route_readiness

        # Message type breakdown
        from collections import Counter
        type_counts = Counter(m.message_type or "unknown" for m in all_messages if m.direction == "out")
        total_typed = sum(type_counts.values()) or 1
        message_types_data = sorted([
            {"type": t, "count": c, "pct": round(c / total_typed * 100)}
            for t, c in type_counts.most_common(12)
        ], key=lambda x: -x["count"])

        # ── COST ESTIMATES ──
        total_msg_count = total_sent + total_received
        twilio_cost = round(total_msg_count * 0.015, 2)
        api_cost = round(total_sent * 0.006, 2)
        total_cost = round(twilio_cost + api_cost, 2)
        cost_per_user = round(total_cost / active_users, 2) if active_users > 0 else 0
        cost_per_msg = round(total_cost / total_msg_count, 4) if total_msg_count > 0 else 0

        return render_template_string(ADMIN_HTML,
            now=now.astimezone(pst).strftime("%b %d, %Y %I:%M %p PST"),
            total_users=total_users,
            active_users=active_users,
            total_sent=total_sent,
            total_received=total_received,
            response_rate=response_rate,
            avg_rating=avg_rating,
            total_ratings=total_ratings,
            today_active=today_active,
            total_meals=total_meals,
            meals_today=meals_today,
            avg_meal_calories=avg_meal_calories,
            total_weight_logs=total_weight_logs,
            d1_responded=d1_responded,
            d1_rate=d1_rate,
            d7_active=d7_active, d7_eligible=d7_eligible,
            d7_rate=d7_rate,
            d14_active=d14_active, d14_eligible=d14_eligible,
            d14_rate=d14_rate,
            d30_active=d30_active, d30_eligible=d30_eligible,
            d30_rate=d30_rate,
            users=users_data,
            rating_counts=rating_counts,
            max_rating_count=max_rating_count,
            recent_messages=recent_messages_data,
            recent_meals=recent_meals_data,
            route_nutrition=route_nutrition,
            route_training=route_training,
            route_readiness=route_readiness,
            route_legacy=route_legacy,
            message_types=message_types_data,
            twilio_cost=twilio_cost,
            api_cost=api_cost,
            total_cost=total_cost,
            cost_per_user=cost_per_user,
            cost_per_msg=cost_per_msg,
        )
    finally:
        session.close()
 


# ─── Manual Send (admin override) ───────────────────
@app.route("/admin/send", methods=["POST"])
def admin_send():
    """Manually send a message to a user (admin override for when AI messes up)."""
    session = get_session()
    try:
        user_id = int(request.form.get("user_id"))
        body = request.form.get("body", "").strip()
        user = session.get(User, user_id)
        if user and body:
            send_sms(user.phone, body, user_id=user.id, message_type="admin")
            return jsonify({"status": "ok"})
        return jsonify({"status": "error"}), 400
    finally:
        session.close()


# ─── Delete User (admin) ────────────────────────────
@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id):
    """Permanently delete a user and all their data."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return jsonify({"status": "error", "message": "User not found"}), 404
        name = user.name
        from models import Meal, WeightLog
        session.query(Message).filter(Message.user_id == user_id).delete()
        session.query(Meal).filter(Meal.user_id == user_id).delete()
        session.query(WeightLog).filter(WeightLog.user_id == user_id).delete()
        session.query(Workout).filter(Workout.user_id == user_id).delete()
        session.query(DailyLog).filter(DailyLog.user_id == user_id).delete()
        session.delete(user)
        session.commit()
        logger.info(f"Admin deleted user: {name} (id={user_id})")
        return jsonify({"status": "ok", "message": f"{name} deleted."})
    finally:
        session.close()


# ─── Admin User Detail ──────────────────────────────
@app.route("/admin/user/<int:user_id>")
def admin_user(user_id):
    """Per-user detail page — profile, metrics, meals, weight, conversation."""
    from datetime import datetime, timezone, timedelta
    import pytz
    from models import Meal, WeightLog
    pst = pytz.timezone("America/Los_Angeles")
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return "User not found", 404

        now = datetime.now(timezone.utc)

        def as_utc(dt):
            if dt is None: return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        def fmt_pst(dt):
            if not dt: return "—"
            return as_utc(dt).astimezone(pst).strftime("%b %d, %I:%M %p")

        def fmt_date(dt):
            if not dt: return "—"
            return as_utc(dt).astimezone(pst).strftime("%b %d, %Y")

        # Messages
        messages = session.query(Message).filter(Message.user_id == user_id).order_by(Message.created_at).all()
        total_sent = sum(1 for m in messages if m.direction == "out")
        total_received = sum(1 for m in messages if m.direction == "in")
        response_rate = round((total_received / total_sent * 100) if total_sent > 0 else 0)

        msgs_data = [{
            "direction": m.direction,
            "body": m.body or "",
            "message_type": m.message_type or "—",
            "time": fmt_pst(m.created_at),
        } for m in messages]

        # Last active
        inbound = [m for m in messages if m.direction == "in"]
        if inbound:
            last_msg = max(inbound, key=lambda m: m.created_at)
            days_inactive = (now - as_utc(last_msg.created_at)).days
            last_active = fmt_pst(last_msg.created_at)
        else:
            days_inactive = 99
            last_active = "Never"

        # Activity over time — messages per day for last 30 days
        activity = {}
        for m in messages:
            if m.created_at:
                d = as_utc(m.created_at).astimezone(pst).strftime("%Y-%m-%d")
                activity[d] = activity.get(d, 0) + 1
        activity_days = []
        for i in range(29, -1, -1):
            d = (now - timedelta(days=i)).astimezone(pst).strftime("%Y-%m-%d")
            activity_days.append({"date": d, "count": activity.get(d, 0)})

        # Meals
        meals = session.query(Meal).filter(Meal.user_id == user_id).order_by(Meal.eaten_at.desc()).all()
        total_meals = len(meals)
        total_calories_logged = sum(m.calories or 0 for m in meals)
        total_protein_logged = sum(m.protein_g or 0 for m in meals)
        meals_data = [{
            "eaten_at": fmt_pst(m.eaten_at),
            "description": m.description or "",
            "calories": m.calories or 0,
            "protein_g": m.protein_g or 0,
            "carbs_g": m.carbs_g or 0,
            "fat_g": m.fat_g or 0,
            "source": m.source or "text",
            "log_type": m.log_type or "—",
            "confidence": m.confidence or "medium",
            "notes": m.notes or "",
        } for m in meals]

        # Weight logs
        weight_logs = session.query(WeightLog).filter(WeightLog.user_id == user_id).order_by(WeightLog.weighed_at.desc()).all()
        weight_data = [{
            "weighed_at": fmt_pst(w.weighed_at),
            "weight_lbs": w.weight_lbs,
            "notes": w.notes or "",
        } for w in weight_logs]
        weight_change = None
        if len(weight_logs) >= 2:
            weight_change = round(weight_logs[0].weight_lbs - weight_logs[-1].weight_lbs, 1)

        # Daily logs
        daily_logs = session.query(DailyLog).filter(DailyLog.user_id == user_id).order_by(DailyLog.date.desc()).limit(30).all()
        daily_logs_data = [{
            "date": fmt_date(dl.date),
            "sleep_hours": dl.sleep_hours or "—",
            "energy_level": dl.energy_level or "—",
            "daily_rating": dl.daily_rating or "—",
            "workout_confirmed": dl.workout_confirmed,
        } for dl in daily_logs]

        # Ratings
        ratings = [int(m.body.strip()) for m in inbound if m.body and m.body.strip() in ["1","2","3","4","5"]]
        avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None

        # Cost estimate
        user_cost = round((total_sent + total_received) * 0.015 + total_sent * 0.006, 2)

        # Onboarding label
        step_labels = {0: "Not started", 1: "In progress", 2: "Complete"}
        onboarding_label = step_labels.get(user.onboarding_step or 0, f"Step {user.onboarding_step}")

        return render_template_string(USER_DETAIL_HTML,
            user=user,
            messages=msgs_data,
            total_sent=total_sent,
            total_received=total_received,
            response_rate=response_rate,
            last_active=last_active,
            days_inactive=days_inactive,
            activity_days=activity_days,
            meals=meals_data,
            total_meals=total_meals,
            total_calories_logged=total_calories_logged,
            total_protein_logged=total_protein_logged,
            weight_logs=weight_data,
            weight_change=weight_change,
            daily_logs=daily_logs_data,
            avg_rating=avg_rating,
            user_cost=user_cost,
            onboarding_label=onboarding_label,
            signed_up=fmt_date(user.created_at),
        )
    finally:
        session.close()


USER_DETAIL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ user.name }} — Cued Admin</title>
<style>
:root{--bg:#050506;--surface:#111114;--card:#19191D;--border:#1F1F24;--text:#F5F5F7;--text2:#A1A1A6;--text3:#6E6E73;--accent:#7C6EFF;--green:#30D158;--yellow:#FFD60A;--red:#FF453A}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent);text-decoration:none}
a:hover{opacity:.75}

/* Header */
.header{padding:20px 28px 0;border-bottom:1px solid var(--border);background:var(--surface)}
.back{font-size:12px;color:var(--text3);display:inline-flex;align-items:center;gap:5px;margin-bottom:14px}
.header-top{display:flex;align-items:flex-end;justify-content:space-between;padding-bottom:0}
.user-title h1{font-size:20px;font-weight:700;letter-spacing:-.4px}
.user-title .meta{font-size:12px;color:var(--text3);margin-top:3px}
.status-badge{font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;background:rgba(124,110,255,.15);color:var(--accent)}

/* Tabs */
.tabs{display:flex;gap:0;margin-top:16px}
.tab{padding:10px 18px;font-size:13px;font-weight:500;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--text);border-bottom-color:var(--accent)}

/* Content */
.content{padding:24px 28px;max-width:1100px}
.tab-pane{display:none}
.tab-pane.active{display:block}

/* Stat cards */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat-label{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.stat-value{font-size:22px;font-weight:700;letter-spacing:-.5px}
.stat-sub{font-size:11px;color:var(--text3);margin-top:3px}

/* Activity heatmap */
.heatmap-wrap{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin-bottom:24px}
.heatmap-title{font-size:12px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:12px}
.heatmap{display:flex;gap:4px;align-items:flex-end;height:48px}
.heatmap-bar{flex:1;border-radius:3px 3px 0 0;min-height:4px;transition:opacity .15s;cursor:default}
.heatmap-bar:hover{opacity:.7}

/* Tables */
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:20px}
.table-header{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.table-header span{font-size:13px;font-weight:600;color:var(--text2)}
.table-header .count{font-size:12px;color:var(--text3)}
table{width:100%;border-collapse:collapse}
th{font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;padding:10px 16px;text-align:left;border-bottom:1px solid var(--border)}
td{font-size:13px;color:var(--text);padding:10px 16px;border-bottom:1px solid var(--border)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.02)}

/* Profile grid */
.profile-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.profile-section{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 20px}
.profile-section h3{font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:1.2px;margin-bottom:14px}
.prow{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border)}
.prow:last-child{border-bottom:none}
.prow .pl{font-size:12px;color:var(--text3)}
.prow .pv{font-size:13px;color:var(--text);text-align:right;max-width:60%}
.confirmed-tag{font-size:10px;color:var(--green);margin-left:6px;font-weight:600}
.memory-box{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin-bottom:20px}
.memory-box h3{font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:1.2px;margin-bottom:10px}
.memory-box p{font-size:13px;color:var(--text2);line-height:1.6;white-space:pre-wrap}

/* Conversation */
.convo-wrap{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.messages{padding:16px;display:flex;flex-direction:column;gap:10px;max-height:560px;overflow-y:auto}
.msg-row{display:flex;flex-direction:column}
.msg-row.out{align-items:flex-end}
.msg-row.in{align-items:flex-start}
.bubble{max-width:78%;padding:10px 14px;border-radius:14px;font-size:13px;line-height:1.55;word-break:break-word}
.bubble.out{background:var(--accent);color:#fff;border-radius:14px 14px 4px 14px}
.bubble.in{background:var(--surface);color:var(--text);border-radius:14px 14px 14px 4px;border:1px solid var(--border)}
.bubble-meta{font-size:10px;color:var(--text3);margin-top:3px}
.send-wrap{padding:14px 16px;border-top:1px solid var(--border);display:flex;gap:8px}
.send-wrap textarea{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:9px 12px;font-size:13px;font-family:inherit;resize:none}
.send-wrap textarea:focus{outline:none;border-color:var(--accent)}
.send-wrap button{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:9px 18px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.send-wrap button:hover{opacity:.88}

/* Badges */
.badge{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px}
.badge.photo{background:rgba(124,110,255,.2);color:var(--accent)}
.badge.text{background:rgba(48,209,88,.15);color:var(--green)}
.badge.high{background:rgba(48,209,88,.15);color:var(--green)}
.badge.medium{background:rgba(255,214,10,.15);color:var(--yellow)}
.badge.low{background:rgba(255,69,58,.15);color:var(--red)}

/* Weight chart */
.weight-list{display:flex;flex-direction:column;gap:0}

@media(max-width:700px){
  .profile-grid{grid-template-columns:1fr}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  .content{padding:16px}
}
</style>
</head>
<body>

<div class="header">
  <a href="/admin" class="back">← Dashboard</a>
  <div class="header-top">
    <div class="user-title">
      <h1>{{ user.name }}</h1>
      <div class="meta">{{ user.phone }} &nbsp;·&nbsp; ID {{ user.id }} &nbsp;·&nbsp; Signed up {{ signed_up }}</div>
    </div>
    <span class="status-badge">{{ onboarding_label }}</span>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('overview')">Overview</div>
    <div class="tab" onclick="showTab('profile')">Profile</div>
    <div class="tab" onclick="showTab('conversation')">Conversation</div>
    <div class="tab" onclick="showTab('meals')">Meals</div>
    <div class="tab" onclick="showTab('logs')">Weight & Logs</div>
  </div>
</div>

<div class="content">

  <!-- OVERVIEW TAB -->
  <div id="tab-overview" class="tab-pane active">
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Messages Sent</div>
        <div class="stat-value">{{ total_sent }}</div>
        <div class="stat-sub">coach → user</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Messages Received</div>
        <div class="stat-value">{{ total_received }}</div>
        <div class="stat-sub">user → coach</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Response Rate</div>
        <div class="stat-value">{{ response_rate }}%</div>
        <div class="stat-sub">replies / coach msgs</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Meals Logged</div>
        <div class="stat-value">{{ total_meals }}</div>
        <div class="stat-sub">{{ total_calories_logged }} cal · {{ total_protein_logged }}g protein</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Avg Rating</div>
        <div class="stat-value">{% if avg_rating %}{{ avg_rating }}/5{% else %}—{% endif %}</div>
        <div class="stat-sub">daily check-ins</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Est. Spend</div>
        <div class="stat-value">${{ user_cost }}</div>
        <div class="stat-sub">API + SMS cost</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Last Active</div>
        <div class="stat-value" style="font-size:14px">{{ last_active }}</div>
        <div class="stat-sub">{{ days_inactive }}d ago</div>
      </div>
      {% if weight_change is not none %}
      <div class="stat-card">
        <div class="stat-label">Weight Change</div>
        <div class="stat-value" style="color:{% if weight_change < 0 %}var(--green){% elif weight_change > 0 %}var(--yellow){% else %}var(--text){% endif %}">
          {% if weight_change > 0 %}+{% endif %}{{ weight_change }} lbs
        </div>
        <div class="stat-sub">first → latest log</div>
      </div>
      {% endif %}
    </div>

    <div class="heatmap-wrap">
      <div class="heatmap-title">30-Day Activity</div>
      <div class="heatmap" id="heatmap"></div>
    </div>
  </div>

  <!-- PROFILE TAB -->
  <div id="tab-profile" class="tab-pane">
    <div class="profile-grid">
      <div class="profile-section">
        <h3>Basic Info</h3>
        {% for label, val in [
          ('Age', user.age), ('Gender', user.gender), ('Occupation', user.occupation),
          ('Height', (user.height_ft|string + "'" + (user.height_in|string or '0') + '"') if user.height_ft else None),
          ('Weight', (user.weight_lbs|string + ' lbs') if user.weight_lbs else None),
          ('Body Fat', (user.body_fat_pct|string + '%') if user.body_fat_pct else None),
          ('Wearable', user.wearable)
        ] %}{% if val %}
        <div class="prow"><span class="pl">{{ label }}</span><span class="pv">{{ val }}</span></div>
        {% endif %}{% endfor %}
      </div>
      <div class="profile-section">
        <h3>Goals & Training</h3>
        {% for label, val, confirmed in [
          ('Goal', user.goal, user.confirmed_goal_priority),
          ('Experience', user.experience, None),
          ('Equipment', user.equipment, None),
          ('Training Split', user.confirmed_training_split, user.confirmed_training_split),
          ('Training Days', user.confirmed_training_days, user.confirmed_training_days),
          ('Workout Time', user.confirmed_workout_time, user.confirmed_workout_time),
          ('Workout Days', user.workout_days, None),
          ('Injuries', user.injuries, None),
          ('Activity Level', user.activity_level, None),
        ] %}{% if val %}
        <div class="prow">
          <span class="pl">{{ label }}</span>
          <span class="pv">{{ val }}{% if confirmed %}<span class="confirmed-tag">✓</span>{% endif %}</span>
        </div>
        {% endif %}{% endfor %}
      </div>
      <div class="profile-section">
        <h3>Nutrition Targets</h3>
        {% for label, val, confirmed in [
          ('Calorie Target', (user.calorie_target|string + ' cal') if user.calorie_target else None, user.calorie_target),
          ('Protein Target', (user.protein_target|string + 'g') if user.protein_target else None, user.protein_target),
          ('Diet', user.diet, None),
          ('Restrictions', user.restrictions, None),
          ('Cooking', user.cooking_situation, None),
          ('Food Context', user.food_context, None),
        ] %}{% if val %}
        <div class="prow">
          <span class="pl">{{ label }}</span>
          <span class="pv">{{ val }}{% if confirmed %}<span class="confirmed-tag">✓</span>{% endif %}</span>
        </div>
        {% endif %}{% endfor %}
      </div>
      <div class="profile-section">
        <h3>Lifestyle & Sleep</h3>
        {% for label, val in [
          ('Wake Time', user.wake_time), ('Bedtime', user.sleep_time),
          ('Sleep Quality', user.sleep_quality), ('Stress Level', user.stress_level),
          ('Motivation', user.motivation), ('Obstacle', user.biggest_obstacle),
          ('Existing Tools', user.existing_tools), ('Tools Decision', user.tools_decision),
          ('Weigh-in Day', user.weigh_in_day),
        ] %}{% if val %}
        <div class="prow"><span class="pl">{{ label }}</span><span class="pv">{{ val }}</span></div>
        {% endif %}{% endfor %}
      </div>
    </div>
    {% if user.memory %}
    <div class="memory-box">
      <h3>Coach Memory</h3>
      <p>{{ user.memory }}</p>
    </div>
    {% endif %}
  </div>

  <!-- CONVERSATION TAB -->
  <div id="tab-conversation" class="tab-pane">
    <div class="convo-wrap">
      <div class="messages" id="msg-container">
        {% for m in messages %}
        <div class="msg-row {{ m.direction }}">
          <div class="bubble {{ m.direction }}">{{ m.body }}</div>
          <div class="bubble-meta">{{ m.time }}{% if m.message_type and m.message_type != '—' %} · {{ m.message_type }}{% endif %}</div>
        </div>
        {% endfor %}
        {% if not messages %}<p style="color:var(--text3);font-size:13px;text-align:center;padding:24px">No messages yet.</p>{% endif %}
      </div>
      <div class="send-wrap">
        <textarea id="msg-body" placeholder="Send a manual message as coach..." rows="2"></textarea>
        <button onclick="sendMsg()">Send</button>
      </div>
    </div>
  </div>

  <!-- MEALS TAB -->
  <div id="tab-meals" class="tab-pane">
    <div class="stats-grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:20px">
      <div class="stat-card">
        <div class="stat-label">Total Meals</div>
        <div class="stat-value">{{ total_meals }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total Calories</div>
        <div class="stat-value">{{ total_calories_logged }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total Protein</div>
        <div class="stat-value">{{ total_protein_logged }}g</div>
      </div>
    </div>
    <div class="table-wrap">
      <div class="table-header">
        <span>Logged Meals</span>
        <span class="count">{{ total_meals }} entries</span>
      </div>
      <table>
        <thead><tr>
          <th>Time</th><th>Description</th><th>Cal</th><th>Protein</th><th>Carbs</th><th>Fat</th><th>Source</th><th>Confidence</th>
        </tr></thead>
        <tbody>
        {% for m in meals %}
        <tr>
          <td style="white-space:nowrap;color:var(--text3)">{{ m.eaten_at }}</td>
          <td>{{ m.description }}</td>
          <td>{{ m.calories }}</td>
          <td>{{ m.protein_g }}g</td>
          <td>{{ m.carbs_g }}g</td>
          <td>{{ m.fat_g }}g</td>
          <td><span class="badge {{ m.source }}">{{ m.source }}</span></td>
          <td><span class="badge {{ m.confidence }}">{{ m.confidence }}</span></td>
        </tr>
        {% endfor %}
        {% if not meals %}<tr><td colspan="8" style="text-align:center;color:var(--text3);padding:24px">No meals logged yet.</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- WEIGHT & LOGS TAB -->
  <div id="tab-logs" class="tab-pane">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <div class="table-wrap">
          <div class="table-header">
            <span>Weight Log</span>
            <span class="count">{{ weight_logs|length }} entries</span>
          </div>
          <table>
            <thead><tr><th>Date</th><th>Weight</th><th>Notes</th></tr></thead>
            <tbody>
            {% for w in weight_logs %}
            <tr>
              <td style="color:var(--text3);white-space:nowrap">{{ w.weighed_at }}</td>
              <td style="font-weight:600">{{ w.weight_lbs }} lbs</td>
              <td style="color:var(--text3)">{{ w.notes }}</td>
            </tr>
            {% endfor %}
            {% if not weight_logs %}<tr><td colspan="3" style="text-align:center;color:var(--text3);padding:20px">No weight logs yet.</td></tr>{% endif %}
            </tbody>
          </table>
        </div>
      </div>
      <div>
        <div class="table-wrap">
          <div class="table-header">
            <span>Daily Logs</span>
            <span class="count">Last 30 days</span>
          </div>
          <table>
            <thead><tr><th>Date</th><th>Sleep</th><th>Energy</th><th>Rating</th><th>Trained</th></tr></thead>
            <tbody>
            {% for dl in daily_logs %}
            <tr>
              <td style="color:var(--text3);white-space:nowrap">{{ dl.date }}</td>
              <td>{% if dl.sleep_hours != '—' %}{{ dl.sleep_hours }}h{% else %}—{% endif %}</td>
              <td>{% if dl.energy_level != '—' %}{{ dl.energy_level }}/5{% else %}—{% endif %}</td>
              <td>{% if dl.daily_rating != '—' %}{{ dl.daily_rating }}/5{% else %}—{% endif %}</td>
              <td>{% if dl.workout_confirmed %}<span style="color:var(--green)">✓</span>{% else %}—{% endif %}</td>
            </tr>
            {% endfor %}
            {% if not daily_logs %}<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:20px">No daily logs yet.</td></tr>{% endif %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

</div><!-- /content -->

<script>
const userId = {{ user.id }};
const activityDays = {{ activity_days | tojson }};

function showTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'conversation') {
    setTimeout(() => {
      const c = document.getElementById('msg-container');
      if (c) c.scrollTop = c.scrollHeight;
    }, 50);
  }
}

async function sendMsg() {
  const body = document.getElementById('msg-body').value.trim();
  if (!body) return;
  await fetch('/admin/send', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: 'user_id=' + userId + '&body=' + encodeURIComponent(body)
  });
  document.getElementById('msg-body').value = '';
  location.reload();
}

// Build heatmap
(function() {
  const hm = document.getElementById('heatmap');
  if (!hm) return;
  const max = Math.max(...activityDays.map(d => d.count), 1);
  activityDays.forEach(d => {
    const bar = document.createElement('div');
    bar.className = 'heatmap-bar';
    const pct = d.count / max;
    const h = Math.max(4, Math.round(pct * 48));
    bar.style.height = h + 'px';
    bar.style.background = d.count === 0 ? 'var(--border)' : `rgba(124,110,255,${0.2 + pct * 0.8})`;
    bar.title = d.date + ': ' + d.count + ' msg' + (d.count !== 1 ? 's' : '');
    hm.appendChild(bar);
  });
})();

// Scroll conversation to bottom on load if that tab is active
const msgC = document.getElementById('msg-container');
if (msgC) msgC.scrollTop = msgC.scrollHeight;
</script>
</body>
</html>
"""


# ─── Health Check ───────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "app": "cued", "version": "0.1.0"})


# ─── HTML Templates ─────────────────────────────────
SIGNUP_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Cued — Sign Up</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, system-ui, sans-serif; background: #18181B; color: #FAFAFA; padding: 20px; }
        .container { max-width: 480px; margin: 48px auto; padding-bottom: 60px; }
        h1 { font-size: 28px; font-weight: 700; margin-bottom: 4px; letter-spacing: -.5px; }
        .sub { color: #A1A1AA; margin-bottom: 10px; font-size: 15px; }
        .intro { color: #71717A; font-size: 13px; margin-bottom: 36px; line-height: 1.6; }
        .section-label { font-size: 10px; font-weight: 700; color: #6D5CFF; text-transform: uppercase; letter-spacing: 2px; margin-top: 32px; margin-bottom: 14px; padding-top: 18px; border-top: 1px solid #27272A; }
        .section-label:first-of-type { border-top: none; margin-top: 0; padding-top: 0; }
        label { display: block; font-size: 13px; color: #A1A1AA; margin-bottom: 5px; margin-top: 16px; }
        input, select { width: 100%; padding: 12px 14px; background: #27272A; border: 1px solid #3F3F46; border-radius: 8px; color: #FAFAFA; font-size: 15px; font-family: inherit; }
        input:focus, select:focus { outline: none; border-color: #6D5CFF; }
        select option { background: #27272A; }
        .row { display: flex; gap: 12px; }
        .row > div { flex: 1; }
        /* Pill toggle groups */
        .pill-group { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
        .pill-group label { display: flex; align-items: center; justify-content: center; gap: 6px; background: #27272A; border: 1px solid #3F3F46; border-radius: 20px; padding: 9px 16px; cursor: pointer; font-size: 13px; color: #A1A1AA; margin: 0; transition: all .15s; white-space: nowrap; }
        .pill-group label:has(input:checked) { border-color: #6D5CFF; background: rgba(109,92,255,.15); color: #FAFAFA; }
        .pill-group input { display: none; }
        /* Consent block */
        .consent-box { background: #27272A; border: 1px solid #3F3F46; border-radius: 8px; padding: 14px 16px; margin-top: 8px; }
        .consent-box label { display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin: 0; color: #FAFAFA; font-size: 13px; line-height: 1.55; }
        .consent-box input[type="checkbox"] { width: 16px; height: 16px; margin-top: 1px; flex-shrink: 0; accent-color: #6D5CFF; cursor: pointer; }
        #consent-error { display: none; color: #EF4444; font-size: 12px; margin-top: 6px; }
        button[type="submit"] { width: 100%; padding: 14px; background: #6D5CFF; color: #fff; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; margin-top: 32px; transition: background .15s; }
        button[type="submit"]:hover { background: #5B4DE6; }
        button[type="submit"]:disabled { opacity: .6; cursor: not-allowed; }
        #result { margin-top: 16px; padding: 12px 14px; border-radius: 8px; display: none; font-size: 13px; }
    </style>
</head>
<body>
<div class="container">
    <h1>cued</h1>
    <p class="sub">Your AI coach, right in your messages.</p>
    <p class="intro">Takes 60 seconds. Your coach will handle the rest over text.</p>

    <form id="signup" onsubmit="return handleSubmit(event)">

        <div class="section-label">You</div>

        <div class="row">
            <div>
                <label>First name</label>
                <input name="name" required placeholder="Alex">
            </div>
            <div>
                <label>Age</label>
                <input name="age" type="number" min="13" max="99" placeholder="21">
            </div>
        </div>

        <label>Phone number</label>
        <input name="phone" required placeholder="(555) 867-5309" inputmode="tel">

        <label>Gender</label>
        <select name="gender">
            <option value="male">Male</option>
            <option value="female">Female</option>
            <option value="non_binary">Non-binary</option>
            <option value="prefer_not_to_say" selected>Prefer not to say</option>
        </select>

        <div class="section-label">Your Goal</div>

        <label>What are you working toward?</label>
        <div class="pill-group" id="goal-group">
            <label><input type="checkbox" name="goals" value="fat_loss"> Lose fat</label>
            <label><input type="checkbox" name="goals" value="muscle_building"> Build muscle</label>
            <label><input type="checkbox" name="goals" value="strength"> Get stronger</label>
            <label><input type="checkbox" name="goals" value="general_fitness"> General fitness</label>
        </div>

        <label style="margin-top:20px;">Biggest obstacle</label>
        <select name="biggest_obstacle">
            <option value="" selected disabled>Pick one</option>
            <option value="consistency">Staying consistent</option>
            <option value="nutrition">Nutrition / what to eat</option>
            <option value="knowledge">Not knowing what to do</option>
            <option value="time">Not enough time</option>
            <option value="motivation">Motivation / accountability</option>
            <option value="injuries">Injuries holding me back</option>
        </select>

        <div class="section-label">Training</div>

        <label>Experience level</label>
        <div class="pill-group" id="exp-group">
            <label><input type="radio" name="experience" value="none"> Just starting out</label>
            <label><input type="radio" name="experience" value="beginner"> Under 6 months</label>
            <label><input type="radio" name="experience" value="intermediate" checked> 6 months – 2 years</label>
            <label><input type="radio" name="experience" value="advanced"> 2+ years</label>
        </div>

        <label style="margin-top:20px;">Equipment access</label>
        <div class="pill-group" id="equip-group">
            <label><input type="radio" name="equipment" value="full_gym" checked> Full gym</label>
            <label><input type="radio" name="equipment" value="limited_gym"> Limited gym</label>
            <label><input type="radio" name="equipment" value="home_gym"> Home gym</label>
            <label><input type="radio" name="equipment" value="bodyweight"> Bodyweight only</label>
        </div>

        <div class="section-label">Consent</div>

        <div class="consent-box">
            <label>
                <input type="checkbox" name="sms_consent" id="sms_consent">
                <span>I agree to receive automated SMS coaching messages from Cued. Message &amp; data rates may apply. Reply STOP at any time to unsubscribe.</span>
            </label>
        </div>
        <div id="consent-error">You must agree to receive SMS messages to use Cued.</div>

        <button type="submit" id="submit-btn">Start coaching →</button>
    </form>
    <div id="result"></div>
</div>

<script>
async function handleSubmit(e) {
    e.preventDefault();

    // Validate SMS consent
    if (!document.getElementById('sms_consent').checked) {
        const err = document.getElementById('consent-error');
        const box = document.querySelector('.consent-box');
        err.style.display = 'block';
        box.style.borderColor = '#EF4444';
        box.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return false;
    }
    document.getElementById('consent-error').style.display = 'none';
    document.querySelector('.consent-box').style.borderColor = '#3F3F46';

    // Validate at least one goal selected
    const goals = Array.from(document.querySelectorAll('input[name="goals"]:checked')).map(i => i.value);
    if (!goals.length) {
        document.getElementById('goal-group').scrollIntoView({ behavior: 'smooth', block: 'center' });
        alert('Please select at least one goal.');
        return false;
    }

    const btn = document.getElementById('submit-btn');
    btn.disabled = true;
    btn.textContent = 'Sending…';

    const form = e.target;
    const fd = new FormData(form);

    // Build the payload with exactly the 8 fields the backend expects
    const payload = {
        name: (fd.get('name') || '').trim(),
        phone: (fd.get('phone') || '').trim(),
        age: fd.get('age') || '',
        gender: fd.get('gender') || 'prefer_not_to_say',
        experience: fd.get('experience') || 'none',
        goal: goals.join(','),
        biggest_obstacle: fd.get('biggest_obstacle') || '',
        equipment: fd.get('equipment') || 'full_gym',
        sms_consent: true,
    };

    try {
        const res = await fetch('/signup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();

        if (data.status === 'ok') {
            document.querySelector('.container').innerHTML = `
                <div style="text-align:center;padding:80px 0 60px;">
                    <div style="font-size:52px;margin-bottom:24px;">✓</div>
                    <h1 style="font-size:26px;margin-bottom:12px;letter-spacing:-.4px;">You're in, ${data.name || 'friend'}.</h1>
                    <p style="color:#A1A1AA;font-size:15px;line-height:1.7;max-width:340px;margin:0 auto 20px;">
                        Your coach is putting together your plan. You'll get a text shortly with a few quick questions to get things dialed in.
                    </p>
                    <p style="color:#6E6E73;font-size:13px;">Keep your phone nearby.</p>
                </div>`;
            window.scrollTo(0, 0);
        } else {
            const el = document.getElementById('result');
            el.style.display = 'block';
            el.style.background = data.status === 'exists' ? 'rgba(109,92,255,.15)' : 'rgba(220,38,38,.15)';
            el.style.color = data.status === 'exists' ? '#A78BFA' : '#FCA5A5';
            el.textContent = data.message;
            btn.disabled = false;
            btn.textContent = 'Start coaching →';
        }
    } catch (err) {
        const el = document.getElementById('result');
        el.style.display = 'block';
        el.style.background = 'rgba(220,38,38,.15)';
        el.style.color = '#FCA5A5';
        el.textContent = 'Something went wrong. Please try again.';
        btn.disabled = false;
        btn.textContent = 'Start coaching →';
    }
}
</script>
</body>
</html>
"""

_UNUSED_OLD_ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Baseline Admin</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, system-ui, sans-serif; background: #0F0F10; color: #FAFAFA; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; }
        h1 { margin-bottom: 24px; }
        .user-card { background: #18181B; border-radius: 12px; padding: 20px; margin-bottom: 20px; }
        .user-name { font-size: 18px; font-weight: 600; }
        .user-meta { color: #A1A1AA; font-size: 13px; margin-top: 4px; }
        .messages { margin-top: 16px; max-height: 400px; overflow-y: auto; }
        .msg { padding: 8px 12px; margin: 4px 0; border-radius: 8px; font-size: 14px; max-width: 85%; }
        .msg.out { background: #6D5CFF; margin-left: auto; text-align: right; color: white; }
        .msg.in { background: #27272A; }
        .msg .time { font-size: 11px; color: #71717A; margin-top: 2px; }
        .msg.out .time { color: #C4B5FF; }
        .send-form { display: flex; gap: 8px; margin-top: 12px; }
        .send-form input { flex: 1; padding: 10px; background: #27272A; border: 1px solid #3F3F46; border-radius: 8px; color: #FAFAFA; font-size: 14px; }
        .send-form button { padding: 10px 20px; background: #6D5CFF; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Baseline Admin</h1>
        {% for ud in users %}
        <div class="user-card">
            <div class="user-name">{{ ud.user.name }}</div>
            <div class="user-meta">{{ ud.user.phone }} · {{ ud.user.goal }} · {{ ud.user.experience }} · wake {{ ud.user.wake_time }}</div>
            <div class="messages">
                {% for m in ud.messages %}
                <div class="msg {{ m.direction }}">
                    {{ m.body }}
                    <div class="time">{{ m.created_at.strftime('%b %d %I:%M %p') }} · {{ m.message_type }}</div>
                </div>
                {% endfor %}
            </div>
            <form class="send-form" onsubmit="return adminSend(event, {{ ud.user.id }})">
                <input name="body" placeholder="Manual override message...">
                <button type="submit">Send</button>
            </form>
        </div>
        {% endfor %}
        {% if not users %}
        <p style="color: #A1A1AA;">No active users yet. Share your signup link!</p>
        {% endif %}
    </div>
    <script>
    async function adminSend(e, userId) {
        e.preventDefault();
        const body = e.target.body.value;
        await fetch('/admin/send', {
            method: 'POST',
            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
            body: 'user_id=' + userId + '&body=' + encodeURIComponent(body)
        });
        e.target.body.value = '';
        location.reload();
        return false;
    }
    </script>
</body>
</html>
"""


# ─── App Startup ────────────────────────────────────
if __name__ == "__main__":
    logger.info("Starting Cued...")
    start_scheduler()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
