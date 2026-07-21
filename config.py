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

# Phase 4 — heartbeat (proactive). A dumb clock, a smart decision, default silent.
# Burn-in runs on the founder's number only (allowlist), on top of the live loop.
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "false").lower() == "true"
HEARTBEAT_ALLOWLIST = [p.strip() for p in os.getenv("HEARTBEAT_ALLOWLIST", "").split(",") if p.strip()]
HEARTBEAT_TICK_MINUTES = int(os.getenv("HEARTBEAT_TICK_MINUTES", "45"))     # dumb clock interval
HEARTBEAT_JITTER_SECONDS = int(os.getenv("HEARTBEAT_JITTER_SECONDS", "600"))  # 0-10 min offset — kills the :00/:30 tell
HEARTBEAT_MAX_PER_DAY = int(os.getenv("HEARTBEAT_MAX_PER_DAY", "5"))        # hard cap (guardrail)
HEARTBEAT_ACTIVE_CONVO_MINUTES = 30    # a recent inbound => obvious-silence pre-gate
HEARTBEAT_RECENT_TICKS = 8             # tick decisions fed into the next tick (anti-repetition)
# Burn-in ships search ON on the proactive path; measure speak rate, then decide a budget.
HEARTBEAT_WEB_SEARCH = os.getenv("HEARTBEAT_WEB_SEARCH", "true").lower() == "true"

# Phase 5 — nightly consolidation + episodic digest. The first writers to memory
# NOT triggered by a user turn, so every knob below is a guardrail against silent
# cross-night drift. All default off/safe.
CONSOLIDATION_ENABLED = os.getenv("CONSOLIDATION_ENABLED", "false").lower() == "true"
CONSOLIDATION_STALE_DAYS = int(os.getenv("CONSOLIDATION_STALE_DAYS", "30"))  # never-used non-safety fact older than this -> close
CONSOLIDATION_MAX_DELTA_FRACTION = float(os.getenv("CONSOLIDATION_MAX_DELTA_FRACTION", "0.5"))  # a run removing >this fraction of valid entries ABORTS
CONSOLIDATION_HOUR = int(os.getenv("CONSOLIDATION_HOUR", "4"))               # nightly run hour, off-peak (Pacific; single-tz base)
CONSOLIDATION_MODEL = HAIKU_MODEL                                            # coaching-summary refresh (cheap)
# Episodic digest — a cheap dated prose note of non-fitness life context when a
# conversation goes quiet. Raw material for heartbeat follow-ups; distinct from the
# watermark summarizer (which owns coaching decisions).
EPISODIC_ENABLED = os.getenv("EPISODIC_ENABLED", "false").lower() == "true"
EPISODIC_QUIET_MINUTES = int(os.getenv("EPISODIC_QUIET_MINUTES", "90"))      # conversation "quiet" threshold (the trigger)
EPISODIC_SWEEP_MINUTES = int(os.getenv("EPISODIC_SWEEP_MINUTES", "30"))      # how often the sweep looks for quiet convos
EPISODIC_MODEL = HAIKU_MODEL                                                 # cheap digest pass
EPISODIC_RECENT_DAYS = int(os.getenv("EPISODIC_RECENT_DAYS", "5"))          # window recent_episodic() surfaces into context
EPISODIC_MIN_MESSAGES = int(os.getenv("EPISODIC_MIN_MESSAGES", "4"))        # don't digest a trivial 1-2 line exchange
