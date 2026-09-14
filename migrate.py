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
    # Phase 1: split pointer (last completed split day + when + provenance).
    "ALTER TABLE users ADD COLUMN split_pointer_day VARCHAR(30)",
    "ALTER TABLE users ADD COLUMN split_pointer_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN split_pointer_source VARCHAR(10)",
    # Phase 3: soft-delete for manage_log (filter via models.active()).
    "ALTER TABLE meals ADD COLUMN deleted_at TIMESTAMP",
    "ALTER TABLE workouts ADD COLUMN deleted_at TIMESTAMP",
    "ALTER TABLE events ADD COLUMN deleted_at TIMESTAMP",
    # Phase 4: heartbeat tick decision log (anti-repetition signal).
    """CREATE TABLE IF NOT EXISTS heartbeat_ticks (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        decided_at TIMESTAMP DEFAULT NOW(),
        spoke BOOLEAN DEFAULT FALSE,
        reason TEXT,
        message TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_heartbeat_user_decided ON heartbeat_ticks (user_id, decided_at)",
    # Phase 5: nightly consolidation audit/rollback + episodic digest.
    "ALTER TABLE users ADD COLUMN last_episodic_message_id INTEGER",
    """CREATE TABLE IF NOT EXISTS consolidation_runs (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        ran_at TIMESTAMP DEFAULT NOW(),
        valid_before INTEGER DEFAULT 0,
        removed_count INTEGER DEFAULT 0,
        aborted BOOLEAN DEFAULT FALSE,
        summary TEXT,
        diff JSONB,
        prev_profile JSONB
    )""",
    "CREATE INDEX IF NOT EXISTS idx_consolidation_user_ran ON consolidation_runs (user_id, ran_at)",
    """CREATE TABLE IF NOT EXISTS episodic_digests (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        occurred_on TIMESTAMP DEFAULT NOW(),
        text TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT NOW(),
        deleted_at TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_episodic_user_occurred ON episodic_digests (user_id, occurred_on)",
    # Burn-in: append-only edit audit for manage_log edits (meals/workouts/events).
    "ALTER TABLE meals ADD COLUMN edits JSONB",
    "ALTER TABLE workouts ADD COLUMN edits JSONB",
    "ALTER TABLE events ADD COLUMN edits JSONB",
    # Addendum: heartbeat search-budget instrumentation (decision, not just outcome).
    "ALTER TABLE heartbeat_ticks ADD COLUMN search_available BOOLEAN DEFAULT FALSE",
    "ALTER TABLE heartbeat_ticks ADD COLUMN search_used BOOLEAN DEFAULT FALSE",
    "ALTER TABLE heartbeat_ticks ADD COLUMN search_query TEXT",
    # users.activity_level is a free-text phrase from the onboarding extractor; prod
    # was widened to VARCHAR(500) by hand, models.py now declares 500 — this makes
    # any DB created before that match. Widening is idempotent + non-destructive.
    "ALTER TABLE users ALTER COLUMN activity_level TYPE VARCHAR(500)",
    # Same drift, found live 2026-09-11 (user 27's workout_time was a 44-char phrase that
    # prod accepted and the model would not): align every free-text-ish onboarding column
    # to prod's actual width. Widening only — idempotent, non-destructive.
    "ALTER TABLE users ALTER COLUMN workout_time TYPE VARCHAR(50)",
    "ALTER TABLE users ALTER COLUMN confirmed_workout_time TYPE VARCHAR(500)",
    "ALTER TABLE users ALTER COLUMN confirmed_training_split TYPE VARCHAR(500)",
    "ALTER TABLE users ALTER COLUMN wake_time TYPE VARCHAR(500)",
    "ALTER TABLE users ALTER COLUMN sleep_time TYPE VARCHAR(500)",
    "ALTER TABLE users ALTER COLUMN cooking_situation TYPE VARCHAR(500)",
    # Photon migration Phase 2: channel routing + delivery outcome. DEFAULTs backfill
    # every existing row as sms/sent so the keystone (increment_unanswered) never sees
    # an ambiguous NULL on legacy rows. Index name matches SQLAlchemy's index=True
    # convention so create_all and migrate agree on a fresh DB.
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS channel VARCHAR(10) DEFAULT 'sms'",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS provider_sid VARCHAR(80)",
    "ALTER TABLE messages ADD COLUMN IF NOT EXISTS delivery_status VARCHAR(12) DEFAULT 'sent'",
    "CREATE INDEX IF NOT EXISTS ix_messages_provider_sid ON messages (provider_sid)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_channel VARCHAR(10) DEFAULT 'sms'",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS channel_failed_over BOOLEAN DEFAULT FALSE",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS channel_failover_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS photon_user_id VARCHAR(64)",
    # Bounded target override (2026-09-14): the user's pick vs the computed pair.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS calorie_target_computed INTEGER",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS protein_target_computed INTEGER",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS targets_source VARCHAR(16)",
    # Photon migration Phase 4B: unknown-sender ledger (Business-tier trigger count).
    """CREATE TABLE IF NOT EXISTS unknown_inbounds (
        id SERIAL PRIMARY KEY,
        handle VARCHAR(200) NOT NULL,
        channel VARCHAR(10) DEFAULT 'imessage',
        body_preview VARCHAR(200),
        received_at TIMESTAMP DEFAULT NOW()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_unknown_inbounds_received ON unknown_inbounds (received_at)",
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

import re

# ─── Lock discipline (incident 2026-09-11) ────────────────────────────────────
# ALTER TABLE takes an ACCESS EXCLUSIVE lock — even to discover the column already
# exists. At deploy, one app connection left "idle in transaction" made this runner
# wait 14 minutes on `users`, and EVERY users read (admin, heartbeat, inbound turns)
# queued behind the pending ALTER: a table-wide stall dressed as a "stuck build".
#   1. Pre-check via information_schema: an already-applied statement is skipped
#      WITHOUT requesting the lock.
#   2. lock_timeout: a statement that can't get its lock in LOCK_TIMEOUT fails
#      instead of queueing the world; retried LOCK_RETRIES times with a pause.
#   3. Still blocked → raise → boot fails LOUDLY (Procfile `&&`), the previous
#      container keeps serving, and the deployment shows FAILED — not BUILDING.
LOCK_TIMEOUT = "15s"
LOCK_RETRIES = 3
LOCK_RETRY_PAUSE_S = 5

_ADD_COLUMN = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.I)
_ALTER_TYPE = re.compile(r"ALTER\s+TABLE\s+(\w+)\s+ALTER\s+COLUMN\s+(\w+)\s+TYPE\s+VARCHAR\((\d+)\)", re.I)


_FK_CONSTRAINT = re.compile(
    r"ALTER\s+TABLE\s+(\w+)\s+(?:DROP\s+CONSTRAINT\s+IF\s+EXISTS|ADD\s+CONSTRAINT)\s+(\w+_fkey)\b"
    r"(?:.*?ON\s+DELETE\s+(SET\s+NULL|CASCADE|RESTRICT|NO\s+ACTION))?", re.I | re.S)


def _fk_pair_target_rule(sql: str):
    """The DROP/ADD CONSTRAINT pair is one logical change; the DROP line carries no
    rule, so find the matching ADD in MIGRATIONS to know the intended delete rule."""
    m = _FK_CONSTRAINT.search(sql)
    if not m:
        return None, None
    name, rule = m.group(2), m.group(3)
    if rule is None:
        for other in MIGRATIONS:
            om = _FK_CONSTRAINT.search(other)
            if om and om.group(2) == name and om.group(3):
                rule = om.group(3)
                break
    return name, (rule.upper().replace("  ", " ") if rule else None)


def already_applied(conn, sql: str):
    """Return a reason string when `sql` is provably already applied (so it can be
    skipped without touching any lock), else None. Pre-checked shapes: ADD COLUMN,
    ALTER COLUMN TYPE VARCHAR(n), and the DROP/ADD FOREIGN KEY pair (which locks
    BOTH tables — the referenced `users` included). CREATE … IF NOT EXISTS already
    checks before locking."""
    fk_name, fk_rule = _fk_pair_target_rule(sql)
    if fk_name:
        row = conn.execute(text(
            "SELECT delete_rule FROM information_schema.referential_constraints WHERE constraint_name=:n"),
            {"n": fk_name.lower()}).first()
        if row and fk_rule and row[0].upper() == fk_rule:
            return f"constraint {fk_name} already ON DELETE {fk_rule}"
        return None
    m = _ADD_COLUMN.search(sql)
    if m:
        table, col = m.group(1), m.group(2)
        row = conn.execute(text(
            "SELECT 1 FROM information_schema.columns WHERE table_name=:t AND column_name=:c"),
            {"t": table.lower(), "c": col.lower()}).first()
        return f"column {table}.{col} exists" if row else None
    m = _ALTER_TYPE.search(sql)
    if m:
        table, col, width = m.group(1), m.group(2), int(m.group(3))
        row = conn.execute(text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name=:t AND column_name=:c"), {"t": table.lower(), "c": col.lower()}).first()
        if row and row[0] is not None and row[0] >= width:
            return f"{table}.{col} already VARCHAR({row[0]})"
        return None
    return None


def _is_lock_timeout(err: Exception) -> bool:
    msg = str(err).lower()
    return "lock timeout" in msg or "lock_not_available" in msg or "canceling statement due to lock timeout" in msg


def run_migrations(*, sleep=time.sleep):
    """Apply every idempotent statement in MIGRATIONS. Importable so the suite
    can run it against a test DB (guarded below so `import migrate` has no side
    effects).

    Raises if any statement fails for a reason OTHER than "already exists" — a
    genuine failure must stop the caller (boot), not get logged and ignored.
    Deploy runs this before the app starts (see Procfile); an uncaught raise
    here exits non-zero and blocks the app from serving with a half-applied
    schema. Lock discipline: see the block above."""
    failures = []
    with engine.connect() as conn:
        conn.execute(text(f"SET lock_timeout = '{LOCK_TIMEOUT}'"))
        conn.commit()
        for sql in MIGRATIONS:
            try:
                reason = already_applied(conn, sql)
            except Exception as e:  # a pre-check failure never blocks the migration itself
                logger.warning(f"PRECHECK failed ({e}) — executing: {sql[:60]}...")
                reason = None
            if reason:
                logger.info(f"SKIP ({reason}, no lock): {sql[:60]}...")
                continue
            for attempt in range(1, LOCK_RETRIES + 1):
                try:
                    conn.execute(text(sql))
                    conn.commit()
                    logger.info(f"OK: {sql[:60]}...")
                    break
                except Exception as e:
                    conn.rollback()
                    if "already exists" in str(e).lower():
                        logger.info(f"SKIP (already exists): {sql[:60]}...")
                        break
                    if _is_lock_timeout(e) and attempt < LOCK_RETRIES:
                        logger.warning(f"LOCK_TIMEOUT attempt {attempt}/{LOCK_RETRIES} — another session "
                                       f"holds a lock on this table (check pg_stat_activity for 'idle in "
                                       f"transaction'); retrying in {LOCK_RETRY_PAUSE_S}s: {sql[:60]}...")
                        sleep(LOCK_RETRY_PAUSE_S)
                        continue
                    logger.error(f"FAILED: {sql[:60]}... — {e}")
                    failures.append((sql, e))
                    break
    logger.info("Migration complete.")
    if failures:
        raise RuntimeError(
            f"{len(failures)} migration statement(s) failed: "
            + "; ".join(f"{sql[:60]}... ({e})" for sql, e in failures)
        )


if __name__ == "__main__":
    wait_for_db()
    run_migrations()
