from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
from models import get_session, User
from coach import generate_scheduled_message
from sms import send_sms
from engagement_tracker import should_send, is_question_type, increment_unanswered, get_tier
from models import is_workout_confirmed_today
import logging

logger = logging.getLogger(__name__)
scheduler = BackgroundScheduler()


def parse_time(time_str: str) -> tuple[int, int]:
    """Parse 'HH:MM' into (hour, minute)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def add_minutes(time_str: str, minutes: int) -> str:
    """Add minutes to a 'HH:MM' time string."""
    h, m = parse_time(time_str)
    dt = datetime(2000, 1, 1, h, m) + timedelta(minutes=minutes)
    return dt.strftime("%H:%M")


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

        # post_workout check-in only fires if user confirmed they trained today
        if message_type == "post_workout" and not is_workout_confirmed_today(user.id):
            logger.info(f"Skipping post_workout for {user.name} — no workout confirmed today")
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
    """Set up all daily scheduled messages for a user."""
    if not user.wake_time or not user.workout_time:
        logger.warning(f"Skipping schedule for {user.name} — missing wake_time or workout_time")
        # Still schedule morning briefing if we have wake time
        if user.wake_time:
            wake_h, wake_m = parse_time(user.wake_time)
            job_id = f"user_{user.id}_morning"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            scheduler.add_job(
                send_scheduled_message,
                trigger=CronTrigger(hour=wake_h, minute=wake_m),
                args=[user.id, "morning"],
                id=job_id,
                replace_existing=True,
                misfire_grace_time=300,
            )
            logger.info(f"Scheduled morning-only for {user.name} (no workout_time yet)")
        return

    wake_h, wake_m = parse_time(user.wake_time)
    workout_h, workout_m = parse_time(user.workout_time)

    # Calculate message times relative to wake and workout
    breakfast_time = add_minutes(user.wake_time, 30)
    pre_workout_time = add_minutes(user.workout_time, -15)
    post_workout_time = add_minutes(user.workout_time, 75)

    breakfast_h, breakfast_m = parse_time(breakfast_time)
    pre_h, pre_m = parse_time(pre_workout_time)
    post_h, post_m = parse_time(post_workout_time)

    # Define the daily schedule
    touchpoints = [
        ("morning",      wake_h,      wake_m),
        ("breakfast",    breakfast_h,  breakfast_m),
        ("lunch",        12,           30),
        ("pre_workout",  pre_h,        pre_m),
        ("post_workout", post_h,       post_m),
        ("dinner",       18,           0),
        ("evening",      21,           0),
    ]

    all_jobs = touchpoints + [("nudge", wake_h, wake_m + 5)]

    for msg_type, hour, minute in all_jobs:
        job_id = f"user_{user.id}_{msg_type}"

        # Remove existing job if rescheduling
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

        scheduler.add_job(
            send_scheduled_message,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[user.id, msg_type],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,  # 5 min grace period
        )

    logger.info(
        f"Scheduled {len(touchpoints)} daily touchpoints for {user.name}: "
        f"wake={user.wake_time}, workout={user.workout_time}"
    )

    # Schedule weekly weigh-in if user has a weigh-in day set
    if user.weigh_in_day and user.wake_time:
        day_map = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        day_num = day_map.get(user.weigh_in_day.lower())
        if day_num is not None:
            wake_h, wake_m = parse_time(user.wake_time)
            weighin_minute = wake_m + 10 if wake_m < 50 else wake_m - 10
            job_id = f"user_{user.id}_weigh_in"
            if scheduler.get_job(job_id):
                scheduler.remove_job(job_id)
            scheduler.add_job(
                send_scheduled_message,
                trigger=CronTrigger(day_of_week=day_num, hour=wake_h, minute=weighin_minute),
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
            schedule_user(user)
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
            if (user.onboarding_step or 0) < 4:
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
