"""
One-time migration script — adds columns that were added to models.py
after the initial database was created.

Run once on the production database:
    python migrate.py

Safe to run multiple times — each ALTER TABLE is wrapped in a try/except
that ignores "column already exists" errors.
"""

import logging
import time
from sqlalchemy import text
from models import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("migrate")

MIGRATIONS = [
    # Added: per-user timezone
    "ALTER TABLE users ADD COLUMN user_timezone VARCHAR(50) DEFAULT 'America/Los_Angeles'",
    # Added: onboarding state tracking
    "ALTER TABLE users ADD COLUMN onboarding_step INTEGER DEFAULT 0",
    # Added: pending clarification tracking
    "ALTER TABLE users ADD COLUMN pending_clarification_topic VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN pending_clarification_answer TEXT",
    # Added: macro targets
    "ALTER TABLE users ADD COLUMN calorie_target INTEGER",
    "ALTER TABLE users ADD COLUMN protein_target INTEGER",
    "ALTER TABLE users ADD COLUMN targets_explained BOOLEAN DEFAULT FALSE",
    # Added: food context and tone
    "ALTER TABLE users ADD COLUMN food_context TEXT",
    "ALTER TABLE users ADD COLUMN communication_style TEXT",
    # Added: engagement tracking
    "ALTER TABLE users ADD COLUMN unanswered_count INTEGER DEFAULT 0",
    # Added: memory fields
    "ALTER TABLE users ADD COLUMN memory TEXT",
    "ALTER TABLE users ADD COLUMN coaching_summary TEXT",
    # Added: goodnight quiet mode
    "ALTER TABLE users ADD COLUMN quiet_until TIMESTAMP",
    # Added: daily totals cache on users
    "ALTER TABLE users ADD COLUMN calories_today INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN protein_today INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN carbs_today INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN fat_today INTEGER DEFAULT 0",
    "ALTER TABLE users ADD COLUMN totals_date VARCHAR(10)",
    # Added: confirmed decisions
    "ALTER TABLE users ADD COLUMN confirmed_goal_priority VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN confirmed_training_split VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN confirmed_workout_time VARCHAR(10)",
    "ALTER TABLE users ADD COLUMN confirmed_training_days VARCHAR(100)",
    # Added: workout_confirmed on daily_logs
    "ALTER TABLE daily_logs ADD COLUMN workout_confirmed BOOLEAN DEFAULT FALSE",
    # Added: weigh-in scheduling + tools tracking + photo meal pending state
    "ALTER TABLE users ADD COLUMN weigh_in_day VARCHAR(10)",
    "ALTER TABLE users ADD COLUMN existing_tools TEXT",
    "ALTER TABLE users ADD COLUMN tools_decision VARCHAR(20)",
    "ALTER TABLE users ADD COLUMN pending_photo_meal TEXT",
    "ALTER TABLE users ADD COLUMN active_meal_id INTEGER",
    "ALTER TABLE users ADD COLUMN active_meal_updated_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN avg_steps INTEGER",
    "ALTER TABLE users ADD COLUMN current_split VARCHAR(50)",
    # Added: variable wake time support
    "ALTER TABLE users ADD COLUMN wake_time_alt VARCHAR(10)",
    "ALTER TABLE users ADD COLUMN wake_days_alt VARCHAR(50)",
    # Added: weight_logs table (CREATE TABLE IF NOT EXISTS)
    """CREATE TABLE IF NOT EXISTS weight_logs (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id),
        weighed_at TIMESTAMP DEFAULT NOW(),
        weight_lbs FLOAT NOT NULL,
        notes TEXT
    )""",
    # Added: Berkeley-specific profile fields
    "ALTER TABLE users ADD COLUMN which_gym VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN meal_plan_status VARCHAR(20)",
    "ALTER TABLE users ADD COLUMN year VARCHAR(20)",
    # Added: onboarding A/B tracking
    "ALTER TABLE users ADD COLUMN onboarding_hook_template VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN first_reply_at TIMESTAMP",
    # Added: feature state tracking
    "ALTER TABLE users ADD COLUMN features_introduced JSON",
    "ALTER TABLE users ADD COLUMN coaching_branch VARCHAR(30)",
    "ALTER TABLE users ADD COLUMN seen_exercise_demos JSON",
    # Added: dining hall menu cache table
    """CREATE TABLE IF NOT EXISTS dining_menu_items (
        id SERIAL PRIMARY KEY,
        scraped_date VARCHAR(10) NOT NULL,
        hall VARCHAR(50) NOT NULL,
        meal_period VARCHAR(20) NOT NULL,
        station VARCHAR(100),
        item_name VARCHAR(200) NOT NULL,
        calories INTEGER,
        protein_g FLOAT,
        carbs_g FLOAT,
        fat_g FLOAT,
        fiber_g FLOAT,
        serving_size VARCHAR(50),
        allergens TEXT,
        dietary_tags TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_dining_date_hall ON dining_menu_items (scraped_date, hall)",
    "ALTER TABLE users ADD COLUMN session_state JSON",
    # Phase A memory architecture: categorized profile JSON, coaching-point repetition guard, summary watermark
    "ALTER TABLE users ADD COLUMN user_profile_memory JSON",
    "ALTER TABLE users ADD COLUMN delivered_coaching_points TEXT",
    "ALTER TABLE users ADD COLUMN last_compressed_message_id INTEGER",
    # Phase C1.5: token_usage table for measured cost tracking
    """CREATE TABLE IF NOT EXISTS token_usage (
        id SERIAL PRIMARY KEY,
        user_id INTEGER REFERENCES users(id),
        created_at TIMESTAMP DEFAULT NOW(),
        model VARCHAR(10),
        site VARCHAR(60),
        input_tokens INTEGER DEFAULT 0,
        cache_creation_input_tokens INTEGER DEFAULT 0,
        cache_read_input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cost_usd FLOAT DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_token_usage_created_at ON token_usage (created_at)",
    "CREATE INDEX IF NOT EXISTS idx_token_usage_user_created ON token_usage (user_id, created_at)",
    # Fix C1.5 FK gap: original token_usage.user_id FK had no ON DELETE clause,
    # which defaults to NO ACTION on Postgres — meaning admin user-delete fails
    # with ForeignKeyViolation if any token_usage rows reference that user.
    # Switch to ON DELETE SET NULL so cost rows survive (real spend, keep them)
    # but stop referencing the deleted user. Idempotent via DROP IF EXISTS.
    "ALTER TABLE token_usage DROP CONSTRAINT IF EXISTS token_usage_user_id_fkey",
    "ALTER TABLE token_usage ADD CONSTRAINT token_usage_user_id_fkey FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL",
    # Waitlist endpoint — new live site (cued.fit) signs users up here before
    # admin activates. See plans/cued-memory-architecture-joyful-ullman.md.
    "ALTER TABLE users ADD COLUMN email VARCHAR(200)",
    "ALTER TABLE users ADD COLUMN signup_source VARCHAR(40)",
    "ALTER TABLE users ADD COLUMN waitlist_status VARCHAR(20)",
    "ALTER TABLE users ADD COLUMN activated_at TIMESTAMP",
    # Partial index for the admin Waitlist tab — keeps the query fast even
    # as the waitlist grows. Postgres-only syntax (SQLite ignores WHERE
    # clause silently; that's fine for dev).
    "CREATE INDEX IF NOT EXISTS idx_users_waitlist_pending ON users (waitlist_status) WHERE waitlist_status = 'pending'",
    # Phase 1: webhook idempotency ledger — dedup Twilio MessageSid retries so a
    # slow-webhook re-delivery can't double-write state. UNIQUE(message_sid) is
    # the whole mechanism. ON DELETE SET NULL so user-delete never blocks on it.
    """CREATE TABLE IF NOT EXISTS processed_messages (
        id SERIAL PRIMARY KEY,
        message_sid VARCHAR(64) UNIQUE NOT NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        received_at TIMESTAMP DEFAULT NOW()
    )""",
    # Phase 1: append-only episodic Event table (went_to_gym, in_class, ...),
    # written synchronously by the deterministic inbound detector; read by the
    # scheduler gates windowed to the user's LOCAL day.
    """CREATE TABLE IF NOT EXISTS events (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        event_type VARCHAR(30) NOT NULL,
        occurred_at TIMESTAMP DEFAULT NOW(),
        ends_at TIMESTAMP,
        source VARCHAR(20) DEFAULT 'regex',
        raw_text TEXT,
        created_at TIMESTAMP DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_user_occurred ON events (user_id, occurred_at)",
]

def wait_for_db(retries=10, delay=3):
    for attempt in range(1, retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Database is ready.")
            return
        except Exception as e:
            logger.warning(f"DB not ready (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                time.sleep(delay)
    raise SystemExit("Could not connect to the database after multiple retries.")

def run_migrations():
    """Apply every idempotent statement in MIGRATIONS. Importable so the suite
    can run it against a test DB (guarded below so `import migrate` has no side
    effects)."""
    with engine.connect() as conn:
        for sql in MIGRATIONS:
            try:
                conn.execute(text(sql))
                conn.commit()
                logger.info(f"OK: {sql[:60]}...")
            except Exception as e:
                conn.rollback()
                if "already exists" in str(e).lower():
                    logger.info(f"SKIP (already exists): {sql[:60]}...")
                else:
                    logger.error(f"FAILED: {sql[:60]}... — {e}")
    logger.info("Migration complete.")


if __name__ == "__main__":
    wait_for_db()
    run_migrations()

logger.info("Migration complete.")
