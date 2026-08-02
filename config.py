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
# The loop's output ceiling is SEPARATE from MAX_RESPONSE_TOKENS (400 = SMS reply
# length, used by legacy). A loop turn must fit adaptive-thinking tokens + one or more
# tool-call JSONs + the reply, all of which count against output on Sonnet 5 — 400
# truncates a multi-item turn (e.g. a calendar screenshot → several log_event calls),
# giving stop_reason=max_tokens with no text. Output is cheap relative to that failure.
AGENT_LOOP_MAX_TOKENS = int(os.getenv("AGENT_LOOP_MAX_TOKENS", "2000"))

# Phase 3 tools — each behind its own flag, added one at a time.
# Bound the tool loop (unbounded agent loops = runaway bills). 8 comfortably fits the
# realistic worst case — a schedule dump wanting ~6 events plus a meal photo in one
# buffer flush — even if the model works through them across turns rather than batching.
AGENT_LOOP_MAX_TOOL_ITERS = int(os.getenv("AGENT_LOOP_MAX_TOOL_ITERS", "8"))
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
# Macro-accuracy Phase A — portion sizing via in-frame reference objects + reading
# visible labels. A SEPARATE system block injected only on reactive image turns:
# voice.md is the heartbeat's shared cached prefix and stays untouched (see
# rewrite/macro-accuracy/INVESTIGATION.md §1.3).
MEAL_ESTIMATION_PROMPT_ENABLED = os.getenv("MEAL_ESTIMATION_PROMPT_ENABLED", "false").lower() == "true"
# Macro-accuracy Phase B — match_meal_history tool: a repeat meal uses the user's OWN
# logged macros (their portions, their prep) as the prior instead of a generic guess.
# Deterministic matcher in meal_history.py; the model judges fit and says "using your
# usual" only when true.
MEAL_HISTORY_TOOL_ENABLED = os.getenv("MEAL_HISTORY_TOOL_ENABLED", "false").lower() == "true"
# Macro-accuracy Phase C — match_dining_item tool: campus food is LOOKED UP in the
# scraped dining menu (estimation direction), not eyeballed. get_dining_menu stays the
# recommendation direction.
DINING_MATCH_TOOL_ENABLED = os.getenv("DINING_MATCH_TOOL_ENABLED", "false").lower() == "true"
# Macro-accuracy Phase D — usda_food_lookup tool: per-100g reference macros for
# identifiable-but-generic foods (USDA FoodData Central; free data.gov key). Empty key
# means the tool answers "not configured" and the coach estimates normally — fails safe
# even if the flag is on. ~1.0s measured latency (see rewrite/macro-accuracy §4).
USDA_LOOKUP_TOOL_ENABLED = os.getenv("USDA_LOOKUP_TOOL_ENABLED", "false").lower() == "true"
USDA_API_KEY = os.getenv("USDA_API_KEY", "")
USDA_TIMEOUT_S = 5
# Macro-accuracy Phase E — the escalation-routing prompt (label → history → dining →
# USDA → web → ask, by type-of-uncertainty; confidence is communication, not control).
# Rides EVERY reactive turn as a second CACHED stable system block (meals are mostly
# text; no pre-classifier exists on purpose). Heartbeat surface untouched.
MEAL_ROUTING_PROMPT_ENABLED = os.getenv("MEAL_ROUTING_PROMPT_ENABLED", "false").lower() == "true"
# log_event: the agent's write path for DATED, day-scoped calendar items (a
# calendar screenshot, "lab till 2 today"). These are Events — dated, auto-expiring,
# local-day windowed — NOT semantic memory facts. Without this the model had no way
# to persist schedule items; they fell to legacy extraction into the `schedule`
# memory category and got evicted by the per-category soft cap (burn-in finding).
LOG_EVENT_TOOL_ENABLED = os.getenv("LOG_EVENT_TOOL_ENABLED", "false").lower() == "true"
# Burn-in fix — render every timestamp in the user's LOCAL zone + a local "now" anchor,
# and inject a code-computed macro totals block. Default ON (these are corrections);
# the flag is rollback insurance if the context reshape ever regresses. See timefmt.py.
CONTEXT_LOCAL_TIME_ENABLED = os.getenv("CONTEXT_LOCAL_TIME_ENABLED", "true").lower() == "true"

# Legacy templated scheduler (morning briefing, pre/post-workout, evening wrap,
# weigh-in, meal-adherence). Disabled by default so the heartbeat is the ONLY
# proactive system during burn-in — two uncoordinated proactive systems double-
# message and confound the speak-rate/cost data. Reversible: flip on to restore
# the legacy jobs. Deletion is Phase 6's playbook, gated on a clean burn-in.
LEGACY_SCHEDULER_ENABLED = os.getenv("LEGACY_SCHEDULER_ENABLED", "false").lower() == "true"

# Phase 4 — heartbeat (proactive). A dumb clock, a smart decision, default silent.
# Burn-in runs on the founder's number only (allowlist), on top of the live loop.
HEARTBEAT_ENABLED = os.getenv("HEARTBEAT_ENABLED", "false").lower() == "true"
HEARTBEAT_ALLOWLIST = [p.strip() for p in os.getenv("HEARTBEAT_ALLOWLIST", "").split(",") if p.strip()]
HEARTBEAT_TICK_MINUTES = int(os.getenv("HEARTBEAT_TICK_MINUTES", "45"))     # dumb clock interval
HEARTBEAT_JITTER_SECONDS = int(os.getenv("HEARTBEAT_JITTER_SECONDS", "600"))  # 0-10 min offset — kills the :00/:30 tell
HEARTBEAT_MAX_PER_DAY = int(os.getenv("HEARTBEAT_MAX_PER_DAY", "5"))        # hard cap (guardrail)
HEARTBEAT_ACTIVE_CONVO_MINUTES = 30    # a recent inbound => obvious-silence pre-gate
HEARTBEAT_RECENT_TICKS = 8             # tick decisions fed into the next tick (anti-repetition)
# Anti-STACK window (guardrail): within this many minutes of an unanswered proactive
# nudge, don't send a second one. Clears on time-elapse (no user reply needed) — the
# fix for the unanswered_gap deadlock. Only proactive (heartbeat) outbounds count;
# reactive replies never gate initiation. See rewrite/heartbeat-calibration.
HEARTBEAT_STACK_WINDOW_MINUTES = int(os.getenv("HEARTBEAT_STACK_WINDOW_MINUTES", "180"))
# Addendum (post-burn-in): ON for burn-in, deliberately — proactive search is part
# of the product claim burn-in exists to validate (a coach that can check hours/
# availability before texting). The managed risk is search UN-BUDGETED, not search
# itself: HEARTBEAT_SEARCH_MAX_PER_DAY below is the code-enforced cap. This flag
# stays the kill switch — false fully disables the tool regardless of budget.
# Reactive search (WEB_SEARCH_TOOL_ENABLED) is separate.
HEARTBEAT_WEB_SEARCH = os.getenv("HEARTBEAT_WEB_SEARCH", "true").lower() == "true"
# Searched TICKS (model actually invoked search) per user-local day — spend, not
# availability; an offered-but-unused tool costs nothing. Enforced in code before
# the tool is offered (guardrail class, like quiet-hours/daily-budget — never a
# prompt rule). At/over budget the tick still runs without the tool; search
# scarcity never suppresses a message. Count derives from HeartbeatTick.search_used
# over timefmt.local_day_bounds (no denormalized counter to drift).
HEARTBEAT_SEARCH_MAX_PER_DAY = int(os.getenv("HEARTBEAT_SEARCH_MAX_PER_DAY", "3"))

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
