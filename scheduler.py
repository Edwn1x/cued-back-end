from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from models import get_session
import config
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


# has_unanswered_outbound moved to engagement_tracker.py (a coach-free home) so the
# proactive path can gate on it without importing this legacy templated-scheduler
# module. Re-exported here for the scheduler's own callers. See phase-6 INVESTIGATION.
from engagement_tracker import has_unanswered_outbound


def start_scheduler():
    """Initialize and start the scheduler."""

    from dining_scraper import scrape_all_halls
    scheduler.add_job(
        scrape_all_halls,
        trigger=CronTrigger(hour=5, minute=30, timezone=ZoneInfo("America/Los_Angeles")),
        id="daily_dining_scrape",
        replace_existing=True,
    )
    try:
        scrape_all_halls()
    except Exception as e:
        logger.warning(f"Startup dining scrape failed: {e}")

    # iMessage-first signup: users who got an opt-in link but chose nothing get
    # their hook by SMS (with the link) after ONBOARDING_HOOK_FALLBACK_MINUTES.
    from apscheduler.triggers.interval import IntervalTrigger as _IT
    from onboarding_agent import send_fallback_hooks
    scheduler.add_job(
        send_fallback_hooks,
        trigger=_IT(minutes=2),
        id="onboarding_hook_fallback",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # Water-reminder offer for EXISTING users: every 10 min, guarded by the heartbeat's
    # own guardrails, each user at most once ever (water_offer.sweep).
    if config.WATER_OFFER_ENABLED:
        from water_offer import sweep as water_offer_sweep
        scheduler.add_job(
            water_offer_sweep,
            trigger=_IT(minutes=10),
            id="water_offer_sweep",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )

    # Google Calendar sync (Part 1): incremental pull per connected user every 30 min
    # (pure API + DB, no model). Flag-gated OFF; no-op when no gcal users are connected.
    if config.GCAL_ENABLED:
        from integrations.gcal_sync import sync_all as gcal_sync_all
        scheduler.add_job(
            gcal_sync_all,
            trigger=_IT(minutes=30),
            id="gcal_sync",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("Google Calendar sync scheduled: every 30 min.")

    # Google Health sync (Part 2a): daily sleep / steps / HR / weight per connected user
    # every 30 min (pure API + DB, no model). Webhook notifications make it fresher;
    # this is the floor.
    if config.GOOGLE_HEALTH_ENABLED:
        from integrations.google_health_sync import sync_all as google_health_sync_all
        scheduler.add_job(
            google_health_sync_all,
            trigger=_IT(minutes=30),
            id="google_health_sync",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("Google Health sync scheduled: every 30 min.")

    # bCourses feed sync (Part 1.4): re-pull every pasted Canvas ICS feed every 6h
    # (pure HTTP + DB, no model). Flag-gated OFF; no-op when no feeds are on file.
    if config.BCOURSES_ENABLED:
        from integrations.bcourses import sync_all as bcourses_sync_all
        scheduler.add_job(
            bcourses_sync_all,
            trigger=_IT(hours=6),
            id="bcourses_sync",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("bCourses feed sync scheduled: every 6h.")

    # Canvas token sync (Part 1.4b): planner items + submission status every 30 min
    # (submission freshness matters — a nag about turned-in homework is the failure).
    if config.CANVAS_ENABLED:
        from integrations.canvas import sync_all as canvas_sync_all
        scheduler.add_job(
            canvas_sync_all,
            trigger=_IT(minutes=30),
            id="canvas_sync",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("Canvas token sync scheduled: every 30 min.")

    # Adaptive targets: daily sweep; each user is only DUE every 14 days.
    if config.ADAPTIVE_TARGETS_ENABLED:
        from adaptive_targets import run_all as adaptive_run_all
        scheduler.add_job(
            adaptive_run_all,
            trigger=CronTrigger(hour=5, minute=45, timezone=ZoneInfo("America/Los_Angeles")),
            id="adaptive_targets_daily", replace_existing=True, coalesce=True, max_instances=1,
        )

    # Workout card: a session still open 6h after it started → abandoned, silently.
    from apscheduler.triggers.interval import IntervalTrigger as _IT2
    from workouts.session_ops import abandon_stale
    scheduler.add_job(abandon_stale, trigger=_IT2(minutes=30), id="workout_abandon_sweep",
                      replace_existing=True, coalesce=True, max_instances=1)

    # RSF crowd meter (series §2): poll during hours, refresh hours weekly, propose
    # beats every 15 min, poll open queue tickets every 60s. All flag-gated inside.
    from apscheduler.triggers.interval import IntervalTrigger as _IT3
    from integrations.rsf import poll_once as rsf_poll_once, refresh_hours as rsf_refresh_hours
    from gym_beats import sweep as gym_beats_sweep, poll_open_tickets
    scheduler.add_job(rsf_poll_once, trigger=_IT3(minutes=config.RSF_POLL_MINUTES), id="rsf_meter_poll",
                      replace_existing=True, coalesce=True, max_instances=1)
    scheduler.add_job(rsf_refresh_hours, trigger=CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=ZoneInfo("America/Los_Angeles")),
                      id="rsf_hours_refresh", replace_existing=True)
    scheduler.add_job(gym_beats_sweep, trigger=_IT3(minutes=15), id="gym_beats_sweep",
                      replace_existing=True, coalesce=True, max_instances=1)
    scheduler.add_job(poll_open_tickets, trigger=_IT3(seconds=60), id="queue_ticket_poll",
                      replace_existing=True, coalesce=True, max_instances=1)

    # Reminders: explicit "remind me" promises fire at the named local time, on their
    # own 60s sweep — never on the heartbeat's cadence. Flag-gated inside fire_due.
    from reminders import fire_due as reminders_fire_due
    scheduler.add_job(reminders_fire_due, trigger=_IT3(seconds=60), id="reminders_fire",
                      replace_existing=True, coalesce=True, max_instances=1)

    # Held outbound (Photon outage): messages parked on an opted-in user's line
    # are re-tried here and delivered in order once Photon answers again; stale
    # ones expire. Cheap no-op when nothing is held. See held_outbound.drain.
    from held_outbound import drain as held_outbound_drain
    scheduler.add_job(held_outbound_drain, trigger=_IT3(seconds=config.IMESSAGE_HOLD_DRAIN_SECONDS),
                      id="held_outbound_drain", replace_existing=True, coalesce=True, max_instances=1)

    # Phase 4 — heartbeat. A dumb interval clock; each fire runs a per-user
    # decision (default silent) with guardrails in code. Jitter the interval so
    # ticks never land on a predictable :00/:45 boundary — the message-shape tell
    # the founder called out. The tick itself is cheap when silent; the decision
    # call only runs after code guardrails pass.
    if config.HEARTBEAT_ENABLED:
        from apscheduler.triggers.interval import IntervalTrigger
        from heartbeat import heartbeat_all
        scheduler.add_job(
            heartbeat_all,
            trigger=IntervalTrigger(minutes=config.HEARTBEAT_TICK_MINUTES),
            id="global_heartbeat",
            jitter=config.HEARTBEAT_JITTER_SECONDS,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info("Heartbeat scheduled: every %s min (+/- %ss jitter).",
                    config.HEARTBEAT_TICK_MINUTES, config.HEARTBEAT_JITTER_SECONDS)

    # Phase 5 — nightly consolidation (off-peak; single-tz user base) + episodic
    # digest sweep (fires when a conversation has gone quiet). Both flag-gated.
    if config.CONSOLIDATION_ENABLED:
        from consolidation import consolidate_all
        scheduler.add_job(
            consolidate_all,
            trigger=CronTrigger(hour=config.CONSOLIDATION_HOUR, minute=0,
                                timezone=ZoneInfo("America/Los_Angeles")),
            id="nightly_consolidation", replace_existing=True,
            coalesce=True, max_instances=1,
        )
        logger.info("Consolidation scheduled: daily at %02d:00 Pacific.", config.CONSOLIDATION_HOUR)

    if config.EPISODIC_ENABLED:
        from apscheduler.triggers.interval import IntervalTrigger
        from episodic import digest_all
        scheduler.add_job(
            digest_all,
            trigger=IntervalTrigger(minutes=config.EPISODIC_SWEEP_MINUTES),
            id="episodic_digest_sweep", replace_existing=True,
            coalesce=True, max_instances=1,
        )
        logger.info("Episodic digest sweep scheduled: every %s min.", config.EPISODIC_SWEEP_MINUTES)

    scheduler.start()
    logger.info("Scheduler started.")


def stop_scheduler():
    """Shut down the scheduler."""
    scheduler.shutdown()
    logger.info("Scheduler stopped.")
