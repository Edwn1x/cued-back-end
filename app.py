import os
import logging
from flask import Flask, request, jsonify, render_template_string
from models import init_db, get_session, User, Message, Workout
from sms import send_sms, log_incoming, get_twiml_response
from coach import get_coach_response, parse_workout_log
from scheduler import start_scheduler, schedule_user
import config
from onboarding_agent import start_onboarding

# ─── Setup ──────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY

# Initialize DB on startup
init_db()


# ─── Twilio Webhook (incoming SMS) ──────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming SMS from Twilio."""
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()
    
    # Check for MMS image
    num_media = int(request.form.get("NumMedia", 0))
    image_url = None
    if num_media > 0:
        image_url = request.form.get("MediaUrl0")
        logger.info(f"MMS image received from {from_number}: {image_url}")

    logger.info(f"Incoming SMS from {from_number}: {body}")

    session = get_session()
    try:
        user = session.query(User).filter(User.phone == from_number).first()

        if not user:
            # Unknown number — send a friendly redirect
            logger.warning(f"Unknown number: {from_number}")
            return get_twiml_response(
                "Hey! You're not signed up for Cued yet. "
                "Visit cued.fit/signup to get started."
            ), 200, {"Content-Type": "text/xml"}

        # Log the incoming message
        log_incoming(user.id, body)

        # Detect message type from context
        message_type = classify_message(body, has_image=image_url is not None)

        # If it looks like a workout log, try to parse it
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

        # Get AI coaching response (with image if present)
        response_text = get_coach_response(user, body, message_type, image_url=image_url) 

        # Send the response
        send_sms(user.phone, response_text, user_id=user.id, message_type=message_type)

        # Return empty TwiML (we already sent the response via API)
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
    """Handle new user signup."""
    session = get_session()
    try:
        phone = request.form.get("phone", "").strip()
        if not phone.startswith("+"):
            phone = "+1" + phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")

        # Check if already exists
        existing = session.query(User).filter(User.phone == phone).first()
        if existing:
            return jsonify({"status": "exists", "message": f"{existing.name} is already signed up!"})

        user = User(
            phone=phone,
            name=request.form.get("name", "").strip(),
            age=safe_int(request.form.get("age")),
            gender=request.form.get("gender", "prefer_not_to_say"),
            occupation=request.form.get("occupation", "").strip() or None,
            goal=request.form.get("goal", "general_fitness"),
            goal_other=request.form.get("goal_other", "").strip() or None,
            biggest_obstacle=request.form.get("biggest_obstacle", "") or None,
            experience=request.form.get("experience", "beginner"),
            prior_coaching=request.form.get("prior_coaching", "no"),
            equipment=request.form.get("equipment", "full_gym"),
            injuries=request.form.get("injuries", "").strip() or None,
            activity_level=request.form.get("activity_level", "lightly_active"),
            diet=request.form.get("diet", "omnivore"),
            restrictions=request.form.get("restrictions", "").strip() or None,
            cooking_situation=request.form.get("cooking_situation", "mix"),
            meals_per_day=request.form.get("meals_per_day", "3"),
            wake_time=request.form.get("wake_time", "07:00"),
            sleep_time=request.form.get("sleep_time", "23:00"),
            sleep_quality=request.form.get("sleep_quality", "okay"),
            stress_level=request.form.get("stress_level", "moderate"),
            workout_time=request.form.get("workout_time", "16:00"),
            workout_days=request.form.get("workout_days", ""),
            height_ft=safe_int(request.form.get("height_ft")),
            height_in=safe_int(request.form.get("height_in")),
            weight_lbs=safe_float(request.form.get("weight_lbs")),
            body_fat_pct=safe_float(request.form.get("body_fat_pct")),
            wearable=request.form.get("wearable", "none"),
            motivation=request.form.get("motivation", "").strip() or None,
            schedule_details=request.form.get("schedule_details", "").strip() or None,
        )
        session.add(user)
        session.commit()

        # Schedule their daily messages
        schedule_user(user)

        # Start onboarding agent (sends personalized welcome sequence in background)
        start_onboarding(user)

        logger.info(f"New user signed up: {user.name} ({user.phone})")
        return jsonify({"status": "ok", "message": f"Welcome {user.name}! Check your phone."})

    except Exception as e:
        logger.error(f"Signup error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()


# ─── Admin Dashboard ────────────────────────────────
@app.route("/admin")
def admin():
    """Metrics dashboard for tracking beta performance."""
    from datetime import datetime, timedelta, timezone
    session = get_session()
    try:
        now = datetime.utcnow()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
 
        # ── USER STATS ──
        all_users = session.query(User).all()
        total_users = len(all_users)
        active_users = sum(1 for u in all_users if u.active)
 
        # ── MESSAGE STATS ──
        all_messages = session.query(Message).all()
        total_sent = sum(1 for m in all_messages if m.direction == "out")
        total_received = sum(1 for m in all_messages if m.direction == "incoming")
        response_rate = round((total_received / total_sent * 100) if total_sent > 0 else 0)
 
        # ── TODAY'S ACTIVITY ──
        today_active = 0
        for u in all_users:
            user_msgs_today = [m for m in all_messages
                              if m.user_id == u.id
                              and m.direction == "incoming"
                              and m.created_at >= today_start]
            if user_msgs_today:
                today_active += 1
 
        # ── RETENTION COHORTS ──
        # Day 1: responded to at least one message
        d1_responded = 0
        for u in all_users:
            user_incoming = [m for m in all_messages if m.user_id == u.id and m.direction == "incoming"]
            if user_incoming:
                d1_responded += 1
        d1_rate = round((d1_responded / total_users * 100) if total_users > 0 else 0)
 
        # Day 7, 14, 30: active within the window
        def active_in_window(days):
            cutoff = now - timedelta(days=days)
            count = 0
            eligible = 0
            for u in all_users:
                # Only count users who signed up at least N days ago
                if hasattr(u, 'created_at') and u.created_at and u.created_at <= cutoff:
                    eligible += 1
                    user_msgs = [m for m in all_messages
                                if m.user_id == u.id
                                and m.direction == "incoming"
                                and m.created_at >= cutoff]
                    if user_msgs:
                        count += 1
                elif not hasattr(u, 'created_at') or not u.created_at:
                    # If no created_at, count all users
                    eligible += 1
                    user_msgs = [m for m in all_messages
                                if m.user_id == u.id
                                and m.direction == "incoming"
                                and m.created_at >= cutoff]
                    if user_msgs:
                        count += 1
            return count, eligible
 
        d7_active, d7_eligible = active_in_window(7)
        d14_active, d14_eligible = active_in_window(14)
        d30_active, d30_eligible = active_in_window(30)
        d7_rate = round((d7_active / d7_eligible * 100) if d7_eligible > 0 else 0)
        d14_rate = round((d14_active / d14_eligible * 100) if d14_eligible > 0 else 0)
        d30_rate = round((d30_active / d30_eligible * 100) if d30_eligible > 0 else 0)
 
        # ── RATINGS ──
        # Look for messages that are just a number 1-5 (day ratings)
        rating_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        total_rating_sum = 0
        total_ratings = 0
        for m in all_messages:
            if m.direction == "incoming" and m.body and m.body.strip() in ["1", "2", "3", "4", "5"]:
                r = int(m.body.strip())
                rating_counts[r] += 1
                total_rating_sum += r
                total_ratings += 1
        avg_rating = round(total_rating_sum / total_ratings, 1) if total_ratings > 0 else 0
        max_rating_count = max(rating_counts.values()) if rating_counts else 1
 
        # ── USER TABLE DATA ──
        users_data = []
        for u in all_users:
            user_msgs = [m for m in all_messages if m.user_id == u.id]
            user_incoming = [m for m in user_msgs if m.direction == "incoming"]
            msg_count = len(user_msgs)
 
            # Last active
            if user_incoming:
                last_msg = max(user_incoming, key=lambda m: m.created_at)
                last_active = last_msg.created_at.strftime("%b %d, %I:%M %p") if last_msg.created_at else "—"
                days_inactive = (now - last_msg.created_at).days if last_msg.created_at else 99
            else:
                last_active = "Never"
                days_inactive = 99
 
            # Workout count
            workout_count = session.query(Workout).filter(Workout.user_id == u.id).count()
 
            # User avg rating
            user_ratings = [int(m.body.strip()) for m in user_incoming
                          if m.body and m.body.strip() in ["1", "2", "3", "4", "5"]]
            user_avg = round(sum(user_ratings) / len(user_ratings), 1) if user_ratings else "—"
 
            signed_up = u.created_at.strftime("%b %d") if hasattr(u, 'created_at') and u.created_at else "—"
 
            users_data.append({
                "id": u.id,
                "name": u.name,
                "phone": u.phone[-4:] if u.phone else "—",  # Show last 4 digits only
                "signed_up": signed_up,
                "last_active": last_active,
                "msg_count": msg_count,
                "workout_count": workout_count,
                "avg_rating": user_avg,
                "days_inactive": days_inactive,
            })
 
        # ── RECENT MESSAGES ──
        recent = sorted(all_messages, key=lambda m: m.created_at if m.created_at else now, reverse=True)[:50]
        recent_messages_data = []
        user_map = {u.id: u.name for u in all_users}
        for m in recent:
            recent_messages_data.append({
                "time": m.created_at.strftime("%I:%M %p") if m.created_at else "—",
                "user_name": user_map.get(m.user_id, "Unknown"),
                "direction": m.direction,
                "body": m.body or "",
                "message_type": m.message_type or "—",
            })
 
        # ── COST ESTIMATES ──
        total_msg_count = total_sent + total_received
        twilio_cost = round(total_msg_count * 0.015, 2)
        api_cost = round(total_sent * 0.006, 2)
        total_cost = round(twilio_cost + api_cost, 2)
 
        return render_template_string(ADMIN_HTML,
            now=now.strftime("%b %d, %Y %I:%M %p UTC"),
            total_users=total_users,
            active_users=active_users,
            total_sent=total_sent,
            total_received=total_received,
            response_rate=response_rate,
            avg_rating=avg_rating,
            total_ratings=total_ratings,
            today_active=today_active,
            d1_responded=d1_responded,
            d1_rate=d1_rate,
            d7_active=d7_active,
            d7_rate=d7_rate,
            d14_active=d14_active,
            d14_rate=d14_rate,
            d30_active=d30_active,
            d30_rate=d30_rate,
            users=users_data,
            rating_counts=rating_counts,
            max_rating_count=max_rating_count,
            recent_messages=recent_messages_data,
            twilio_cost=twilio_cost,
            api_cost=api_cost,
            total_cost=total_cost,
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
        user = session.query(User).get(user_id)
        if user and body:
            send_sms(user.phone, body, user_id=user.id, message_type="admin")
            return jsonify({"status": "ok"})
        return jsonify({"status": "error"}), 400
    finally:
        session.close()


# ─── Health Check ───────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "app": "cued", "version": "0.1.0"})


# ─── HTML Templates ─────────────────────────────────
SIGNUP_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Baseline — Sign Up</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, system-ui, sans-serif; background: #18181B; color: #FAFAFA; padding: 20px; }
        .container { max-width: 520px; margin: 40px auto; padding-bottom: 60px; }
        h1 { font-size: 28px; margin-bottom: 4px; }
        .sub { color: #A1A1AA; margin-bottom: 12px; font-size: 15px; }
        .intro { color: #71717A; font-size: 13px; margin-bottom: 32px; line-height: 1.5; }
        .section-label { font-size: 11px; font-weight: 600; color: #6D5CFF; text-transform: uppercase; letter-spacing: 2px; margin-top: 28px; margin-bottom: 12px; padding-top: 16px; border-top: 1px solid #27272A; }
        .section-label:first-of-type { border-top: none; margin-top: 0; padding-top: 0; }
        label { display: block; font-size: 13px; color: #A1A1AA; margin-bottom: 4px; margin-top: 14px; }
        .label-hint { font-size: 11px; color: #52525B; margin-bottom: 4px; }
        input, select, textarea { width: 100%; padding: 12px; background: #27272A; border: 1px solid #3F3F46; border-radius: 8px; color: #FAFAFA; font-size: 15px; font-family: inherit; }
        input:focus, select:focus, textarea:focus { outline: none; border-color: #6D5CFF; }
        textarea { resize: vertical; min-height: 70px; }
        button { width: 100%; padding: 14px; background: #6D5CFF; color: white; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; margin-top: 28px; }
        button:hover { background: #5B4DE6; }
        .row { display: flex; gap: 12px; }
        .row > div { flex: 1; }
        .row3 > div { flex: 1; }
        .checkbox-group { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
        .checkbox-group label { display: flex; align-items: center; gap: 6px; background: #27272A; border: 1px solid #3F3F46; border-radius: 8px; padding: 10px 14px; cursor: pointer; font-size: 14px; color: #FAFAFA; margin: 0; flex: 1; min-width: 140px; transition: border-color 0.15s; }
        .checkbox-group label:has(input:checked) { border-color: #6D5CFF; background: #6D5CFF15; }
        .checkbox-group input[type="checkbox"] { width: auto; accent-color: #6D5CFF; }
        .day-group { display: flex; gap: 6px; margin-top: 6px; }
        .day-group label { display: flex; align-items: center; justify-content: center; width: 44px; height: 44px; background: #27272A; border: 1px solid #3F3F46; border-radius: 8px; cursor: pointer; font-size: 13px; color: #A1A1AA; margin: 0; transition: all 0.15s; }
        .day-group label:has(input:checked) { border-color: #6D5CFF; background: #6D5CFF22; color: #FAFAFA; }
        .day-group input { display: none; }
        #other-goal-wrap { display: none; margin-top: 8px; }
        #result { margin-top: 16px; padding: 12px; border-radius: 8px; display: none; }
    </style>
</head>
<body>
    <div class="container">
        <h1>baseline</h1>
        <p class="sub">Your AI coach, right in your messages.</p>
        <p class="intro">Fill this out once and your coach takes it from there. The more you share, the better we can tailor your workouts, meals, and daily plan. Takes about 3 minutes.</p>

        <form id="signup" onsubmit="return handleSubmit(event)">

            <div class="section-label">About You</div>

            <div class="row">
                <div><label>First name</label><input name="name" required placeholder="Edwin"></div>
                <div><label>Age</label><input name="age" type="number" placeholder="20"></div>
            </div>

            <label>Phone number</label>
            <input name="phone" required placeholder="(209) 555-1234">

            <label>Gender</label>
            <select name="gender">
                <option value="male">Male</option>
                <option value="female">Female</option>
                <option value="non_binary">Non-binary</option>
                <option value="prefer_not_to_say" selected>Prefer not to say</option>
            </select>

            <label>What do you do?</label>
            <div class="label-hint">Job, school, etc. — helps us understand your daily energy and stress</div>
            <input name="occupation" placeholder="e.g. CS student at UC Berkeley, part-time barista">

            <div class="section-label">Body Stats</div>

            <label>Height</label>
            <div class="row">
                <div><input name="height_ft" type="number" min="3" max="8" placeholder="5" style="text-align:center;"> <p style="font-size:11px;color:#71717A;text-align:center;margin-top:2px;">feet</p></div>
                <div><input name="height_in" type="number" min="0" max="11" placeholder="10" style="text-align:center;"> <p style="font-size:11px;color:#71717A;text-align:center;margin-top:2px;">inches</p></div>
            </div>

            <div class="row">
                <div>
                    <label>Weight (lbs)</label>
                    <input name="weight_lbs" type="number" placeholder="170">
                </div>
                <div>
                    <label>Body fat %</label>
                    <div class="label-hint">Optional — skip if unsure</div>
                    <input name="body_fat_pct" type="number" step="0.1" placeholder="e.g. 18">
                </div>
            </div>

            <div class="section-label">Your Goals</div>

            <label>What are you working toward? (select all that apply)</label>
            <div class="checkbox-group">
                <label><input type="checkbox" name="goals" value="fat_loss"> Lose fat</label>
                <label><input type="checkbox" name="goals" value="muscle_building"> Build muscle</label>
                <label><input type="checkbox" name="goals" value="general_fitness"> General fitness</label>
                <label><input type="checkbox" name="goals" value="endurance"> Endurance / cardio</label>
                <label><input type="checkbox" name="goals" value="strength"> Get stronger</label>
                <label><input type="checkbox" name="goals" value="flexibility"> Flexibility / mobility</label>
                <label><input type="checkbox" name="goals" value="other" id="other-check" onchange="toggleOther()"> Other</label>
            </div>
            <div id="other-goal-wrap">
                <input name="goal_other" placeholder="Describe your goal...">
            </div>

            <label>Why do you want coaching?</label>
            <div class="label-hint">This helps your coach understand what motivates you</div>
            <textarea name="motivation" placeholder="e.g. I've been working out on and off for a year but can't stay consistent. I want someone to keep me accountable and tell me exactly what to do." rows="3"></textarea>

            <label>What's been your biggest obstacle?</label>
            <select name="biggest_obstacle">
                <option value="" selected>Select one</option>
                <option value="consistency">Staying consistent</option>
                <option value="nutrition">Nutrition / diet</option>
                <option value="knowledge">Not knowing what to do</option>
                <option value="time">Not enough time</option>
                <option value="motivation">Motivation / accountability</option>
                <option value="injuries">Injuries holding me back</option>
                <option value="plateaus">Hit a plateau</option>
            </select>

            <div class="section-label">Training</div>

            <label>Experience level</label>
            <select name="experience">
                <option value="none">Complete beginner — never worked out</option>
                <option value="beginner">Beginner — less than 6 months</option>
                <option value="intermediate" selected>Intermediate — 6 months to 2 years</option>
                <option value="advanced">Advanced — 2+ years consistent</option>
            </select>

            <label>Equipment access</label>
            <select name="equipment">
                <option value="full_gym" selected>Full gym (barbells, machines, dumbbells)</option>
                <option value="limited_gym">Limited gym (dumbbells + some machines)</option>
                <option value="home_gym">Home gym (dumbbells + bench)</option>
                <option value="minimal">Minimal (resistance bands, pull-up bar)</option>
                <option value="bodyweight">Bodyweight only</option>
            </select>

            <label>Any injuries or physical limitations?</label>
            <input name="injuries" placeholder="e.g. bad left knee, lower back issues, shoulder impingement">

            <div class="row">
                <div>
                    <label>Worked with a coach before?</label>
                    <select name="prior_coaching">
                        <option value="no" selected>No</option>
                        <option value="yes">Yes</option>
                    </select>
                </div>
                <div>
                    <label>Activity outside the gym?</label>
                    <select name="activity_level">
                        <option value="sedentary">Sedentary (desk all day)</option>
                        <option value="lightly_active" selected>Light (some walking)</option>
                        <option value="active">Active (on my feet a lot)</option>
                        <option value="very_active">Very active (physical job)</option>
                    </select>
                </div>
            </div>

            <label>Which days can you work out?</label>
            <div class="day-group">
                <label><input type="checkbox" name="workout_days" value="mon">Mon</label>
                <label><input type="checkbox" name="workout_days" value="tue">Tue</label>
                <label><input type="checkbox" name="workout_days" value="wed">Wed</label>
                <label><input type="checkbox" name="workout_days" value="thu">Thu</label>
                <label><input type="checkbox" name="workout_days" value="fri">Fri</label>
                <label><input type="checkbox" name="workout_days" value="sat">Sat</label>
                <label><input type="checkbox" name="workout_days" value="sun">Sun</label>
            </div>

            <label>Usual workout time</label>
            <input name="workout_time" type="time" value="16:00">

            <div class="section-label">Nutrition</div>

            <label>Diet</label>
            <select name="diet">
                <option value="omnivore" selected>Omnivore — I eat everything</option>
                <option value="vegetarian">Vegetarian</option>
                <option value="vegan">Vegan</option>
                <option value="pescatarian">Pescatarian</option>
                <option value="keto">Keto / low-carb</option>
                <option value="halal">Halal</option>
                <option value="kosher">Kosher</option>
            </select>

            <label>Food allergies or restrictions</label>
            <input name="restrictions" placeholder="e.g., lactose intolerant, no shellfish, gluten-free">

            <div class="row">
                <div>
                    <label>How do you eat most days?</label>
                    <select name="cooking_situation">
                        <option value="cook_myself">I cook for myself</option>
                        <option value="dining_hall" selected>Dining hall / meal plan</option>
                        <option value="mostly_eat_out">Mostly eat out / takeout</option>
                        <option value="mix">Mix of everything</option>
                        <option value="cook_family">I cook for family</option>
                    </select>
                </div>
                <div>
                    <label>Meals per day?</label>
                    <select name="meals_per_day">
                        <option value="1-2">1-2 meals</option>
                        <option value="3" selected>3 meals</option>
                        <option value="4+">4+ meals / snacks</option>
                    </select>
                </div>
            </div>

            <div class="section-label">Your Schedule</div>

            <div class="row">
                <div><label>Wake time</label><input name="wake_time" type="time" value="07:00"></div>
                <div><label>Bedtime</label><input name="sleep_time" type="time" value="23:00"></div>
            </div>

            <div class="row">
                <div>
                    <label>Sleep quality?</label>
                    <select name="sleep_quality">
                        <option value="great">Great</option>
                        <option value="okay" selected>Okay</option>
                        <option value="poor">Poor</option>
                        <option value="terrible">Terrible</option>
                    </select>
                </div>
                <div>
                    <label>Current stress level?</label>
                    <select name="stress_level">
                        <option value="low">Low</option>
                        <option value="moderate" selected>Moderate</option>
                        <option value="high">High</option>
                        <option value="very_high">Very high</option>
                    </select>
                </div>
            </div>

            <label>Tell us about your weekly schedule</label>
            <div class="label-hint">Classes, work, commitments — so your coach can plan around them</div>
            <textarea name="schedule_details" placeholder="e.g. MWF classes 9am-2pm, T/Th work 10am-4pm, weekends are usually free. I prefer working out in the afternoon." rows="3"></textarea>

            <div class="section-label">Device</div>

            <label>Wearable</label>
            <select name="wearable">
                <option value="none" selected>None</option>
                <option value="apple_watch">Apple Watch</option>
                <option value="oura">Oura Ring</option>
                <option value="garmin">Garmin</option>
                <option value="whoop">Whoop</option>
                <option value="fitbit">Fitbit</option>
                <option value="samsung">Samsung Galaxy Watch</option>
                <option value="other_wearable">Other</option>
            </select>

            <button type="submit">Start coaching me</button>
        </form>
        <div id="result"></div>
    </div>
    <script>
    function toggleOther() {
        document.getElementById('other-goal-wrap').style.display =
            document.getElementById('other-check').checked ? 'block' : 'none';
    }
    async function handleSubmit(e) {
        e.preventDefault();
        const form = e.target;
        const fd = new FormData(form);
        // Collect multi-select goals into comma-separated
        const goals = fd.getAll('goals').join(',');
        fd.delete('goals');
        fd.append('goal', goals);
        // Collect workout days
        const days = fd.getAll('workout_days').join(',');
        fd.delete('workout_days');
        fd.append('workout_days', days);
        const res = await fetch('/signup', { method: 'POST', body: fd });
        const data = await res.json();
        const el = document.getElementById('result');
        el.style.display = 'block';
        el.style.background = data.status === 'ok' ? '#16A34A22' : '#DC262622';
        el.style.color = data.status === 'ok' ? '#16A34A' : '#DC2626';
        el.textContent = data.message;
        if (data.status === 'ok') { form.reset(); window.scrollTo(0, 0); }
    }
    </script>
</body>
</html>
"""

ADMIN_HTML = """
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
