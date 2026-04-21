from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Text, Float, DateTime, Boolean, JSON, ForeignKey
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
import config

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
    wake_time = Column(String(10), default=None)  # HH:MM format
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
    onboarding_step = Column(Integer, default=0)  # 0=not started, 1=welcome sent, 2=clarification sent, 3=complete
    quiet_until = Column(DateTime, default=None)  # suppress outbound messages until this time (set when user says goodnight)
    user_timezone = Column(String(50), default="America/Los_Angeles")  # IANA timezone string
    memory = Column(Text, default=None)  # permanent extracted facts about the user — preferences, life events, PRs, etc.
    coaching_summary = Column(Text, default=None)  # rolling summary of coaching decisions and progress
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    calories_today = Column(Integer, default=0)  # running total for today
    protein_today = Column(Integer, default=0)
    carbs_today = Column(Integer, default=0)
    fat_today = Column(Integer, default=0)
    totals_date = Column(String(10), default=None)  # YYYY-MM-DD — the date these totals are for

    weigh_in_day = Column(String(10), default=None)  # "monday", "tuesday", etc. — user-picked weekly weigh-in day
    existing_tools = Column(Text, default=None)  # comma-separated apps/devices: "strava,whoop,apple_watch"
    tools_decision = Column(String(20), default=None)  # "migrate", "coexist", or "none"
    pending_photo_meal = Column(Text, default=None)  # JSON blob of initial photo estimate, cleared after user answers

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


def get_or_create_today_log(session, user_id: int) -> "DailyLog":
    """Get today's DailyLog for a user, creating it if it doesn't exist."""
    from sqlalchemy import func
    today = datetime.now(timezone.utc).date()
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


def init_db():
    """Create all tables."""
    Base.metadata.create_all(engine)


def get_session():
    """Get a new database session."""
    return Session()


if __name__ == "__main__":
    init_db()
    print("Database initialized.")