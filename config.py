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

# Phase C1/C1.5 — prompt caching + cost telemetry.
# Anthropic API pricing, USD per 1M tokens. Verified Jun 2026 — update if rates change.
MODEL_PRICING = {
    "sonnet": {"input": 3.00, "output": 15.00},
    "haiku":  {"input": 1.00, "output": 5.00},
}
CACHE_WRITE_MULTIPLIER = 1.25   # 5-min ephemeral cache write, on the input rate
CACHE_READ_MULTIPLIER  = 0.10   # cached-input read (90% off), on the input rate
TWILIO_COST_PER_SEGMENT = 0.015 # volume-based; keep separate from API cost
# Feature flag — when false, system prompts ship as a single string (no cache_control blocks).
# Lets us roll back the structural prompt-caching change without a redeploy if something breaks.
PROMPT_CACHING_ENABLED = os.getenv("PROMPT_CACHING_ENABLED", "true").lower() == "true"

# Part B — workout logging mode. State machine that puts the coach in silent
# set-by-set logging mode until the user texts an exit signal. See
# plans/cued-memory-architecture-joyful-ullman.md Part B.
HAIKU_MODEL = "claude-haiku-4-5-20251001"      # per-set parse uses Haiku (3x cheaper than Sonnet)
WORKOUT_LOG_TIMEOUT_HOURS = 4                  # stale-session auto-finalize threshold
WORKOUT_LOG_EXIT_SUMMARY = "silent"            # "silent" | "brief" | "full" — default per user
WORKOUT_LOG_ACK_VERBOSE = False                # if True, ack shows "✓ bench 185x5"; if False, just "✓"
WORKOUT_LOGGING_ENABLED = os.getenv("WORKOUT_LOGGING_ENABLED", "true").lower() == "true"

# Phase 2 — single agent loop (inbound). Separate model key from the legacy
# COACH_MODEL (which stays on claude-sonnet-4-6 until Phase 6): the loop runs on
# current-gen Sonnet 5, which REJECTS temperature/top_p/top_k and manual
# budget_tokens with a 400. Loop passes no sampling params; adaptive thinking +
# low effort held constant (see rewrite/phase-2/INVESTIGATION.md §5).
AGENT_LOOP_MODEL = "claude-sonnet-5"
SINGLE_AGENT_LOOP_ENABLED = os.getenv("SINGLE_AGENT_LOOP_ENABLED", "false").lower() == "true"

# Phase 3 tools — each behind its own flag, added one at a time.
AGENT_LOOP_MAX_TOOL_ITERS = 5   # safety bound on the tool-execution loop
REMEMBER_TOOL_ENABLED = os.getenv("REMEMBER_TOOL_ENABLED", "false").lower() == "true"
LOG_WORKOUT_TOOL_ENABLED = os.getenv("LOG_WORKOUT_TOOL_ENABLED", "false").lower() == "true"
MANAGE_LOG_TOOL_ENABLED = os.getenv("MANAGE_LOG_TOOL_ENABLED", "false").lower() == "true"
LOG_MEAL_TOOL_ENABLED = os.getenv("LOG_MEAL_TOOL_ENABLED", "false").lower() == "true"
GET_DINING_MENU_TOOL_ENABLED = os.getenv("GET_DINING_MENU_TOOL_ENABLED", "false").lower() == "true"
# web_search is Anthropic's SERVER-SIDE tool (web_search_20260209 on Sonnet 5) — runs
# inline, no client handler. Adds per-search billing on top of tokens.
WEB_SEARCH_TOOL_ENABLED = os.getenv("WEB_SEARCH_TOOL_ENABLED", "false").lower() == "true"
WEB_SEARCH_MAX_USES = 3
# read_image: send inbound MMS to the model's vision so IT routes food/calendar/
# whiteboard/other in-call (no pre-classifier). Non-food schema is PROVISIONAL until
# real screenshots refine it (see voice.md).
READ_IMAGE_ENABLED = os.getenv("READ_IMAGE_ENABLED", "false").lower() == "true"
