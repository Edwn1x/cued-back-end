import os
from dotenv import load_dotenv

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
_raw_db_url = os.getenv("DATABASE_URL", "sqlite:///baseline.db")
if _raw_db_url.startswith("postgres://"):
    _raw_db_url = _raw_db_url.replace("postgres://", "postgresql+psycopg://", 1)
elif _raw_db_url.startswith("postgresql://"):
    _raw_db_url = _raw_db_url.replace("postgresql://", "postgresql+psycopg://", 1)
DATABASE_URL = _raw_db_url
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-key-change-me")
PROFILE_BASE_URL = os.getenv("PROFILE_BASE_URL", "https://cued.fit/profile.html")

# CORS — comma-separated list of allowed frontend origins, e.g. "https://mycued.com,https://www.mycued.com"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

# Coach settings
COACH_MODEL = "claude-sonnet-4-6"
MAX_RESPONSE_TOKENS = 400  # keep SMS responses concise
CONVERSATION_HISTORY_LIMIT = 50  # last N messages to include in prompt context

# Phase A memory architecture — see plans/cued-memory-architecture-joyful-ullman.md
USER_PROFILE_MEMORY_CHAR_LIMIT = 2000  # global hard cap; eviction trigger
USER_PROFILE_MEMORY_CATEGORY_SOFT_CAP = 400  # per-category soft cap so no bucket starves others
COACHING_POINTS_CHAR_LIMIT = 1000  # delivered_coaching_points cap
# Feature flag — when false, build_memory_block returns legacy user.memory blob for every agent_type.
# Extractions still WRITE to user_profile_memory so flipping back to true preserves data.
USER_PROFILE_MEMORY_ENABLED = os.getenv("USER_PROFILE_MEMORY_ENABLED", "true").lower() == "true"
