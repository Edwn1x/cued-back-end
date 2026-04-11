from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
from models import get_session, User
from coach import generate_scheduled_message
from sms import send_sms
from engagement_tracker import should_send, is_question_type, increment_unanswered, get_tier
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

        # Check engagement tier — skip if this touchpoint isn't allowed
        if not should_send(user, message_type):
            tier = get_tier(user.unanswered_count or 0)
            logger.info(f"Skipping {message_type} for {user.name} (tier={tier}, unanswered={user.unanswered_count})")
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


def start_scheduler():
    """Initialize and start the scheduler."""
    schedule_all_users()
    scheduler.start()
    logger.info("Scheduler started.")


def stop_scheduler():
    """Shut down the scheduler."""
    scheduler.shutdown()
    logger.info("Scheduler stopped.")
