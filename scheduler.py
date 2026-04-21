from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from models import get_session, User
from coach import generate_scheduled_message
from sms import send_sms
from engagement_tracker import should_send, is_question_type, increment_unanswered, get_tier
from models import is_workout_confirmed_today
import logging

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def parse_time(time_str: str) -> tuple[int, int]:
    """Extract hour and minute from a time string, even if freeform."""
    import re
    if not time_str:
        return 18, 0
    # First try HH:MM pattern
    match = re.search(r'\b(\d{1,2}):(\d{2})\b', time_str)
    if match:
        return int(match.group(1)), int(match.group(2))
    # Fall back to bare hour
    match = re.search(r'\b(\d{1,2})\b', time_str)
    if match:
        return int(match.group(1)), 0
    return 18, 0


def add_minutes(time_str: str, minutes: int) -> str:
    """Add minutes to a 'HH:MM' time string."""
    h, m = parse_time(time_str)
    dt = datetime(2000, 1, 1, h, m) + timedelta(minutes=minutes)
    return dt.strftime("%H:%M")


def user_local_to_utc(hour: int, minute: int, tz_str: str) -> tuple[int, int]:
    """
    Convert a local time (hour, minute) in the user's timezone to UTC hour and minute.
    Used so CronTrigger (which runs in UTC) fires at the right local time.
    """
    try:
        user_tz = ZoneInfo(tz_str or "America/Los_Angeles")
    except Exception:
        user_tz = ZoneInfo("America/Los_Angeles")

    # Use today's date for DST accuracy
    today = datetime.now(user_tz).date()
    local_dt = datetime(today.year, today.month, today.day, hour, minute, tzinfo=user_tz)
    utc_dt = local_dt.astimezone(ZoneInfo("UTC"))
    return utc_dt.hour, utc_dt.minute


def has_unanswered_outbound(user_id: int) -> bool:
    """
    Return True if the last outbound message sent TODAY has not received a reply.
    Prevents back-to-back unsolicited messages when the user hasn't responded.
    Only looks at today's window — a stale unanswered message from yesterday
    shouldn't block today's scheduled touchpoints.
    """
    from models import Message
    from datetime import timezone
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        last_out_today = (
            session.query(Message)
            .filter(
                Message.user_id == user_id,
                Message.direction == "out",
                Message.created_at >= today_start,
            )
            .order_by(Message.created_at.desc())
            .first()
        )
        if not last_out_today:
            return False  # nothing sent today, no block

        last_in_today = (
            session.query(Message)
            .filter(
                Message.user_id == user_id,
                Message.direction == "in",
                Message.created_at >= today_start,
            )
            .order_by(Message.created_at.desc())
            .first()
        )
        if not last_in_today:
            return True  # sent today, no reply today
        return last_out_today.created_at > last_in_today.created_at
    finally:
        session.close()


def _is_training_day(user) -> bool:
    """
    Return True if today is a known training day for this user.

    Uses confirmed_training_days (e.g. "mon,wed,fri") when available.
    Falls back to True when only a count is stored ("4", "3-5") — in that
    case the morning briefing asks "training day or rest day?" and the
    pre/post nudges rely on is_workout_confirmed_today() as the gate.
    """
    days_str = user.confirmed_training_days or user.workout_days or ""
    days_str = days_str.strip().lower()
    if not days_str:
        return True  # no info — don't suppress, let morning briefing handle it

    # If it looks like specific days (contains letters), parse them
    import re
    if re.search(r'[a-z]', days_str):
        day_map = {
            "mon": 0, "monday": 0,
            "tue": 1, "tuesday": 1,
            "wed": 2, "wednesday": 2,
            "thu": 3, "thursday": 3,
            "fri": 4, "friday": 4,
            "sat": 5, "saturday": 5,
            "sun": 6, "sunday": 6,
        }
        today_num = datetime.now().weekday()
        tokens = re.split(r'[\s,/]+', days_str)
        for token in tokens:
            if day_map.get(token) == today_num:
                return True
        return False

    # Only a number or range — can't determine specific days, allow through
    return True


def send_scheduled_message(user_id: int, message_type: str):
    """Generate and send a scheduled coaching message to a user."""
    session = get_session()
    try:
        user = session.query(User).get(user_id)
        if not user or not user.active:
            return

        # Respect quiet_until — don't send scheduled messages if user said goodnight
        if user.quiet_until and datetime.now() < user.quiet_until:
            logger.info(f"Skipping {message_type} for {user.name} — quiet until {user.quiet_until}")
            return

        # Check engagement tier — skip if this touchpoint isn't allowed
        if not should_send(user, message_type):
            tier = get_tier(user.unanswered_count or 0)
            logger.info(f"Skipping {message_type} for {user.name} (tier={tier}, unanswered={user.unanswered_count})")
            return

        # pre/post_workout nudges only fire on known training days
        if message_type in ("pre_workout", "post_workout"):
            if not _is_training_day(user):
                logger.info(f"Skipping {message_type} for {user.name} — not a training day")
                return

        # post_workout check-in only fires if user confirmed they trained today
        if message_type == "post_workout" and not is_workout_confirmed_today(user.id):
            logger.info(f"Skipping post_workout for {user.name} — no workout confirmed today")
            return

        # No back-to-back outbound without a reply (except morning briefing which always fires)
        if message_type not in ("morning_briefing", "morning") and has_unanswered_outbound(user_id):
            logger.info(f"Skipping {message_type} for {user.name} — last outbound still unanswered")
            return

        # Before sending a question-type message, check if the previous one was answered
        if is_question_type(message_type):
            increment_unanswered(user_id)
            # Re-fetch user after potential increment
            session.close()
            session = get_session()
            user = session.query(User).get(user_id)
            # Re-check tier after increment
            if not should_send(user, message_type):
                tier = get_tier(user.unanswered_count or 0)
                logger.info(f"Skipping {message_type} for {user.name} after increment (tier={tier})")
                return

        logger.info(f"Sending {message_type} to {user.name} (unanswered={user.unanswered_count})")

        body = generate_scheduled_message(user, message_type)
        send_sms(user.phone, body, user_id=user.id, message_type=message_type)

        logger.info(f"Sent {message_type} to {user.name}: {body[:80]}...")

    except Exception as e:
        logger.error(f"Failed to send {message_type} to user {user_id}: {e}")
    finally:
        session.close()


def schedule_user(user: User):
    """
    Set up daily scheduled messages for a user based on their wake_time,
    workout_time, and sleep_time. All times are converted from the user's
    local timezone to UTC for the cron triggers.

    Daily rhythm:
    - Morning briefing: at wake_time
    - Pre-workout nudge: 15 min before workout_time
    - Evening accountability: 90 min before sleep_time
    - Weekly weigh-in: at wake_time + 10 min on weigh_in_day
    """
    tz_str = user.user_timezone or "America/Los_Angeles"

    if not user.wake_time:
        logger.warning(f"Skipping schedule for {user.name} — no wake_time set")
        return

    wake_h, wake_m = parse_time(user.wake_time)
    wake_utc_h, wake_utc_m = user_local_to_utc(wake_h, wake_m, tz_str)

    touchpoints = []

    # Morning briefing — always schedule if we have wake_time
    touchpoints.append(("morning_briefing", wake_utc_h, wake_utc_m))

    # Pre-workout nudge — 15 min before workout_time
    if user.workout_time:
        pre_workout_str = add_minutes(user.workout_time, -15)
        pre_h, pre_m = parse_time(pre_workout_str)
        pre_utc_h, pre_utc_m = user_local_to_utc(pre_h, pre_m, tz_str)
        touchpoints.append(("pre_workout", pre_utc_h, pre_utc_m))

        # Post-workout check-in — 75 min after workout_time
        post_workout_str = add_minutes(user.workout_time, 75)
        post_h, post_m = parse_time(post_workout_str)
        post_utc_h, post_utc_m = user_local_to_utc(post_h, post_m, tz_str)
        touchpoints.append(("post_workout", post_utc_h, post_utc_m))

    # Evening accountability — 90 min before sleep_time
    if user.sleep_time:
        evening_str = add_minutes(user.sleep_time, -90)
        eve_h, eve_m = parse_time(evening_str)
        eve_utc_h, eve_utc_m = user_local_to_utc(eve_h, eve_m, tz_str)
        touchpoints.append(("evening_wrap", eve_utc_h, eve_utc_m))

    for msg_type, hour, minute in touchpoints:
        job_id = f"user_{user.id}_{msg_type}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        scheduler.add_job(
            send_scheduled_message,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[user.id, msg_type],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
        )

    logger.info(
        f"Scheduled {len(touchpoints)} touchpoints for {user.name} "
        f"(tz={tz_str}, wake={user.wake_time}, workout={user.workout_time}, sleep={user.sleep_time})"
    )

    # Weekly weigh-in
    if user.weigh_in_day and user.wake_time:
        day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        day_num = day_map.get(user.weigh_in_day.lower())
        if day_num is not None:
            weighin_m = wake_m + 10 if wake_m < 50 else wake_m - 10
            wi_utc_h, wi_utc_m = user_local_to_utc(wake_h, weighin_m, tz_str)
            job_id = f"user_{user.id}_weigh_in"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            scheduler.add_job(
                send_scheduled_message,
                trigger=CronTrigger(day_of_week=day_num, hour=wi_utc_h, minute=wi_utc_m),
                args=[user.id, "weigh_in"],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info(f"Scheduled weekly weigh-in for {user.name} on {user.weigh_in_day}")


def schedule_all_users():
    """Load all active users and schedule their messages."""
    session = get_session()
    try:
        users = session.query(User).filter(User.active == True).all()
        for user in users:
            try:
                schedule_user(user)
            except Exception as e:
                logger.error(f"Failed to schedule {user.name}: {e}")
        logger.info(f"Scheduled messages for {len(users)} active users.")
    finally:
        session.close()


def check_meal_adherence():
    """
    Daily check: if a user has been active but hasn't logged a meal in 24+ hours, nudge them.
    After 48+ hours, slightly firmer tone.
    """
    from datetime import timezone
    from models import Meal, Message

    session = get_session()
    try:
        users = session.query(User).filter(User.active == True).all()
        for user in users:
            # Skip if user said goodnight recently
            if user.quiet_until and datetime.now() < user.quiet_until:
                continue

            # Skip if user is still in onboarding
            if (user.onboarding_step or 0) < 2:
                continue

            last_meal = (
                session.query(Meal)
                .filter(Meal.user_id == user.id)
                .order_by(Meal.eaten_at.desc())
                .first()
            )

            last_inbound = (
                session.query(Message)
                .filter(Message.user_id == user.id, Message.direction == "in")
                .order_by(Message.created_at.desc())
                .first()
            )

            if not last_inbound:
                continue

            now = datetime.now(timezone.utc)
            hours_since_meal = None
            if last_meal:
                hours_since_meal = (now - last_meal.eaten_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600

            hours_since_activity = (now - last_inbound.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600

            # Only nudge if active in last 24h but no meal logged
            if hours_since_activity < 24 and (hours_since_meal is None or hours_since_meal > 24):
                if hours_since_meal is not None and hours_since_meal >= 48:
                    send_scheduled_message(user.id, "adherence_firm")
                else:
                    send_scheduled_message(user.id, "adherence_gentle")
    finally:
        session.close()


def start_scheduler():
    """Initialize and start the scheduler."""
    schedule_all_users()
    scheduler.add_job(
        check_meal_adherence,
        trigger=CronTrigger(hour=20, minute=0),
        id="global_adherence_check",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("Scheduler started.")


def stop_scheduler():
    """Shut down the scheduler."""
    scheduler.shutdown()
    logger.info("Scheduler stopped.")
