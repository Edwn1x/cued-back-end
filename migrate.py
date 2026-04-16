"""
One-time migration script — adds columns that were added to models.py
after the initial database was created.

Run once on the production database:
    python migrate.py

Safe to run multiple times — each ALTER TABLE is wrapped in a try/except
that ignores "column already exists" errors.
"""

import logging
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
    # Added: confirmed decisions
    "ALTER TABLE users ADD COLUMN confirmed_goal_priority VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN confirmed_training_split VARCHAR(50)",
    "ALTER TABLE users ADD COLUMN confirmed_workout_time VARCHAR(10)",
    "ALTER TABLE users ADD COLUMN confirmed_training_days VARCHAR(100)",
    # Added: workout_confirmed on daily_logs
    "ALTER TABLE daily_logs ADD COLUMN workout_confirmed BOOLEAN DEFAULT FALSE",
]

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
