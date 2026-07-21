import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, Boolean, JSON, ForeignKey
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import config

logger = logging.getLogger("cued.models")

engine = create_engine(config.DATABASE_URL, echo=False)
Session = sessionmaker(bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, nullable=False)
    name = Column(String(100), nullable=False)
    age = Column(Integer)
    gender = Column(String(20))  # male, female, non_binary, prefer_not_to_say
    occupation = Column(String(200))  # what they do — student, desk job, physical labor, etc.
    goal = Column(String(200))  # comma-separated: fat_loss,muscle_building,etc.
    goal_other = Column(Text)  # freeform if they picked "other"
    biggest_obstacle = Column(String(50))  # consistency, nutrition, knowledge, time, motivation, injuries
    experience = Column(String(20))  # beginner, intermediate, advanced
    prior_coaching = Column(String(5))  # yes, no
    equipment = Column(String(100))  # full_gym, home_gym, bodyweight
    injuries = Column(Text)  # injuries or physical limitations
    activity_level = Column(String(20))  # sedentary, lightly_active, active, very_active
    diet = Column(String(100))  # omnivore, vegetarian, vegan, etc.
    restrictions = Column(Text)  # allergies, dislikes
    cooking_situation = Column(String(20))  # cook_myself, dining_hall, mostly_eat_out, mix
    meals_per_day = Column(String(5))  # 1-2, 3, 4+
    schedule = Column(Text)  # workout days/times, class schedule
    schedule_details = Column(Text)  # freeform: classes, work, commitments
    wake_time = Column(String(10), default=None)  # HH:MM format — primary wake time
    wake_time_alt = Column(String(10), default=None)  # HH:MM — secondary wake time (e.g. 12:00 on off days)
    wake_days_alt = Column(String(50), default=None)  # comma-separated days that use wake_time_alt (e.g. "mon,wed,fri")
    sleep_time = Column(String(10), default=None)  # target bedtime
    sleep_quality = Column(String(20))  # great, okay, poor, terrible
    stress_level = Column(String(20))  # low, moderate, high, very_high
    workout_time = Column(String(10), default=None)
    workout_days = Column(String(100))  # comma-separated: mon,tue,wed,etc.
    height_ft = Column(Integer)
    height_in = Column(Integer)
    weight_lbs = Column(Float)
    body_fat_pct = Column(Float)  # optional, if they know it
    wearable = Column(String(50))  # apple_watch, oura, garmin, none
    motivation = Column(Text)  # why they want coaching — personal touch
    active = Column(Boolean, default=True)
    unanswered_count = Column(Integer, default=0)  # increments on outbound questions with no reply; resets on any reply
    communication_style = Column(Text, default=None)  # auto-derived tone descriptor, updated after enough exchanges
    food_context = Column(Text, default=None)  # what they actually have/eat — fridge contents, nearby restaurants, go-to orders
    calorie_target = Column(Integer, default=None)  # computed daily calorie target
    protein_target = Column(Integer, default=None)  # computed daily protein target (grams)
    targets_explained = Column(Boolean, default=False)  # True once the coach has explained the targets to the user
    confirmed_goal_priority = Column(String(50), default=None)  # "cutting" or "building" — set once user confirms
    confirmed_training_split = Column(String(50), default=None)  # "ppl", "upper_lower", "full_body", etc.
    confirmed_workout_time = Column(String(10), default=None)  # user-confirmed workout time, separate from default
    confirmed_training_days = Column(String(100), default=None)  # user-confirmed days, e.g. "mon,tue,thu,fri,sat"
    pending_clarification_topic = Column(String(50), default=None)  # topic of unanswered onboarding question
    pending_clarification_answer = Column(Text, default=None)  # user's answer once received
    onboarding_step = Column(Integer, default=0)  # 0=not started, 1=hook sent, 2=collecting, 3=complete
    quiet_until = Column(DateTime, default=None)  # suppress outbound messages until this time (set when user says goodnight)
    user_timezone = Column(String(50), default="America/Los_Angeles")  # IANA timezone string
    memory = Column(Text, default=None)  # permanent extracted facts about the user — preferences, life events, PRs, etc.
    coaching_summary = Column(Text, default=None)  # rolling summary of coaching decisions and progress
    # Phase A memory architecture — categorized profile, per-call coaching points, summary watermark.
    # Helper read/write happens in memory.py; column-as-source-of-truth rule keeps weight/diet/etc. out of this JSON.
    user_profile_memory = Column(JSON, default=None)  # {category: [{id, text, ts, uses, safety?}]} — see memory.py
    delivered_coaching_points = Column(Text, default=None)  # capped list of recommendations already given — prevents repetition
    last_compressed_message_id = Column(Integer, default=None)  # watermark for Phase B summary/raw-history boundary
    last_episodic_message_id = Column(Integer, default=None)  # Phase 5 watermark: episodic digest idempotency (independent of the summary watermark)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    calories_today = Column(Integer, default=0)  # running total for today
    protein_today = Column(Integer, default=0)
    carbs_today = Column(Integer, default=0)
    fat_today = Column(Integer, default=0)
    totals_date = Column(String(10), default=None)  # YYYY-MM-DD — the date these totals are for

    weigh_in_day = Column(String(10), default=None)  # "monday", "tuesday", etc. — user-picked weekly weigh-in day
    existing_tools = Column(Text, default=None)  # comma-separated apps/devices: "strava,whoop,apple_watch"
    tools_decision = Column(String(20), default=None)  # "migrate", "coexist", or "none"
    avg_steps = Column(Integer, default=None)  # average daily step count from onboarding
    current_split = Column(String(50), default=None)  # "ppl", "upper_lower", "full_body", "bro_split", "custom", "none"
    # Phase 1 split pointer: two facts + provenance. The last COMPLETED split day
    # and when, advanced only by code (split_pointer.advance_split_pointer). source
    # distinguishes a user-confirmed day from an inferred one (an unnamed "already
    # went" advance), so the model can hedge on inferred days and a named
    # correction overwrites an inferred same-day value. NOT a "today's workout"
    # field — the model derives that from the pointer at reasoning time.
    split_pointer_day = Column(String(30), default=None)
    split_pointer_at = Column(DateTime, default=None)
    split_pointer_source = Column(String(10), default=None)  # "confirmed" | "inferred"
    pending_photo_meal = Column(Text, default=None)  # JSON blob of initial photo estimate, cleared after user answers
    active_meal_id = Column(Integer, default=None)    # FK to meals.id — meal currently being discussed/refined
    active_meal_updated_at = Column(DateTime, default=None)  # last touch of the active meal context
    session_state = Column(JSON, default=None)  # {"status": "at_gym"|"logging_food"|"sleeping", "started_at": ISO, ...}

    # Berkeley-specific profile fields
    which_gym = Column(String(50), default=None)         # rsf / dorm / apartment / off_campus
    meal_plan_status = Column(String(20), default=None)  # meal_plan / no_meal_plan / flex_only
    year = Column(String(20), default=None)              # freshman / sophomore / junior / senior / grad / other

    # Onboarding A/B tracking
    onboarding_hook_template = Column(String(50), default=None)  # which hook was assigned (for A/B analysis)
    first_reply_at = Column(DateTime, default=None)              # timestamp of first inbound message after hook

    # Waitlist (live site at cued.fit signs users up here before admin activates).
    # See plans/cued-memory-architecture-joyful-ullman.md — Waitlist Endpoint section.
    # User.active stays True for waitlist users; waitlist_status is the sole waitlist marker.
    email = Column(String(200), default=None)              # optional, validated only if present
    signup_source = Column(String(40), default=None)       # "hero"|"nav"|... — accept any string ≤40 chars; don't validate
    waitlist_status = Column(String(20), default=None)     # None = not on waitlist (legacy + activated). "pending" = currently on waitlist.
    activated_at = Column(DateTime, default=None)          # stamped when admin promotes from waitlist; source of truth for "ever activated"

    # Feature state
    features_introduced = Column(JSON, default=None)   # {"food_photo": true, "receipt": true}
    coaching_branch = Column(String(30), default=None) # "training_nutrition" or "nutrition_only"
    seen_exercise_demos = Column(JSON, default=None)   # {"bench_press": true, ...}

    messages = relationship("Message", back_populates="user", order_by="Message.created_at")
    workouts = relationship("Workout", back_populates="user", order_by="Workout.date.desc()")
    meals = relationship("Meal", back_populates="user", order_by="Meal.eaten_at.desc()")
    weight_logs = relationship("WeightLog", back_populates="user", order_by="WeightLog.weighed_at.desc()")
    daily_logs = relationship("DailyLog", back_populates="user", order_by="DailyLog.date.desc()")

    @property
    def profile_summary(self):
        """Build a profile string for the LLM prompt context."""
        height_str = None
        if self.height_ft:
            height_str = f"{self.height_ft}'{self.height_in or 0}\""

        parts = [
            f"Name: {self.name}",
            f"Age: {self.age}" if self.age else None,
            f"Gender: {self.gender}" if self.gender and self.gender != "prefer_not_to_say" else None,
            f"Occupation: {self.occupation}" if self.occupation else None,
            f"Goal: {self.goal}" + (f" — {self.goal_other}" if self.goal_other else ""),
            f"Biggest obstacle: {self.biggest_obstacle}" if self.biggest_obstacle else None,
            f"Experience: {self.experience}",
            f"Has worked with a coach before: {self.prior_coaching}" if self.prior_coaching else None,
            f"Equipment: {self.equipment}",
            f"Injuries/limitations: {self.injuries}" if self.injuries else None,
            f"Activity level outside gym: {self.activity_level}" if self.activity_level else None,
            f"Diet: {self.diet}" if self.diet else None,
            f"Restrictions: {self.restrictions}" if self.restrictions else None,
            f"Cooking situation: {self.cooking_situation}" if self.cooking_situation else None,
            f"Meals per day: {self.meals_per_day}" if self.meals_per_day else None,
            f"Workout days: {self.workout_days}" if self.workout_days else None,
            f"Schedule/commitments: {self.schedule_details}" if self.schedule_details else None,
            f"Wake time: {self.wake_time}, Bedtime: {self.sleep_time}" if self.wake_time or self.sleep_time else None,
            f"Sleep quality: {self.sleep_quality}" if self.sleep_quality else None,
            f"Stress level: {self.stress_level}" if self.stress_level else None,
            f"Workout time: {self.workout_time}",
            f"Height: {height_str}" if height_str else None,
            f"Weight: {self.weight_lbs}lbs" if self.weight_lbs else None,
            f"Body fat: ~{self.body_fat_pct}%" if self.body_fat_pct else None,
            f"Wearable: {self.wearable}" if self.wearable else None,
            f"Motivation: {self.motivation}" if self.motivation else None,
            f"Food context (actual foods/restaurants they use): {self.food_context}" if self.food_context else None,
            f"Gym: {self.which_gym}" if self.which_gym else None,
            f"Meal plan: {self.meal_plan_status}" if self.meal_plan_status else None,
            f"Year: {self.year}" if self.year else None,
            f"Coaching branch: {self.coaching_branch}" if self.coaching_branch else None,
            f"Pending clarification — coach asked about '{self.pending_clarification_topic}' and is waiting for answer" if self.pending_clarification_topic and not self.pending_clarification_answer else None,
            f"Clarification received — coach asked about '{self.pending_clarification_topic}', user answered: {self.pending_clarification_answer}" if self.pending_clarification_topic and self.pending_clarification_answer else None,
        ]
        return "\n".join(p for p in parts if p)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    direction = Column(String(3), nullable=False)  # "in" or "out"
    body = Column(Text, nullable=False)
    message_type = Column(String(30))  # morning, breakfast, lunch, dinner, workout, post_workout, evening, freeform
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="messages")


class Workout(Base):
    __tablename__ = "workouts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    workout_type = Column(String(50))  # push, pull, legs, full_body, upper, lower, cardio, rest
    exercises = Column(JSON)  # list of {name, sets, reps, weight, notes}
    user_notes = Column(Text)  # what the user reported back
    ai_notes = Column(Text)  # coach's parsed observations
    completed = Column(Boolean, default=False)
    deleted_at = Column(DateTime, default=None)  # soft delete — filter via models.active()

    user = relationship("User", back_populates="workouts")


class Meal(Base):
    __tablename__ = "meals"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    eaten_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # when the user actually ate it
    logged_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))  # when the system logged it
    description = Column(Text, nullable=False)  # "chicken burrito bowl from chipotle"
    calories = Column(Integer)
    protein_g = Column(Integer)
    carbs_g = Column(Integer)
    fat_g = Column(Integer)
    source = Column(String(20))  # "text", "photo"
    log_type = Column(String(30))  # "user_reported", "confirmed_suggestion"
    confidence = Column(String(10))  # "high", "medium", "low"
    notes = Column(Text)  # any clarifying details
    deleted_at = Column(DateTime, default=None)  # soft delete — filter via models.active()

    user = relationship("User", back_populates="meals")


class WeightLog(Base):
    __tablename__ = "weight_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    weighed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    weight_lbs = Column(Float, nullable=False)
    notes = Column(Text)  # optional context from user

    user = relationship("User", back_populates="weight_logs")


class DailyLog(Base):
    __tablename__ = "daily_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    date = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    sleep_hours = Column(Float)
    energy_level = Column(Integer)  # 1-5
    daily_rating = Column(Integer)  # 1-5
    weight = Column(Float)
    meals = Column(JSON)  # {breakfast: {planned, adherence}, lunch: {...}, dinner: {...}}
    notes = Column(Text)
    workout_confirmed = Column(Boolean, default=False)  # True only when user explicitly confirmed training

    user = relationship("User", back_populates="daily_logs")


class DiningMenuItem(Base):
    __tablename__ = "dining_menu_items"

    id = Column(Integer, primary_key=True)
    scraped_date = Column(String(10), nullable=False)   # YYYY-MM-DD
    hall = Column(String(50), nullable=False)           # crossroads / foothill / clark_kerr / cafe3
    meal_period = Column(String(20), nullable=False)    # breakfast / lunch / dinner / brunch
    station = Column(String(100))
    item_name = Column(String(200), nullable=False)
    calories = Column(Integer)
    protein_g = Column(Float)
    carbs_g = Column(Float)
    fat_g = Column(Float)
    fiber_g = Column(Float)
    serving_size = Column(String(50))
    allergens = Column(Text)     # comma-separated: nuts, gluten, dairy...
    dietary_tags = Column(Text)  # comma-separated: vegan, vegetarian, halal
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class TokenUsage(Base):
    """
    One row per Anthropic messages.create() call. Persisted because Railway
    log retention can't support historical aggregation. Stores raw token
    buckets (so we can recompute) AND the as-charged cost_usd at insert time
    (so historical totals stay correct if MODEL_PRICING changes later).
    See plans/cued-memory-architecture-joyful-ullman.md — Phase C1.5.
    """
    __tablename__ = "token_usage"

    id = Column(Integer, primary_key=True)
    # ON DELETE SET NULL: when a user is deleted, the cost row survives
    # (it represents real spend that already happened) but its user_id
    # becomes NULL — treated as a "system call" thereafter. Without this,
    # admin user-delete fails with ForeignKeyViolation on Postgres.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)
    model = Column(String(10))   # "sonnet" | "haiku"
    site = Column(String(60))    # e.g. "coach.get_coach_response", "extract_and_store_memory"
    input_tokens = Column(Integer, default=0)                  # fresh uncached input
    cache_creation_input_tokens = Column(Integer, default=0)
    cache_read_input_tokens = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cost_usd = Column(Float, default=0.0)


class ProcessedMessage(Base):
    """
    Webhook idempotency ledger — one row per Twilio MessageSid we have accepted
    for processing. The UNIQUE constraint on message_sid is the whole mechanism:
    a re-delivered inbound (Twilio retries webhooks it can't answer within ~15s,
    which a slow synchronous classify_message can trip) hits the constraint and
    is deduped instead of double-writing state. Append-only; kept off the hot
    `messages` table so it's independently revertable.

    Claimed at the top of the webhook (so concurrent retries dedupe) and RELEASED
    on an unhandled exception in the synchronous pass (so a crash-after-claim
    doesn't leave a poisoned claim that would silently drop Twilio's retry).
    """
    __tablename__ = "processed_messages"

    id = Column(Integer, primary_key=True)
    message_sid = Column(String(64), unique=True, nullable=False)
    # ON DELETE SET NULL so deleting a user never fails on this ledger (same
    # discipline as token_usage); the dedup row is not worth blocking a delete.
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    received_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Event(Base):
    """
    Append-only episodic record: things that happened. Written synchronously on
    the inbound path by a deterministic regex floor for the two nudge-critical
    types (went_to_gym, in_class) — same pattern as the safety pre-pass. Read via
    events.todays_events(), which windows by the user's LOCAL day (a 5pm-Pacific
    gym visit is 1am-UTC-tomorrow, so a naive UTC window would mis-bucket it).

    Timestamps are stored as naive UTC (matching quiet_until / session_state).
    """
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(30), nullable=False)  # went_to_gym | in_class | skipped | ate | traveling | life
    occurred_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    ends_at = Column(DateTime, nullable=True)         # e.g. in_class end (naive UTC); None = use default duration
    source = Column(String(20), default="regex")      # regex | model
    raw_text = Column(Text)                            # the message snippet that triggered detection
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    deleted_at = Column(DateTime, default=None)        # soft delete — filter via models.active()


class HeartbeatTick(Base):
    """One row per heartbeat tick decision (spoke or silent + why). Fed back into
    the next tick's context so the coach can't re-conclude and re-send the same
    nudge three times (the anti-repetition signal, distinct from the daily cap)."""
    __tablename__ = "heartbeat_ticks"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    decided_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    spoke = Column(Boolean, default=False)
    reason = Column(Text)              # silence reason, or "spoke"
    message = Column(Text)             # the composed message when spoke


class ConsolidationRun(Base):
    """One row per nightly consolidation attempt per user. Home for the three
    audit guarantees: the JSON diff, the human-readable summary (founder's daily
    sanity check), and the pre-run profile snapshot for rollback. `aborted` marks
    a run the bounded-delta guardrail rejected (live memory left untouched)."""
    __tablename__ = "consolidation_runs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ran_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    valid_before = Column(Integer, default=0)      # count of valid (non-history) entries pre-run
    removed_count = Column(Integer, default=0)     # entries closed/merged out this run
    aborted = Column(Boolean, default=False)       # bounded-delta guardrail tripped -> nothing written
    summary = Column(Text)                         # one-line human-readable per-user change summary
    diff = Column(JSON)                            # structured before/after of touched entries
    prev_profile = Column(JSON)                    # pre-run user_profile_memory snapshot (rollback)


class EpisodicDigest(Base):
    """A short dated prose note of the non-obvious substance of a session —
    especially NON-fitness life context ("orgo midterm tomorrow", "moving out").
    The heartbeat's raw material for personal follow-ups. Kept OUT of
    user_profile_memory so nightly consolidation never dedupes/closes it. Distinct
    from the watermark summarizer (coaching decisions) and from Event (structured
    nudge detections). Soft-deletable — filter via models.active()."""
    __tablename__ = "episodic_digests"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    occurred_on = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))
    deleted_at = Column(DateTime, default=None)    # soft delete — filter via models.active()


def claim_message_sid(message_sid: str, user_id: int = None) -> bool:
    """
    Claim a Twilio MessageSid for idempotent processing.

    Returns True  -> PROCEED (newly claimed, OR fail-open on a missing sid /
                     unexpected error — a rare duplicate beats a dropped message).
    Returns False -> this sid was already processed; caller should stop (duplicate).

    Fail-open is deliberate: on a messaging rail, never let dedup bookkeeping drop
    a real message. Only a genuine UNIQUE violation returns False.
    """
    if not message_sid:
        return True
    session = get_session()
    try:
        session.add(ProcessedMessage(message_sid=message_sid, user_id=user_id))
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False
    except Exception as e:  # noqa: BLE001 — fail open on anything unexpected
        session.rollback()
        logger.warning("SID_CLAIM_FAILED sid=%s err=%s — failing open", message_sid, e)
        return True
    finally:
        session.close()


def release_message_sid(message_sid: str):
    """Release a claimed MessageSid (best-effort) so a crashed pass can be retried."""
    if not message_sid:
        return
    session = get_session()
    try:
        session.query(ProcessedMessage).filter(
            ProcessedMessage.message_sid == message_sid).delete()
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        logger.warning("SID_RELEASE_FAILED sid=%s err=%s", message_sid, e)
    finally:
        session.close()


def get_or_create_today_log(session, user_id: int) -> "DailyLog":
    """Get today's DailyLog for a user, creating it if it doesn't exist."""
    from sqlalchemy import func
    from zoneinfo import ZoneInfo
    user = session.get(User, user_id)
    try:
        user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles") if user else ZoneInfo("America/Los_Angeles")
    except Exception:
        user_tz = ZoneInfo("America/Los_Angeles")
    today = datetime.now(user_tz).date()
    log = (
        session.query(DailyLog)
        .filter(
            DailyLog.user_id == user_id,
            func.date(DailyLog.date) == today,
        )
        .first()
    )
    if not log:
        log = DailyLog(user_id=user_id)
        session.add(log)
        session.commit()
    return log


def is_workout_confirmed_today(user_id: int) -> bool:
    """Return True if the user confirmed their workout today."""
    session = get_session()
    try:
        log = get_or_create_today_log(session, user_id)
        return bool(log.workout_confirmed)
    finally:
        session.close()


def confirm_workout_today(user_id: int):
    """Mark today's workout as confirmed for this user."""
    session = get_session()
    try:
        log = get_or_create_today_log(session, user_id)
        if not log.workout_confirmed:
            log.workout_confirmed = True
            session.commit()
    finally:
        session.close()


def maybe_infer_training_days(user_id: int) -> str | None:
    """
    Look at the last 3 weeks of DailyLog.workout_confirmed to infer
    which days of the week the user consistently trains.

    Consistency rules:
    - A day is included if it appears in at least 2 of the 3 weeks
      (allows one missed week without breaking the pattern)
    - At least 3 consistent days must qualify before writing anything
      (avoids locking in a half-formed schedule in the first two weeks)
    - Only runs when confirmed_training_days is not yet set

    Returns the locked-in day string (e.g. "mon,wed,fri") or None.
    Called in a background thread after each workout confirmation.
    """
    from datetime import timedelta
    from collections import defaultdict

    DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    WEEKS_REQUIRED = 3
    WEEKS_MATCH_THRESHOLD = 2   # day must appear in at least this many weeks
    MIN_DAYS_TO_LOCK = 3        # need at least this many consistent days total

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or user.confirmed_training_days:
            return None  # already locked in, nothing to do

        today = datetime.now(timezone.utc).date()
        cutoff = today - timedelta(weeks=WEEKS_REQUIRED)

        logs = (
            session.query(DailyLog)
            .filter(
                DailyLog.user_id == user_id,
                DailyLog.workout_confirmed == True,
                DailyLog.date >= cutoff,
            )
            .all()
        )

        if not logs:
            return None

        # Group confirmed days by ISO week number
        weeks: dict[int, set[int]] = defaultdict(set)
        for log in logs:
            log_date = log.date.date() if hasattr(log.date, "date") else log.date
            week_num = log_date.isocalendar()[1]
            weeks[week_num].add(log_date.weekday())  # 0=Mon … 6=Sun

        if len(weeks) < WEEKS_REQUIRED:
            return None

        # Count how many weeks each weekday appears in
        week_sets = list(weeks.values())
        day_counts: dict[int, int] = defaultdict(int)
        for week_set in week_sets:
            for day in week_set:
                day_counts[day] += 1

        # Keep days that appear in at least WEEKS_MATCH_THRESHOLD weeks
        consistent_days = {day for day, count in day_counts.items() if count >= WEEKS_MATCH_THRESHOLD}

        if len(consistent_days) < MIN_DAYS_TO_LOCK:
            return None

        day_str = ",".join(DAY_NAMES[d] for d in sorted(consistent_days))
        user.confirmed_training_days = day_str
        session.commit()
        return day_str

    finally:
        session.close()


ACTIVE_MEAL_WINDOW_SECONDS = 600  # 10 minutes


def set_active_meal(user_id: int, meal_id: int):
    """Mark a meal as the active meal being discussed."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user:
            user.active_meal_id = meal_id
            user.active_meal_updated_at = datetime.now(timezone.utc)
            session.commit()
    finally:
        session.close()


def get_active_meal(user_id: int):
    """
    Return the active Meal row if the context window is still open, else None.
    Also clears the context if the window has expired.
    """
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not user.active_meal_id or not user.active_meal_updated_at:
            return None

        elapsed = (datetime.now(timezone.utc) - user.active_meal_updated_at.replace(tzinfo=timezone.utc)).total_seconds()
        if elapsed > ACTIVE_MEAL_WINDOW_SECONDS:
            user.active_meal_id = None
            user.active_meal_updated_at = None
            session.commit()
            return None

        meal = session.get(Meal, user.active_meal_id)
        return meal
    finally:
        session.close()


def clear_active_meal(user_id: int):
    """Explicitly close the active meal context (topic change, etc.)."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user:
            user.active_meal_id = None
            user.active_meal_updated_at = None
            session.commit()
    finally:
        session.close()


def get_session_state(user_id: int) -> dict | None:
    """Return the user's current session state, or None if stale/unset."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not user.session_state:
            return None
        state = user.session_state
        started_at = state.get("started_at")
        if started_at:
            started = datetime.fromisoformat(started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            # Auto-clear if from a previous calendar day (midnight reset)
            if started.date() < now.date():
                user.session_state = None
                session.commit()
                return None
            # Auto-clear stale gym sessions (2+ hours)
            if state.get("status") == "at_gym" and (now - started).total_seconds() > 7200:
                user.session_state = None
                session.commit()
                return None
        return state
    finally:
        session.close()


def set_session_state(user_id: int, status: str, **kwargs):
    """Set the user's session state."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user:
            state = {"status": status, "started_at": datetime.now(timezone.utc).isoformat()}
            state.update(kwargs)
            user.session_state = state
            session.commit()
    finally:
        session.close()


def clear_session_state(user_id: int):
    """Clear the user's session state."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if user and user.session_state:
            user.session_state = None
            session.commit()
    finally:
        session.close()


def resolve_pending_clarification(user_id: int, answer: str):
    """
    If a clarification question is pending and unanswered, store the user's reply as the answer.
    Called on any incoming message — first reply after the question is asked gets captured.
    """
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return
        if user.pending_clarification_topic and not user.pending_clarification_answer:
            user.pending_clarification_answer = answer.strip()
            session.commit()
    finally:
        session.close()



def ensure_todays_totals(user_id: int):
    """
    Reset today's running totals if they're from a previous day.
    Should be called before reading or updating daily totals.
    Uses the user's timezone to determine 'today'.
    """
    from zoneinfo import ZoneInfo
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return

        try:
            user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
        except Exception:
            user_tz = ZoneInfo("America/Los_Angeles")

        today_str = datetime.now(user_tz).strftime("%Y-%m-%d")

        if user.totals_date != today_str:
            user.calories_today = 0
            user.protein_today = 0
            user.carbs_today = 0
            user.fat_today = 0
            user.totals_date = today_str
            session.commit()
    finally:
        session.close()


def active(session, Model, user_id=None):
    """Soft-delete CHOKEPOINT. Every reader of a soft-deletable table (Meal,
    Workout, Event) queries through this, so a future reader can't forget the
    deleted filter and resurrect ghost rows. Returns a Query to further filter/order."""
    q = session.query(Model).filter(Model.deleted_at.is_(None))
    if user_id is not None:
        q = q.filter(Model.user_id == user_id)
    return q


def recompute_daily_totals(user_id: int):
    """Recompute today's cal/protein/carb/fat from ACTIVE meals in the user's local
    day. The counters are denormalized (incremented per meal), so a soft-delete or
    edit must recompute from source — not just filter a query — or a deleted meal's
    macros linger in the totals (the correction round-trip depends on this)."""
    from zoneinfo import ZoneInfo
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return
        try:
            tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
        except Exception:
            tz = ZoneInfo("America/Los_Angeles")
        midnight_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight_local.astimezone(timezone.utc).replace(tzinfo=None)
        end = (midnight_local + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        meals = (active(session, Meal, user_id=user_id)
                 .filter(Meal.eaten_at >= start, Meal.eaten_at < end).all())
        user.calories_today = sum(m.calories or 0 for m in meals)
        user.protein_today = sum(m.protein_g or 0 for m in meals)
        user.carbs_today = sum(m.carbs_g or 0 for m in meals)
        user.fat_today = sum(m.fat_g or 0 for m in meals)
        user.totals_date = midnight_local.strftime("%Y-%m-%d")
        session.commit()
    finally:
        session.close()


def init_db():
    """Create all tables."""
    Base.metadata.create_all(engine)


def get_session():
    """Get a new database session."""
    return Session()


if __name__ == "__main__":
    init_db()
    print("Database initialized.")