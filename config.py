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
# Admin console auth (Tier 0). When set, every /admin* route requires HTTP Basic
# auth with this password (any username). When EMPTY the console stays open —
# deliberate non-breaking rollout so a deploy without the var can't lock the
# founder out; the /admin/system page shows a red banner until it's set.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
# Every Anthropic client (llm_client.make_client) is bounded. Incident 2026-09-11: with
# the SDK default (10 min × 2 retries) one hung call held a DB transaction that blocked a
# deploy's ALTER TABLE and, behind it, every users-table read. 120s covers a search turn
# (~30–60s) with room; one retry keeps the worst case under ~5 minutes.
ANTHROPIC_TIMEOUT_S = float(os.getenv("ANTHROPIC_TIMEOUT_S", "120"))
ANTHROPIC_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "1"))
PROFILE_BASE_URL = os.getenv("PROFILE_BASE_URL", "https://cued.fit/profile.html")
# Signs the per-user profile link (see profile_page.py). Falls back to
# FLASK_SECRET_KEY so a deploy without the var still mints valid links; set a
# dedicated random value in prod so rotating one secret never touches the other.
PROFILE_TOKEN_SECRET = os.getenv("PROFILE_TOKEN_SECRET", "")
# Workout card (card_page.py): signed 24h links rendered inside the iMessage bubble.
# Falls back to PROFILE_TOKEN_SECRET then FLASK_SECRET_KEY; set its own value in prod.
CARD_TOKEN_SECRET = os.getenv("CARD_TOKEN_SECRET", "")
# The page itself lives on the site (cued-site card.html), like profile.html: the
# link users see is cued.fit; the page talks to this API with the token.
CARD_PAGE_URL = os.getenv("CARD_PAGE_URL", "https://cued.fit/card.html")

# CORS — comma-separated list of allowed frontend origins, e.g. "https://mycued.com,https://www.mycued.com"
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",")]

# Coach settings
# All reasoning surfaces run on Opus 4.8 — the most capable production-tier
# model. Same request surface as Sonnet 5 (rejects temperature/top_p/top_k and
# manual budget_tokens; adaptive thinking supported). Env-overridable as the
# rollback lever, matching the flag culture elsewhere in this file.
COACH_MODEL = os.getenv("COACH_MODEL", "claude-opus-4-8")
# SMS conciseness is PROMPT-governed; this cap is truncation insurance, sized
# for richer outputs (macro breakdowns, multi-part replies, future features) —
# a hit cap means a reply cut mid-sentence, which is strictly worse than a long
# one. Env-overridable like the other ceilings.
MAX_RESPONSE_TOKENS = int(os.getenv("MAX_RESPONSE_TOKENS", "1000"))
CONVERSATION_HISTORY_LIMIT = 50  # last N messages to include in prompt context

# Phase A memory architecture — see plans/cued-memory-architecture-joyful-ullman.md
USER_PROFILE_MEMORY_CHAR_LIMIT = 2000  # global hard cap; eviction trigger
USER_PROFILE_MEMORY_CATEGORY_SOFT_CAP = 400  # per-category soft cap so no bucket starves others
COACHING_POINTS_CHAR_LIMIT = 1000  # delivered_coaching_points cap
# Feature flag — when false, build_memory_block returns legacy user.memory blob for every agent_type.
# Extractions still WRITE to user_profile_memory so flipping back to true preserves data.
USER_PROFILE_MEMORY_ENABLED = os.getenv("USER_PROFILE_MEMORY_ENABLED", "true").lower() == "true"

# heartbeat-stale-thread Fixes 2+3 — category crowding. food_on_hand is transient
# INVENTORY: entries older than this many days are invalidated into history on the
# next write pass / nightly consolidation (0 = TTL off). Grocery bought Aug 6 is
# irrelevant by Aug 20; it must never live in the immortal constraints bucket.
FOOD_ON_HAND_TTL_DAYS = int(os.getenv("FOOD_ON_HAND_TTL_DAYS", "14"))
# Superseded safety states ("currently ill" after "fully recovered") close via the
# trigger-audited validity mechanism instead of coexisting immortally and crowding
# the category cap (the live cause of grocery/food evictions). Ship-on + instrumented
# (every closure logs at WARNING); the flag is the rollback lever.
MEMORY_SAFETY_SUPERSESSION_ENABLED = os.getenv("MEMORY_SAFETY_SUPERSESSION_ENABLED", "true").lower() == "true"

# Phase C1/C1.5 — prompt caching + cost telemetry.
# Anthropic API pricing, USD per 1M tokens. Verified Jun 2026 — update if rates change.
MODEL_PRICING = {
    "opus":   {"input": 5.00, "output": 25.00},
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
# Onboarding field extraction: Sonnet, not Haiku. Live (2026-09-11, user 27): Haiku
# inferred cooking_situation=mostly_eat_out from a malatang ANECDOTE and diet=omnivore
# from nothing — "only what the user clearly stated" needs a model that obeys it. A
# wrong field here steers every meal suggestion for the whole relationship; onboarding
# is one conversation per user, so the cost delta is noise.
ONBOARDING_EXTRACTOR_MODEL = os.getenv("ONBOARDING_EXTRACTOR_MODEL", "claude-sonnet-5")
# Post-turn memory extraction (app.extract_and_store_memory): same story, same fix.
# Live 2026-09-11 (user 27) on Haiku: constraints=["messed up"] clipped from a sentence
# (constraints render into EVERY prompt), "Thursday, Sep 12, 2026" (a Saturday) for
# "yesterday", and "im cs" stored nowhere. memory.sanitize_facts is the deterministic
# backstop under whichever model runs here.
MEMORY_EXTRACTOR_MODEL = os.getenv("MEMORY_EXTRACTOR_MODEL", "claude-sonnet-5")
WORKOUT_LOG_TIMEOUT_HOURS = 4                  # stale-session auto-finalize threshold
WORKOUT_LOG_EXIT_SUMMARY = "silent"            # "silent" | "brief" | "full" — default per user
WORKOUT_LOG_ACK_VERBOSE = False                # if True, ack shows "✓ bench 185x5"; if False, just "✓"
WORKOUT_LOGGING_ENABLED = os.getenv("WORKOUT_LOGGING_ENABLED", "true").lower() == "true"

# Phase 2 — single agent loop (inbound). Separate model key from the legacy
# COACH_MODEL (kept separate until Phase 6 unifies the surfaces): the loop runs
# on Opus 4.8, which REJECTS temperature/top_p/top_k and manual budget_tokens
# with a 400. Loop passes no sampling params; adaptive thinking + low effort
# held constant (see rewrite/phase-2/INVESTIGATION.md §5). Env-overridable as
# the rollback lever.
AGENT_LOOP_MODEL = os.getenv("AGENT_LOOP_MODEL", "claude-opus-4-8")
SINGLE_AGENT_LOOP_ENABLED = os.getenv("SINGLE_AGENT_LOOP_ENABLED", "false").lower() == "true"
# The loop's output ceiling is SEPARATE from MAX_RESPONSE_TOKENS (the SMS reply
# governor, used by legacy). A loop turn must fit adaptive-thinking tokens + one or
# more tool-call JSONs + the reply, all of which count against output — a tight cap
# truncates a multi-item turn (e.g. a calendar screenshot → several log_event calls),
# giving stop_reason=max_tokens with no text. Output is cheap relative to that
# failure, and max_tokens is a CEILING, not spend — only generated tokens bill.
# 8000 leaves room for future tools/features with heavier tool-JSON + reasoning
# turns while staying under the ~16k non-streaming SDK-timeout zone.
AGENT_LOOP_MAX_TOKENS = int(os.getenv("AGENT_LOOP_MAX_TOKENS", "8000"))

# Phase 3 tools — each behind its own flag, added one at a time.
# Bound the tool loop (unbounded agent loops = runaway bills). 8 comfortably fits the
# realistic worst case — a schedule dump wanting ~6 events plus a meal photo in one
# buffer flush — even if the model works through them across turns rather than batching.
# Inbound buffer bands, seconds (min, max), env-tunable without a deploy of code:
#   ONBOARDING_BUFFER_S  — one short answer at a time; 5–8 catches an immediate double-text.
#   REPLY_BUFFER_S       — every post-onboarding text. Founder 2026-09-23: the old
#                          "fresh thread 90–150s" band was dead code (the just-logged inbound
#                          always read as an active conversation) and 20–30 still felt long
#                          for direct asks ("send my card", "my link") — one short band now.
#   PHOTO_BUFFER_S       — a captionless photo: the caption usually follows the pic.
def _band(env, default):
    raw = os.getenv(env, default)
    try:
        lo, hi = [int(x) for x in raw.replace("-", ",").split(",")[:2]]
        return (max(1, lo), max(max(1, lo), hi))
    except Exception:
        lo, hi = [int(x) for x in default.split(",")]
        return (lo, hi)
ONBOARDING_BUFFER_S = _band("ONBOARDING_BUFFER_S", "5,8")
REPLY_BUFFER_S = _band("REPLY_BUFFER_S", "10,15")
PHOTO_BUFFER_S = _band("PHOTO_BUFFER_S", "45,60")
# Typing dots on arrival only when the wait is short; a 45–60s photo hold would leave
# dots up for a minute, so those get dots at flush (existing behaviour).
TYPING_ON_ARRIVAL_MAX_S = int(os.getenv("TYPING_ON_ARRIVAL_MAX_S", "20"))
AGENT_LOOP_MAX_TOOL_ITERS = int(os.getenv("AGENT_LOOP_MAX_TOOL_ITERS", "8"))
REMEMBER_TOOL_ENABLED = os.getenv("REMEMBER_TOOL_ENABLED", "false").lower() == "true"
LOG_WORKOUT_TOOL_ENABLED = os.getenv("LOG_WORKOUT_TOOL_ENABLED", "false").lower() == "true"
MANAGE_LOG_TOOL_ENABLED = os.getenv("MANAGE_LOG_TOOL_ENABLED", "false").lower() == "true"
LOG_MEAL_TOOL_ENABLED = os.getenv("LOG_MEAL_TOOL_ENABLED", "false").lower() == "true"
GET_DINING_MENU_TOOL_ENABLED = os.getenv("GET_DINING_MENU_TOOL_ENABLED", "false").lower() == "true"
# web_search is Anthropic's SERVER-SIDE tool (web_search_20260209 on Sonnet 5) — runs
# inline, no client handler. Adds per-search billing on top of tokens.
WEB_SEARCH_TOOL_ENABLED = os.getenv("WEB_SEARCH_TOOL_ENABLED", "false").lower() == "true"
# Cap per REPLY (founder, 2026-09-11): a friend checks one thing, maybe two — a
# search-every-turn coach feels slow (each search adds seconds). The WEB_SEARCH_QUERY
# log line (agent_tools.log_web_search_queries) is how we see what it reaches for.
WEB_SEARCH_MAX_USES = 2
# Hosts the web_search tool must never return (SEO-spam / content-farm / hijacked
# proxy mirrors). The general fix for source quality is the voice.md rule (trust the
# OFFICIAL source); this hard-blocks the specific offenders. The two proxy hosts fed a
# WRONG "RSF closes at 8pm" on 2026-09-18. Extend via env (comma-separated) without a
# code change. Base list = known content-farm patterns + those two offenders.
WEB_SEARCH_BLOCKED_DOMAINS = [d for d in ([
    "phplive-aws.uccs.edu",       # "CodeForge Hub" spam mirror (wrong RSF hours)
    "sbc-hc-proxy.stanford.edu",  # proxy gateway serving the same spam
] + [x.strip() for x in os.getenv("WEB_SEARCH_BLOCKED_DOMAINS_EXTRA", "").split(",")]) if d]
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
# lookup_events: query the FULL synced Event table (bcourses/gcal/log_event) by keyword +
# date-range, so "when is HW4 due" / "what's due in October" works past the 7-day UPCOMING
# context window (a due date weeks out is synced but not in the prompt). Read-only.
LOOKUP_EVENTS_TOOL_ENABLED = os.getenv("LOOKUP_EVENTS_TOOL_ENABLED", "true").lower() == "true"
LOOKUP_EVENTS_MAX_DAYS = int(os.getenv("LOOKUP_EVENTS_MAX_DAYS", "120"))
# Reminders (reminders.py): set_reminder/cancel_reminder tools, onboarding capture, and the
# 60s firing sweep. Ships ON (founder rule: capabilities ship on + budgeted + instrumented);
# one flag covers all three so a revert is one var.
REMINDERS_ENABLED = os.getenv("REMINDERS_ENABLED", "true").lower() == "true"
# Logger bridge (rewrite/logger-bridge): a user still logging food in another app.
# Gates the OTHER FOOD LOGGER context block, the diary-screenshot rules, parity
# lines and nightly graduation. Live 2026-09-19: user 32's MyNetDiary screenshot
# was double-logged beside its photo estimate. Off until flipped for the founder.
FOOD_LOGGER_BRIDGE_ENABLED = os.getenv("FOOD_LOGGER_BRIDGE_ENABLED", "false").lower() == "true"
SET_FOOD_LOGGER_TOOL_ENABLED = os.getenv("SET_FOOD_LOGGER_TOOL_ENABLED", "false").lower() == "true"
# Bounded target override on the coach loop (set_targets: ±15% of computed). Defaults ON —
# the same rule runs deterministically in onboarding; this just extends it past day one.
SET_TARGETS_TOOL_ENABLED = os.getenv("SET_TARGETS_TOOL_ENABLED", "true").lower() == "true"
# Post-onboarding rundown: a second bubble after the kickoff, written from
# capabilities.py for THIS user (top 3 + their obstacle). Never a feature list.
ONBOARDING_RUNDOWN_ENABLED = os.getenv("ONBOARDING_RUNDOWN_ENABLED", "true").lower() == "true"
ONBOARDING_RUNDOWN_DELAY_S = float(os.getenv("ONBOARDING_RUNDOWN_DELAY_S", "4"))
# Adaptive targets (adaptive_targets.py): log_weight tool + daily cycle sweep.
LOG_WEIGHT_TOOL_ENABLED = os.getenv("LOG_WEIGHT_TOOL_ENABLED", "true").lower() == "true"
# Lets the coach shift a user's nutrition-day rollover hour when they explicitly ask
# (default day stays midnight for everyone). On — it only acts on an explicit request.
SET_DAY_RESET_TOOL_ENABLED = os.getenv("SET_DAY_RESET_TOOL_ENABLED", "true").lower() == "true"
# save_menu: persist a menu / meal-plan / list of options a user sends "so you can log
# accurately later" (a dining-hall, frat-house, or meal-prep menu) into a per-user
# saved_menus JSON, surfaced every turn so "I ate the Wednesday burrito" logs from the
# saved macros instead of being read once and lost. TTL-aged (menus go stale), capped.
SAVE_MENU_TOOL_ENABLED = os.getenv("SAVE_MENU_TOOL_ENABLED", "true").lower() == "true"
SAVED_MENU_TTL_DAYS = int(os.getenv("SAVED_MENU_TTL_DAYS", "14"))
SAVED_MENU_MAX = int(os.getenv("SAVED_MENU_MAX", "5"))            # most-recent N kept
SAVED_MENU_MAX_ITEMS = int(os.getenv("SAVED_MENU_MAX_ITEMS", "40"))
ADAPTIVE_TARGETS_ENABLED = os.getenv("ADAPTIVE_TARGETS_ENABLED", "true").lower() == "true"
# Workout logger card (workouts/): the coach tool that sends today's session.
START_WORKOUT_TOOL_ENABLED = os.getenv("START_WORKOUT_TOOL_ENABLED", "true").lower() == "true"
# Card web-link fallback: some users don't want the Spectrum iMessage extension (needed to
# tap sets in-thread). When a user prefers it, send the card as a plain browser link (the
# card_page web app works extension-free) instead of the Photon extension card. set_card_delivery
# flips users.prefers_card_link; send_workout_card honors it.
CARD_LINK_FALLBACK_ENABLED = os.getenv("CARD_LINK_FALLBACK_ENABLED", "true").lower() == "true"
# Receipts → pantry (receipts.py). Off by default: the image pre-classifier adds
# one haiku call to every photo turn; flip after GATE 1 on the founder's phone.
RECEIPTS_ENABLED = os.getenv("RECEIPTS_ENABLED", "false").lower() == "true"
RECEIPT_CLASSIFIER_MODEL = os.getenv("RECEIPT_CLASSIFIER_MODEL", "claude-haiku-4-5-20251001")
RECEIPT_EXTRACTOR_MODEL = os.getenv("RECEIPT_EXTRACTOR_MODEL", "claude-sonnet-5")
# A full week's grocery receipt itemizes to a long JSON list; the old 1500 cap
# truncated it mid-string (stop=max_tokens) → unparseable JSON → the misleading
# "couldn't read the photo" reply looped. Give it real headroom; on truncation we
# now detect stop_reason and ask for the items instead of blaming the photo.
RECEIPT_EXTRACTOR_MAX_TOKENS = int(os.getenv("RECEIPT_EXTRACTOR_MAX_TOKENS", "8000"))
# Multi-image inbound: a user can send several photos at once (a product front +
# its nutrition label, or a few dishes) — the model should see ALL of them, not
# just the first. image_data stays the PRIMARY (first) image for single-image
# signals/paths; a parallel list carries the rest to the vision call. Capped to
# bound tokens/cost (~1–1.5k tokens/image). Flag off = first-image-only (legacy).
MULTI_IMAGE_ENABLED = os.getenv("MULTI_IMAGE_ENABLED", "true").lower() == "true"
MAX_INBOUND_IMAGES = int(os.getenv("MAX_INBOUND_IMAGES", "5"))
PANTRY_MAX_STOCKED_DAYS = 7
# RSF crowd meter + virtual line (integrations/rsf.py, occupancy.py, gym_beats.py,
# integrations/waitwell/). All default off. The Density share token is the public
# one embedded in recwell's crowd-meter page (see INVESTIGATION.md).
RSF_METER_ENABLED = os.getenv("RSF_METER_ENABLED", "false").lower() == "true"
RSF_BEATS_ENABLED = os.getenv("RSF_BEATS_ENABLED", "false").lower() == "true"
RSF_QUEUE_ENABLED = os.getenv("RSF_QUEUE_ENABLED", "false").lower() == "true"
DENSITY_SHARE_TOKEN = os.getenv("DENSITY_SHARE_TOKEN", "shr_o69HxjQ0BYrY2FPD9HxdirhJYcFDCeRolEd744Uj88e")
DENSITY_DISPLAY_ID = os.getenv("DENSITY_DISPLAY_ID", "dsp_956223069054042646")
RSF_CONTACT_EMAIL = os.getenv("RSF_CONTACT_EMAIL", "enrr865@gmail.com")
RSF_TIMEOUT_S = int(os.getenv("RSF_TIMEOUT_S", "10"))
RSF_POLL_MINUTES = int(os.getenv("RSF_POLL_MINUTES", "5"))

# ─── Integrations (OAuth: Google Calendar, Strava, bCourses) ──────────────────
# Shared plumbing (integrations/ package). Every flag defaults OFF; the whole
# surface ships dark and is flipped for Nau's number first. See the spec.
#
# Tokens are encrypted at rest with Fernet — INTEGRATION_TOKEN_ENC_KEY is REQUIRED
# to store or read any third-party token; without it integrations.crypto refuses
# rather than persist plaintext. Generate with:
#     python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
INTEGRATION_TOKEN_ENC_KEY = os.getenv("INTEGRATION_TOKEN_ENC_KEY", "")
# Signs the single-use connect-link token (integrations/tokens.py). Same fallback
# chain as the card token so a deploy without the var still mints valid links.
CONNECT_TOKEN_SECRET = os.getenv("CONNECT_TOKEN_SECRET", "")
# The host that serves /c/<provider> (connect link) and /oauth/<provider>/callback.
# This is the FLASK app's own public URL, NOT the cued.fit static site — a static
# host can't 302 into an OAuth flow. The OAuth redirect_uri registered with Google
# and Strava must exactly match "<INTEGRATIONS_BASE_URL>/oauth/<provider>/callback".
INTEGRATIONS_BASE_URL = os.getenv("INTEGRATIONS_BASE_URL", "https://web-production-90171c.up.railway.app")
# Per-provider read/write flags.
GCAL_ENABLED = os.getenv("GCAL_ENABLED", "false").lower() == "true"
BCOURSES_ENABLED = os.getenv("BCOURSES_ENABLED", "false").lower() == "true"
# Canvas personal access token (Part 1.4b): the richer bCourses connection — planner
# API with submission status. Sits ON TOP of the feed: while a token is valid it
# supersedes the feed's events; revoke → the feed sync resumes. Separate flag.
CANVAS_ENABLED = os.getenv("CANVAS_ENABLED", "false").lower() == "true"
CANVAS_BASE_URL = os.getenv("CANVAS_BASE_URL", "https://bcourses.berkeley.edu")
STRAVA_READ_ENABLED = os.getenv("STRAVA_READ_ENABLED", "false").lower() == "true"
STRAVA_POST_ENABLED = os.getenv("STRAVA_POST_ENABLED", "false").lower() == "true"
# The coach tool that texts an OAuth connect link (agent_tools.SEND_CONNECT_LINK_TOOL).
SEND_CONNECT_LINK_TOOL_ENABLED = os.getenv("SEND_CONNECT_LINK_TOOL_ENABLED", "false").lower() == "true"
# OAuth client credentials (set in prod once the provider apps exist; never logged).
GOOGLE_OAUTH_CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
GOOGLE_OAUTH_CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
STRAVA_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "")
STRAVA_CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "")
# Strava webhook subscription verify token (echoed back on the GET handshake).
STRAVA_WEBHOOK_VERIFY_TOKEN = os.getenv("STRAVA_WEBHOOK_VERIFY_TOKEN", "")
INTEGRATIONS_HTTP_TIMEOUT_S = int(os.getenv("INTEGRATIONS_HTTP_TIMEOUT_S", "15"))
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
# Standing overnight quiet window for PROACTIVE sends (heartbeat + gym beats), so a
# nudge never lands at 1am. Separate from quiet_until (a transient goodnight). Default
# 9pm–8am LOCAL; a user's parseable sleep_time/wake_time can only EXTEND it (more
# protective), never shrink below this floor. Live 2026-09-21: clearing the allowlist
# exposed 1am/5am sends. ON by default in prod; the test harness sets it false so the
# existing clock-uncontrolled heartbeat tests don't flake.
HEARTBEAT_STANDING_QUIET_ENABLED = os.getenv("HEARTBEAT_STANDING_QUIET_ENABLED", "true").lower() == "true"
HEARTBEAT_QUIET_START_HOUR = int(os.getenv("HEARTBEAT_QUIET_START_HOUR", "21"))  # 9pm local
HEARTBEAT_QUIET_END_HOUR = int(os.getenv("HEARTBEAT_QUIET_END_HOUR", "8"))       # 8am local

# Daily rhythm (rewrite/daily-rhythm/CHANGESPEC.md) — user 32: "check-ups are way too far
# apart". All five default OFF; flipped in prod after the deploy.
# MEAL GAP standing condition: an unlogged lunch at 2pm was never a reason to speak.
HEARTBEAT_MEAL_GAP_ENABLED = os.getenv("HEARTBEAT_MEAL_GAP_ENABLED", "false").lower() == "true"
# Gates only the every_hours affordance on set_reminder (water); the engine is inert without rows.
WATER_REMINDERS_ENABLED = os.getenv("WATER_REMINDERS_ENABLED", "false").lower() == "true"
# Quiet = sleep-30min .. wake+15min from the user's own 'HH:MM' profile times, else the global window.
QUIET_HOURS_FROM_PROFILE_ENABLED = os.getenv("QUIET_HOURS_FROM_PROFILE_ENABLED", "false").lower() == "true"
# MORNING OPEN / EVENING CLOSE standing conditions: the rhythm the disabled legacy briefings left behind.
HEARTBEAT_RHYTHM_ENABLED = os.getenv("HEARTBEAT_RHYTHM_ENABLED", "false").lower() == "true"
# set_checkin_level tool: "text me more" / "chill with the texts" becomes a code-enforced cap, not a said ok.
SET_CHECKIN_LEVEL_TOOL_ENABLED = os.getenv("SET_CHECKIN_LEVEL_TOOL_ENABLED", "false").lower() == "true"
# Water-reminder OFFER (water_offer.py): the one-line, once-only "want water pings? yes or no"
# after the kickoff (new users) and on a guarded sweep (existing users); the reply is
# handled in code. Founder 2026-09-22: users can't ask for a feature they've never heard of.
WATER_OFFER_ENABLED = os.getenv("WATER_OFFER_ENABLED", "false").lower() == "true"

# STOP opt-out (iMessage — SMS is Twilio/carrier-handled). Deliberately high-friction to
# avoid ACCIDENTAL opt-outs losing a user: the trigger is "STOP" or "UNSUBSCRIBE" as the
# WHOLE message (any case, period optional — founder 2026-09-23: the confirmation step is
# the buffer), which then sends a confirmation; only a second STOP actually opts out. "pause" takes a few days off instead. Any inbound
# resumes an opted-out or paused user. Ships flag-gated OFF. See optout.py.
STOP_OPTOUT_ENABLED = os.getenv("STOP_OPTOUT_ENABLED", "false").lower() == "true"
STOP_PAUSE_DAYS = int(os.getenv("STOP_PAUSE_DAYS", "4"))
# Decide-call output ceiling — SEPARATE from MAX_RESPONSE_TOKENS (the SMS reply
# governor). One decide turn spends adaptive-thinking tokens + the send_text/
# stay_silent tool JSON (message included) + any inline-search reasoning against a
# single output budget; at 400 a long-reasoning tick truncated mid-decision and
# logged as "no message composed" (Aug 6 prod). Same lesson, same sizing as
# AGENT_LOOP_MAX_TOKENS — output is cheap relative to a silently dropped tick,
# and the ceiling only bills what's actually generated.
HEARTBEAT_DECIDE_MAX_TOKENS = int(os.getenv("HEARTBEAT_DECIDE_MAX_TOKENS", "8000"))
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

# ─── Photon / iMessage channel (PHOTON_MIGRATION_HANDOFF_v2) ──────────────────
# Two kill switches, both default OFF so the seam ships dark and is flipped
# deliberately (playbook §IV):
#   IMESSAGE_CHANNEL_ENABLED    — sms.send_sms may route a user whose
#       preferred_channel='imessage' through the sidecar. Off → everyone is SMS,
#       whatever preferred_channel says.
#   PHOTON_PROVISIONING_ENABLED — start_onboarding registers the new user with
#       the Spectrum users API (the Free/Pro allowlist) and flips their
#       preferred_channel. Off → no call; users stay SMS.
IMESSAGE_CHANNEL_ENABLED = os.getenv("IMESSAGE_CHANNEL_ENABLED", "false").lower() == "true"
PHOTON_PROVISIONING_ENABLED = os.getenv("PHOTON_PROVISIONING_ENABLED", "false").lower() == "true"
# iMessage-first signup (2026-09-14): when /signup could mint an opt-in link the
# hook is NOT sent right away — the site shows "Text me on iMessage" (their first
# blue text triggers the hook there) and "I don't have an iPhone" (hook by SMS).
# If they do neither for this many minutes, the hook goes by SMS with the link.
ONBOARDING_HOOK_FALLBACK_MINUTES = int(os.getenv("ONBOARDING_HOOK_FALLBACK_MINUTES", "10"))
SIDECAR_URL = os.getenv("SIDECAR_URL", "")                       # http://sidecar.railway.internal:8080
INTERNAL_SHARED_SECRET = os.getenv("INTERNAL_SHARED_SECRET", "")  # same value on the sidecar service
SIDECAR_TIMEOUT_S = int(os.getenv("SIDECAR_TIMEOUT_S", "15"))
# iMessage typing bubble from the moment an inbound is buffered until the reply lands
# (typing_indicator.py). ON by default (ships on + instrumented: grep TYPING_SIGNAL);
# reactive replies only.
# The heartbeat is separate and OFF: decide() may choose silence, and a bubble that
# appears and then nothing arrives reads as a glitch — founder's call after feeling it.
TYPING_INDICATOR_ENABLED = os.getenv("TYPING_INDICATOR_ENABLED", "true").lower() == "true"
# Tapback reactions + threaded replies as coach tools (agent_tools.REACT_TOOL /
# THREAD_REPLY_TOOL), offered only when the user's resolved channel is iMessage.
# The WHEN rules live in voice.md; the never-a-strike rule in engagement_tracker.
IMESSAGE_REACTIONS_ENABLED = os.getenv("IMESSAGE_REACTIONS_ENABLED", "true").lower() == "true"
TYPING_INDICATOR_HEARTBEAT = os.getenv("TYPING_INDICATOR_HEARTBEAT", "false").lower() == "true"
# "Read 11:04" on the user's message the moment it is logged (read_receipts.py),
# re-asserted when generation begins. ON by default; every reactive path incl. the ack 👍.
READ_RECEIPTS_ENABLED = os.getenv("READ_RECEIPTS_ENABLED", "true").lower() == "true"

SPECTRUM_PROJECT_ID = os.getenv("SPECTRUM_PROJECT_ID", "")
SPECTRUM_PROJECT_SECRET = os.getenv("SPECTRUM_PROJECT_SECRET", "")
SPECTRUM_API_URL = os.getenv("SPECTRUM_API_URL", "https://spectrum.photon.codes")
PHOTON_TIMEOUT_S = int(os.getenv("PHOTON_TIMEOUT_S", "10"))
