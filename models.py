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
    wake_time = Column(String(10), default="07:00")  # HH:MM format
    sleep_time = Column(String(10), default="23:00")  # target bedtime
    sleep_quality = Column(String(20))  # great, okay, poor, terrible
    stress_level = Column(String(20))  # low, moderate, high, very_high
    workout_time = Column(String(10), default="16:00")
    workout_days = Column(String(100))  # comma-separated: mon,tue,wed,etc.
    height_ft = Column(Integer)
    height_in = Column(Integer)
    weight_lbs = Column(Float)
    body_fat_pct = Column(Float)  # optional, if they know it
    wearable = Column(String(50))  # apple_watch, oura, garmin, none
    motivation = Column(Text)  # why they want coaching — personal touch
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    messages = relationship("Message", back_populates="user", order_by="Message.created_at")
    workouts = relationship("Workout", back_populates="user", order_by="Workout.date.desc()")
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
            f"Wake time: {self.wake_time}, Bedtime: {self.sleep_time}",
            f"Sleep quality: {self.sleep_quality}" if self.sleep_quality else None,
            f"Stress level: {self.stress_level}" if self.stress_level else None,
            f"Workout time: {self.workout_time}",
            f"Height: {height_str}" if height_str else None,
            f"Weight: {self.weight_lbs}lbs" if self.weight_lbs else None,
            f"Body fat: ~{self.body_fat_pct}%" if self.body_fat_pct else None,
            f"Wearable: {self.wearable}" if self.wearable else None,
            f"Motivation: {self.motivation}" if self.motivation else None,
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

    user = relationship("User", back_populates="daily_logs")


def init_db():
    """Create all tables."""
    Base.metadata.create_all(engine)


def get_session():
    """Get a new database session."""
    return Session()


if __name__ == "__main__":
    init_db()
    print("Database initialized.")