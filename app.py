import os
import re
import json
import logging
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify, render_template_string, Response, make_response
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.middleware.proxy_fix import ProxyFix
from models import init_db, get_session, record_unknown_inbound, User, Message, Workout, DailyLog, confirm_workout_today, is_workout_confirmed_today, resolve_pending_clarification, maybe_infer_training_days, set_session_state, clear_session_state, get_session_state, claim_message_sid, release_message_sid
from sms import send_sms, log_incoming, get_twiml_response
from coach import get_coach_response, parse_workout_log
from scheduler import start_scheduler, schedule_user
import config
from onboarding_agent import start_onboarding, handle_onboarding_reply
from admin_dashboard import ADMIN_HTML
from admin_system import admin_system_bp
from engagement_tracker import reset_unanswered
from tone_analyzer import maybe_update_style
from message_buffer import buffer_message
from memory import build_memory_block, build_memory_block_with_ids, apply_facts, CATEGORIES, update_memory_uses_task, apply_safety_signals_task, extract_and_store_coaching_points_task
from events import apply_event_signals_task
from agent_loop import run_agent_loop
from cost_tracking import track as track_usage
from profile_page import profile_url, verify_profile_token, build_profile_payload
from llm_client import make_client

# ─── Setup ──────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY
CORS(app, origins=config.ALLOWED_ORIGINS)
# Introspection-driven debug console (/admin/system, /admin/heartbeat,
# /admin/consolidation, /admin/data/<table>, /admin/user/<id>/debug).
app.register_blueprint(admin_system_bp)
from card_page import card_bp
app.register_blueprint(card_bp)


@app.before_request
def _admin_auth_gate():
    """HTTP Basic auth on everything under /admin (dashboard + debug blueprint +
    the write endpoints). Empty ADMIN_PASSWORD = gate off — non-breaking rollout;
    the system page banners until the var is set."""
    if not request.path.startswith("/admin"):
        return None
    if not config.ADMIN_PASSWORD:
        return None
    import secrets as _secrets
    auth = request.authorization
    if auth and auth.type == "basic" and _secrets.compare_digest(
            auth.password or "", config.ADMIN_PASSWORD):
        return None
    return Response("Authentication required", 401,
                    {"WWW-Authenticate": 'Basic realm="cued admin"'})


if not config.ADMIN_PASSWORD:
    logger.warning("ADMIN_PASSWORD not set — /admin console is UNAUTHENTICATED "
                   "(including write actions). Set the env var to enable auth.")

# ProxyFix: Railway runs behind a TCP proxy. Without this, request.remote_addr
# resolves to Railway's edge IP, so Flask-Limiter would rate-limit ALL users
# to a single shared bucket. ProxyFix reads X-Forwarded-For and exposes the
# real client IP. x_for=1 = trust the first proxy hop (Railway). Don't increase
# without auditing each hop.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Per-IP rate limiter for public endpoints (currently /waitlist only). Memory
# backend is single-dyno; switch storage_uri to redis:// when Cued scales out.
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri="memory://",
    default_limits=[],
)

@app.errorhandler(429)
def _ratelimit_handler(e):
    """Frontend reads response.json().message — return the spec'd shape."""
    return jsonify({
        "status": "error",
        "message": "Too many submissions. Try again in a minute.",
    }), 429

# Initialize DB on startup
init_db()


# ─── Decision Extractor ──────────────────────────────
def extract_and_store_decisions(user_id: int, user_message: str, coach_response: str):
    """After each exchange, check if any decisions or profile data were confirmed and store them."""
    import anthropic
    import json

    client = make_client()

    prompt = f"""Analyze this SMS coaching exchange and extract any CONFIRMED settings, decisions, or profile data that should be stored permanently.

Extract a value if EITHER:
1. The user explicitly stated it in this exchange, OR
2. The coach is treating it as a settled fact (referencing it confidently without asking the user to confirm)

For example:
- Coach says "you're at 1050/2200 cal" → calorie_target is 2200
- Coach says "we're targeting 145g protein" → protein_target is 145
- Coach mentions "at 146lbs" → weight_lbs is 146
- User says "I'm 5'6"" → height_ft is 5, height_in is 6
- User says "I'm 20" → age is 20
- User says "lets do cutting" → goal_priority is "cutting"
- Coach says "since you're cutting" → goal_priority is "cutting"
- User says "I walk around campus" → activity_level is "lightly_active"
- User describes their meals/eating habits → food_context captures the summary
- User says "I usually wake up at 7" → wake_time is "07:00"
- User says "I try to sleep by 11" → sleep_time is "23:00"

User said: "{user_message}"
Coach said: "{coach_response}"

Return ONLY valid JSON with these fields (use null for anything not mentioned or confirmed):
{{
  "age": number or null,
  "goal_priority": "cutting" or "building" or "recomp" or null,
  "calorie_target": number or null,
  "protein_target": number or null,
  "training_split": "ppl" or "upper_lower" or "full_body" or "bro_split" or null,
  "workout_time": "HH:MM" or null,
  "training_days": "mon,tue,wed..." or null,
  "height_ft": number or null,
  "height_in": number or null,
  "weight_lbs": number or null,
  "activity_level": "sedentary" or "lightly_active" or "active" or "very_active" or null,
  "wake_time": "HH:MM" or null,
  "sleep_time": "HH:MM" or null,
  "food_context": "brief description of what they eat/cook/order, or null"
}}

If nothing can be extracted, return all null."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            # 2000: all 14 fields populated + fences + a free-text food_context is
            # ~300-400 tokens; the old 250 cap truncated live (Aug 7-8 stop= lines).
            # Doubled headroom — truncation here discards the whole extraction.
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        track_usage(user_id, "extract_and_store_decisions",
                    "claude-haiku-4-5-20251001", response)
        # Gate on stop_reason BEFORE parsing: a truncated blob that happens to
        # parse would store fields cut mid-output. Discard; next exchange re-extracts.
        if response.stop_reason == "max_tokens":
            logger.warning("BG_JOB_TRUNCATED site=extract_and_store_decisions user=%s "
                           "max_tokens=%d — discarding, nothing stored", user_id, 2000)
            return
        text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
        if "}" in text:
            text = text[:text.rindex("}") + 1]
        data = json.loads(text)

        session = get_session()
        try:
            user = session.get(User, user_id)
            if not user:
                return

            changed = False
            if data.get("age") and not user.age:
                user.age = data["age"]
                changed = True
            if data.get("goal_priority") and not user.confirmed_goal_priority:
                user.confirmed_goal_priority = data["goal_priority"]
                changed = True
            if data.get("calorie_target") and not user.calorie_target:
                user.calorie_target = data["calorie_target"]
                changed = True
            if data.get("protein_target") and not user.protein_target:
                user.protein_target = data["protein_target"]
                changed = True
            if data.get("training_split") and not user.confirmed_training_split:
                user.confirmed_training_split = data["training_split"]
                changed = True
            if data.get("workout_time") and not user.confirmed_workout_time:
                user.confirmed_workout_time = data["workout_time"]
                changed = True
            if data.get("training_days") and not user.confirmed_training_days:
                user.confirmed_training_days = data["training_days"]
                changed = True
            if data.get("height_ft") and not user.height_ft:
                user.height_ft = data["height_ft"]
                changed = True
            if data.get("height_in") is not None and user.height_in is None:
                user.height_in = data["height_in"]
                changed = True
            if data.get("weight_lbs") and not user.weight_lbs:
                user.weight_lbs = data["weight_lbs"]
                changed = True
            if data.get("activity_level") and not user.activity_level:
                user.activity_level = data["activity_level"]
                changed = True
            if data.get("wake_time") and not user.wake_time:
                user.wake_time = data["wake_time"]
                changed = True
            if data.get("sleep_time") and not user.sleep_time:
                user.sleep_time = data["sleep_time"]
                changed = True
            if data.get("food_context") and not user.food_context:
                user.food_context = data["food_context"]
                changed = True

            if changed:
                session.commit()
                logger.info(f"Stored profile data for {user.name}: {data}")
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Decision extraction failed for user {user_id}: {e}")


# ─── Memory Extractor ────────────────────────────────
def _render_existing_profile_for_prompt(profile: dict) -> str:
    """Render the current user_profile_memory as a per-category list for the
    extraction prompt. Used so Haiku can target `update`/`skip` actions against
    real existing facts via the `replaces_text` byte-exact contract."""
    if not profile:
        return "(no existing facts)"
    lines = []
    for cat in CATEGORIES:
        entries = profile.get(cat) or []
        if not entries:
            continue
        lines.append(f"[{cat}]")
        for e in entries:
            flag = " (safety)" if e.get("safety") else ""
            lines.append(f"  - {e['text']}{flag}")
    return "\n".join(lines) if lines else "(no existing facts)"


def extract_memory_facts(user_id: int, user_message: str, coach_response: str):
    """
    Phase A2/A3 extraction — the MODEL half. Ask the extractor to emit categorized
    fact records with action verbs (add/update/skip) and a safety_critical flag.
    Returns the raw fact list (possibly empty), or None when the output was
    truncated (nothing may be stored from a cut list). Split from the store so
    the live tier-2 replay can judge the model's output on its own.

    Column-as-source-of-truth: the model is told NOT to emit body metrics
    (weight/height/body fat) or dietary identity (diet/restrictions). Those
    fields have typed columns on User and are handled by the A5 regex
    pre-pass + future logic, never by JSON facts.
    """
    import anthropic
    import json

    client = make_client()

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return None
        existing_profile = dict(user.user_profile_memory or {})
        user_tz = user.user_timezone
    finally:
        session.close()

    from zoneinfo import ZoneInfo
    try:
        _tz = ZoneInfo(user_tz or "America/Los_Angeles")
    except Exception:
        _tz = ZoneInfo("America/Los_Angeles")
    _local = datetime.now(_tz)
    now_line = f"Now: {_local:%A}, {_local:%b} {_local.day}, {_local.year} (user's local time)."

    existing_block = _render_existing_profile_for_prompt(existing_profile)

    prompt = f"""You are maintaining a structured memory of a fitness coaching client from a text-message exchange. You will emit one or more fact records that the storage layer applies verbatim. The storage layer enforces safety; you do not need to.

Categories (use exactly one of these per fact):
  - identity                 — durable identity facts (school, year, job, training partner names, family context)
  - constraints              — injuries, medical issues, doctor's orders, "can't do X" hard limits
  - training_preferences     — exercise/style preferences (loves deadlifts, hates morning cardio, prefers structured plans)
  - communication_preferences— how they want to be coached (blunt callouts, gentle reminders, long explanations, terse replies)
  - schedule                 — recurring time constraints (Tuesdays class-heavy, gym closes 10pm, travels monthly for work)
  - goals                    — durable goals/motivations (lean for summer, train for hike, look good for wedding)
  - food_on_hand             — groceries / food at home not yet eaten ("did a TJ's run, got eggs and ground beef"). Transient inventory: it auto-expires as it's eaten. NEVER put on-hand food in constraints, and it is never safety_critical.

DO NOT emit facts for these (they have typed columns; storage handles them elsewhere):
  - weight, height, body fat percentage
  - diet identity (vegan, vegetarian, kosher, halal)
  - food allergies / food restrictions (lactose, gluten, "allergic to whey")
  - food context (which dining hall, favorite restaurants)
  - confirmed plan decisions (training split, workout time, coach-set calorie/protein targets)
  (Macro preferences they track to ON THEIR OWN beyond calories/protein — "I keep fat under 55g and carbs around 250g" — DO belong here, category goals, so the coach can honor them.)

Existing facts (use these for `replaces_text` on `update`, or to decide `skip`):
{existing_block}

User said: "{user_message}"
Coach said: "{coach_response}"

For each new or changed fact, emit a JSON object:
  {{
    "action": "add" | "update" | "skip",
    "category": "<one of the categories above>",
    "text": "<concise statement about the user, present tense>",
    "replaces_text": "<EXACT existing fact text being superseded, byte-for-byte; null if action != update>",
    "safety_critical": <true if this is an injury / medical / doctor's order / hard physical limit; false otherwise>
  }}

Action semantics:
  - add: a genuinely new fact not present in existing facts.
  - update: the user revealed an updated version of an existing durable fact (e.g. gym changed, school year advanced, training partner name changed). `replaces_text` MUST exactly match the existing fact text — if you're not sure of the exact text, use `add` instead.
  - skip: nothing meaningful was revealed, or the candidate is already present even if reworded. Use this liberally — empty output is fine.

{now_line}

Rules:
  - Each fact is one concise sentence written as a statement about the user.
  - NO GENDERED PRONOUNS. Write "their app", "them", "themself" — never his/her/him. The profile already carries gender; a guessed pronoun is wrong half the time and reads back to the user as a stranger's description of them.
  - A FACT IS A COMPLETE STATEMENT, never a clipped phrase. "my gym schedule is all messed up" is NOT the fact "messed up" — it is either "gym schedule is irregular because of a stacked class schedule" or nothing. If you can't write it as a full sentence about the user, skip it.
  - AN ANECDOTE IS NOT A PATTERN. One workout, one meal, one late night, one skipped session says nothing durable ("went to the gym 9-11pm last night" is not a training preference) — unless the user says it's how they usually do things, or the SAME thing has now come up more than once.
  - DIRECT IDENTITY STATEMENTS ALWAYS COUNT: "im cs", "i'm a junior", "i live at the frat", "my roommate lifts too" → identity. Don't skip a plain statement of who they are because it was short.
  - DATES, carefully. Only write a calendar date when the user named a specific day ("friday", "the 24th", "yesterday"). Compute it against Now above — "yesterday" is Now minus ONE day; write BOTH the weekday and the date and make sure they agree (a mismatch means you miscounted; recount). A habit or a recurring situation gets recurring phrasing ("late-evening sessions on stacked days like Thursday"), never a pinned date.
  - Resolve relative dates in fact text to absolute dates against Now above ("tomorrow" → the actual date). Never store bare "today"/"tomorrow"/"this afternoon" — the fact is read on later days, when those words resolve to the wrong day.
  - Do NOT emit temporary states ("is tired today") unless they're a recurring pattern.
  - Do NOT emit anything the coach said unless the user confirmed it.
  - Do NOT emit body metrics or dietary identity (see exclusion list above).
  - Doctor's orders / injuries / medical limits MUST have safety_critical=true.
  - When the user says an injury/illness is OVER, emit the recovered state as its own fact (safety_critical=true, e.g. "shoulder fully recovered") — storage closes the superseded active states it replaces.
  - If nothing meaningful was revealed, return: {{"facts": []}}

Return ONLY valid JSON:
  {{"facts": [<fact object>, ...]}}

Examples:
User: "I tweaked my left shoulder benching today"
→ {{"facts": [{{"action": "add", "category": "constraints", "text": "left shoulder tweaks on heavy bench", "replaces_text": null, "safety_critical": true}}]}}

User: "yeah I switched gyms — using the RSF now instead of my apartment gym"
(existing: "trains at apartment gym")
→ {{"facts": [{{"action": "update", "category": "identity", "text": "trains at the RSF", "replaces_text": "trains at apartment gym", "replaces_text_match_required": true, "safety_critical": false}}]}}

User: "Tuesdays are crushing me with classes"
→ {{"facts": [{{"action": "add", "category": "schedule", "text": "Tuesdays are class-heavy", "replaces_text": null, "safety_critical": false}}]}}

User: "see why my gym schedule is all messed up"
→ {{"facts": []}}   (a complaint, not a fact — and "messed up" alone is a fragment)

User: "lol im cs"
→ {{"facts": [{{"action": "add", "category": "identity", "text": "CS major at UC Berkeley", "replaces_text": null, "safety_critical": false}}]}}

Return ONLY valid JSON."""

    try:
        response = client.messages.create(
            model=config.MEMORY_EXTRACTOR_MODEL,
            # 3000: a dense turn legitimately emits 6-8 facts (~600+ tokens with
            # fences); the old 600 cap sat inside that range. Doubled headroom —
            # truncation here discards the whole fact list.
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        track_usage(user_id, "extract_and_store_memory",
                    config.MEMORY_EXTRACTOR_MODEL, response)
        # Gate on stop_reason BEFORE parsing: a cut fact list that still parses
        # would write durable memory from an incomplete output. Discard.
        if response.stop_reason == "max_tokens":
            logger.warning("BG_JOB_TRUNCATED site=extract_and_store_memory user=%s "
                           "max_tokens=%d — discarding, nothing stored", user_id, 3000)
            return None
        # ALL text blocks (Sonnet thinks by default → content[0] may be a
        # ThinkingBlock), then the outermost {...} in case of prose around it.
        from agent_loop import _join_text
        raw = _join_text(response.content).replace("```json", "").replace("```", "").strip()
        if "{" in raw and "}" in raw:
            raw = raw[raw.index("{"):raw.rindex("}") + 1]
        data = json.loads(raw)
        return data.get("facts", []) or []
    except Exception as e:
        logger.error(f"Memory extraction failed for user {user_id}: {e}")
        return None


def extract_and_store_memory(user_id: int, user_message: str, coach_response: str):
    """The STORE half: run the extractor, drop what the deterministic sanitizer
    rejects (fragments, weekday/date mismatches), then apply via
    memory.apply_facts() — action verbs, byte-exact replaces_text, the dedup
    ladder, eviction — and write back atomically under a row lock."""
    from memory import sanitize_facts

    facts = extract_memory_facts(user_id, user_message, coach_response)
    if not facts:
        return
    facts, rejected = sanitize_facts(facts, user_id=user_id)
    if rejected:
        logger.info("MEMORY_SANITIZE user=%s rejected=%d kept=%d", user_id, rejected, len(facts))
    if not facts:
        return

    try:
        session = get_session()
        try:
            # Row-lock the user row so concurrent extractions / future uses-bumps
            # serialize on a single critical section. SELECT ... FOR UPDATE is a
            # no-op on SQLite but real on Postgres (production).
            user = session.query(User).filter(User.id == user_id).with_for_update().one_or_none()
            if not user:
                return

            # De-deixis floor (memory-freshness Fix 2): this extractor runs post-turn
            # with no tools, so a time-bound fact it catches can only become memory
            # text — pin any relative day-words to their resolved dates before the
            # text goes durable. (The live "interview this afternoon" entries came
            # from exactly this path.)
            from timefmt import resolve_deixis
            for f in facts:
                if f.get("text"):
                    f["text"] = resolve_deixis(f["text"], user)
            current_profile = dict(user.user_profile_memory or {})
            new_profile, stats = apply_facts(current_profile, facts, user_id=user_id)
            user.user_profile_memory = new_profile
            # flag_modified is required: SQLAlchemy's JSON tracker treats
            # in-place nested mutations as no-ops. Without this the apply_facts
            # `update` action and the eviction path would silently fail to persist.
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(user, "user_profile_memory")
            session.commit()
            logger.info(
                "MEMORY_EXTRACT user=%s added=%d updated=%d deduped=%d mismatched=%d skipped=%d invalid=%d",
                user.name, stats["added"], stats["updated"], stats["deduped"],
                stats["mismatched"], stats["skipped"], stats["invalid"],
            )
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Memory extraction failed for user {user_id}: {e}")


# ─── Coaching Summarizer (Phase B — watermark-driven incremental) ────────────
#
# Phase B design: eliminate the summary <-> raw-history overlap that the
# original code created by summarizing "all-but-last-8" every 8 turns while
# build_context separately pulled the last 50 raw messages. Messages 9-50
# were double-counted.
#
# New invariant: User.last_compressed_message_id is the watermark. Summary
# owns messages with id <= watermark; raw window owns id > watermark.
# Summarization is incremental — only post-watermark older messages get
# folded into the existing summary, instead of rebuilding wholesale.
#
# Cadence: every 8 messages once we have enough raw history past the
# watermark (>= 16) to keep an 8-message buffer of fresh context unsummarized.

_RAW_BUFFER_BEFORE_COMPRESS = 8  # how many recent messages stay raw, untouched
_MIN_NEW_TO_COMPRESS = 8         # don't run summarizer for <8 new post-watermark messages


def maybe_update_coaching_summary(user_id: int):
    """
    Watermark-driven incremental summarization. Folds NEW post-watermark
    messages into the existing summary; advances the watermark. The raw
    window (built in coach.build_context) starts at the watermark, so
    nothing is double-counted.
    """
    import anthropic

    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return

        watermark = user.last_compressed_message_id or 0

        # Count messages above the watermark — that's the "potentially
        # summarizable" pool. We summarize everything except the most
        # recent _RAW_BUFFER_BEFORE_COMPRESS messages, so the active
        # conversation stays raw.
        new_msgs_count = (
            session.query(Message)
            .filter(Message.user_id == user_id, Message.id > watermark)
            .count()
        )

        # Need at least RAW_BUFFER + MIN_NEW above the watermark to even
        # consider running. Otherwise there isn't a useful chunk to compress.
        if new_msgs_count < (_RAW_BUFFER_BEFORE_COMPRESS + _MIN_NEW_TO_COMPRESS):
            return

        # The cohort to summarize: post-watermark, but EXCLUDE the last
        # _RAW_BUFFER_BEFORE_COMPRESS so they stay in the raw window.
        new_to_summarize_count = new_msgs_count - _RAW_BUFFER_BEFORE_COMPRESS

        cohort = (
            session.query(Message)
            .filter(Message.user_id == user_id, Message.id > watermark)
            .order_by(Message.created_at.asc())
            .limit(new_to_summarize_count)
            .all()
        )
        if not cohort:
            return

        new_watermark = cohort[-1].id  # advance to last summarized msg

        conversation_text = "\n".join(
            f"[{m.created_at.strftime('%b %d %I:%M %p')}] {'Coach' if m.direction == 'out' else user.name}: {m.body}"
            for m in cohort
        )

        existing_summary = user.coaching_summary or "(no prior summary)"
        user_name = user.name
    finally:
        session.close()

    client = make_client()

    prompt = f"""You are maintaining a rolling summary of an SMS coaching relationship. Your job is to FOLD new conversation into the existing summary — not rebuild it from scratch. Keep useful past context, integrate the new material, drop anything stale.

The user's permanent personal details (preferences, stats, life events) are stored separately in a memory system — do NOT duplicate those here. Focus only on the COACHING ARC: what topics were discussed, what decisions were made, what workouts happened, what adjustments were tried, what the user struggled with or succeeded at.

Existing summary:
{existing_summary}

NEW conversation to fold in (everything since the last summary update):
{conversation_text}

Return a structured summary under these headers. Keep it tight — each section 1-4 bullets max. Only include sections that have content.

## Coaching Decisions
(e.g., "Set calories at 2200 for cut", "Decided full body 2x/week over PPL")

## Workouts Completed
(e.g., "Apr 15: First full body session — squat, bench, row", "Apr 17: Upper body — bench hit 155x8")

## Adjustments Made
(e.g., "Dropped lateral raises after shoulder discomfort", "Added 15-min walk to rest days")

## Recent Themes
(e.g., "User has been asking about macros breakdown", "Focus recently on form over weight")

## Open Items
(e.g., "User hasn't confirmed training time yet", "Considering whether to add cardio")

Keep under 400 words total. This REPLACES the prior summary — bring forward whatever from it is still load-bearing."""

    try:
        response = client.messages.create(
            model=config.COACH_MODEL,
            # 3000: the prompt's own ask (≤400 words structured) is ~550-700 tokens
            # WITH headers — the old 600 cap sat INSIDE the asked-for range. The
            # 400-word instruction stays the real length governor; doubled for the
            # Opus tokenizer (~30% more tokens for the same text) + headroom.
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        track_usage(user_id, "maybe_update_coaching_summary",
                    config.COACH_MODEL, response)
        # A partial summary is worse than last cycle's intact one — and advancing
        # the watermark over half-folded messages loses them permanently. Keep
        # both untouched; the next cycle refolds the same cohort (self-healing).
        if response.stop_reason == "max_tokens":
            logger.warning("BG_JOB_TRUNCATED site=maybe_update_coaching_summary user=%s "
                           "max_tokens=%d — keeping prior summary and watermark", user_id, 3000)
            return
        summary = response.content[0].text.strip()

        session = get_session()
        try:
            user = session.get(User, user_id)
            if user:
                user.coaching_summary = summary
                user.last_compressed_message_id = new_watermark
                session.commit()
                logger.info(
                    "COACHING_SUMMARY_UPDATE user=%s folded=%d new watermark=%d",
                    user_name, len(cohort), new_watermark,
                )
        finally:
            session.close()
    except Exception as e:
        logger.error(f"Summary generation failed for user {user_id}: {e}")


# ─── Buffered Message Processor ─────────────────────
def _react_to_latest_inbound(user_id: int, emoji: str) -> None:
    """Best-effort tapback on the user's newest iMessage (their PR text)."""
    try:
        from sms import react_to_message
        session = get_session()
        try:
            m = (session.query(Message.provider_sid)
                 .filter(Message.user_id == user_id, Message.direction == "in", Message.channel == "imessage",
                         Message.provider_sid.isnot(None)).order_by(Message.id.desc()).first())
        finally:
            session.close()
        if m and m[0]:
            react_to_message(user_id, m[0], emoji)
    except Exception as e:  # noqa: BLE001
        logger.info("REACT_LATEST_SKIPPED user=%s err=%s", user_id, e)


def process_buffered_message(user_id: int, combined_body: str, message_type: str, image_url: dict = None):
    """Called by the message buffer after the delay expires. Processes the combined message and sends a response."""
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).first()
        if not user:
            return
        # Release the connection NOW. Everything below is the model turn (seconds to a
        # minute; a hung call, minutes) and it must not run inside an open transaction:
        # on 2026-09-11 a connection left "idle in transaction" through a turn held a
        # users lock that a deploy's ALTER TABLE queued behind — and every users read
        # queued behind the ALTER. `user` stays usable detached (columns are loaded; the
        # turn reads by user.id through its own short sessions).
        session.close()

        # "Read" then "Cued is typing…" — the buffer (reading) is over; generation
        # starts now. Receipt first so the stamp lands before the dots.
        from read_receipts import mark_read
        from typing_indicator import typing_start, typing_stop
        mark_read(user.id)
        typing_start(user.id)

        # iMessage-first signup: their first text IS the channel choice. The hook
        # was deferred at signup; send it now as the reply (it routes blue — the
        # breaker was never tripped) and skip the model. Their "hey cued" is already
        # logged as the inbound; the friend turn starts on their next message.
        from onboarding_agent import awaiting_channel_choice, send_onboarding_hook
        if awaiting_channel_choice(user):
            logger.info("ONBOARDING_HOOK_ON_FIRST_TEXT user=%s", user.id)
            send_onboarding_hook(user.id, reason="first_text")
            typing_stop(user.id)
            return

        # Series §1.4: pantry text ('out of chicken', 'what do i have') is answered
        # in code — one line, no model turn. Flag-gated inside handle_pantry_text.
        if (user.onboarding_step or 0) >= 3 and not image_url:
            try:
                from receipts import handle_pantry_text
                pline = handle_pantry_text(user.id, combined_body)
            except Exception as e:  # noqa: BLE001
                logger.error("PANTRY_TEXT_PATH_FAILED user=%s err=%s", user.id, e, exc_info=True)
                pline = None
            if pline:
                send_sms(user.phone, pline, user_id=user.id, message_type="pantry")
                typing_stop(user.id)
                return

        # Series §2.7: opt-in replies / 'handle the line for me' / 'not going' — in code.
        if (user.onboarding_step or 0) >= 3 and not image_url:
            try:
                from gym_beats import handle_text as _gym_text
                gline = _gym_text(user.id, combined_body)
            except Exception as e:  # noqa: BLE001
                logger.error("GYM_TEXT_PATH_FAILED user=%s err=%s", user.id, e, exc_info=True)
                gline = None
            if gline:
                send_sms(user.phone, gline, user_id=user.id, message_type="gym_reply")
                if gline.startswith("bet — i'll handle it from now on"):
                    from gym_beats import propose as _gym_propose
                    s2 = get_session()
                    try:
                        u2 = s2.get(User, user.id)
                        beat = _gym_propose(u2, s2)
                    finally:
                        s2.close()
                    if beat and beat.kind in ("line_d2", "line_d1"):
                        send_sms(user.phone, beat.text, user_id=user.id, message_type=beat.message_type)
                typing_stop(user.id)
                return

        # Series §2.8: 'heading to the gym' while the line's on → the join link (or the
        # queue, if opted in) in code, before any model turn. Meter-gated, not beats-gated.
        if (user.onboarding_step or 0) >= 3 and not image_url:
            try:
                from gym_beats import heading_out as _heading_out
                hbeat = _heading_out(user.id, combined_body)
            except Exception as e:  # noqa: BLE001
                logger.error("GYM_HEADING_OUT_PATH_FAILED user=%s err=%s", user.id, e, exc_info=True)
                hbeat = None
            if hbeat:
                send_sms(user.phone, hbeat.text, user_id=user.id, message_type=hbeat.message_type)
                logger.info("GYM_HEADING_OUT user=%s kind=%s", user.id, hbeat.kind)
                typing_stop(user.id)
                return

        # Workout card, Phase 4: with a session open, a terse set ('190 x4',
        # 'only got 3', 'skipped incline') or a close ('done') is handled in code —
        # exactly one line back ('swapped it in.' / the PR line), never a model turn.
        if (user.onboarding_step or 0) >= 3:
            try:
                from workouts.session_ops import apply_text_update
                line = apply_text_update(user.id, combined_body)
            except Exception as e:  # noqa: BLE001 — never let the card path break a turn
                logger.error("WORKOUT_TEXT_PATH_FAILED user=%s err=%s", user.id, e, exc_info=True)
                line = None
            if line is not None:
                if line:
                    send_sms(user.phone, line, user_id=user.id, message_type="workout_set")
                    if "PR" in line:
                        _react_to_latest_inbound(user.id, "emphasize")   # ‼️ = the hype tapback
                typing_stop(user.id)
                return

        # If user is still in onboarding, route to onboarding handler
        if (user.onboarding_step or 0) < 3:
            handle_onboarding_reply(user, combined_body)
            # Onboarding turns are FULL of durable life facts (their classes, where
            # they eat, gear, year) and until 2026-09-11 none of it reached memory —
            # this branch returned before the post-reply extraction below. Run the
            # same memory extraction normal turns get (background, best-effort).
            from onboarding_agent import _last_coach_message
            reply_text = _last_coach_message(user.id) or ""
            threading.Thread(
                target=extract_and_store_memory,
                args=(user.id, combined_body, reply_text),
                daemon=True,
            ).start()
            return

        # Phase 2: single agent loop behind a flag. On ANY runtime failure, fall
        # back to the legacy classifier->specialists pipeline and log loudly — the
        # user never sees a gap (invariant #5). Legacy stays live until Phase 6.
        response_text = None
        if config.SINGLE_AGENT_LOOP_ENABLED:
            try:
                response_text = run_agent_loop(user, combined_body, message_type, image_data=image_url)
            except Exception as e:
                logger.error("AGENT_LOOP_FALLBACK user=%s falling back to legacy: %s",
                             user.id, e, exc_info=True)
                response_text = None
        if response_text is None:
            from orchestrator import route_message
            response_text = route_message(user, combined_body, message_type, image_data=image_url)

        # Send the response — threaded on the message the coach chose, if any. A
        # reaction-only turn returns "" : the tapback was the reply, send nothing
        # and clear the typing bubble (no text is coming). Belt to the loop's
        # suspenders: the literal sentinel must never reach a phone.
        from agent_tools import pop_turn_state, REACTION_ONLY_SENTINEL
        if (response_text or "").strip().lower() == REACTION_ONLY_SENTINEL:
            logger.info("SENTINEL_SWALLOWED_AT_SEND user=%s", user.id)
            response_text = ""
        turn = pop_turn_state(user.id)
        if response_text:
            send_sms(user.phone, response_text, user_id=user.id, message_type=message_type,
                     reply_to_sid=turn.get("reply_to"))
        else:
            logger.info("REACTION_ONLY_TURN user=%s — no text sent", user.id)
            typing_stop(user.id)

        # Detect end-of-workout signals and clear session state
        end_signals = ["done", "finished", "that's it", "thats it", "heading out", "heading home", "leaving gym", "left the gym"]
        if any(sig in combined_body.lower() for sig in end_signals):
            clear_session_state(user.id)

        # Extract and store any confirmed decisions (runs in background, doesn't block)
        threading.Thread(
            target=extract_and_store_decisions,
            args=(user.id, combined_body, response_text),
            daemon=True,
        ).start()

        # A5 safety pre-pass: now runs SYNCHRONOUSLY at the top of the webhook
        # (see Fix 5 comment there). The previous daemon spawn here was
        # bypassed by every webhook early-return path (goodnight, ack-
        # suppression, logging-mode intercept), which silently dropped
        # injuries reported alongside any of those signals. Removed.

        # Extract and store user memory (runs in background, doesn't block)
        threading.Thread(
            target=extract_and_store_memory,
            args=(user.id, combined_body, response_text),
            daemon=True,
        ).start()

        # A6: tag substantive coaching points delivered this turn so the
        # coach can reference them rather than re-deliver them next time.
        threading.Thread(
            target=extract_and_store_coaching_points_task,
            args=(user.id, combined_body, response_text),
            daemon=True,
        ).start()

        # A4: async uses-bump for memory entries that were rendered into the
        # agent's context this turn. We don't know which agent the orchestrator
        # routed to without surfacing it back up, so we collect ids for every
        # agent map and dedupe — bump_uses is keyed by id, so duplicates are
        # free. Pure-Python (no DB) so this is cheap to compute synchronously
        # before queueing the actual DB write to the daemon thread.
        try:
            injected_ids = set()
            for _atype in ("nutrition", "training", "readiness", "coach"):
                _, _ids = build_memory_block_with_ids(user, _atype)
                injected_ids.update(_ids)
            if injected_ids:
                threading.Thread(
                    target=update_memory_uses_task,
                    args=(user.id, list(injected_ids)),
                    daemon=True,
                ).start()
        except Exception as _e:
            logger.warning(f"memory uses-bump queueing failed for user {user.id}: {_e}")

        # Update coaching summary periodically (runs in background, doesn't block)
        threading.Thread(
            target=maybe_update_coaching_summary,
            args=(user.id,),
            daemon=True,
        ).start()

    except Exception as e:
        logger.error(f"Error processing buffered message for user {user_id}: {e}", exc_info=True)
        try:
            from typing_indicator import typing_stop as _typing_stop
            _typing_stop(user_id)  # no reply is coming — don't leave the bubble up
        except Exception:  # noqa: BLE001
            pass
        try:
            s2 = get_session()  # the outer session is already closed by design
            try:
                u = s2.query(User).filter(User.id == user_id).first()
                phone = u.phone if u else None
            finally:
                s2.close()
            if phone:
                send_sms(phone, "Something went wrong on my end — I'll be back shortly.", user_id=user_id)
        except Exception:
            pass
    finally:
        session.close()  # no-op when already closed


# ─── Goodnight Detection ─────────────────────────────
def _is_training_day_confirmation(body: str) -> bool:
    """
    Detect when a user is confirming they're training today.
    Intentionally tight — false negatives (missing a confirmation and
    skipping the pre-workout nudge) are much better than false positives
    (flagging a rest day and nagging someone who's not training).

    Rules:
    - Explicit negation anywhere → always False
    - Must match a specific multi-word phrase or clear gym/lift keyword
    - Ambiguous replies ("maybe", "depends", "probably") → False
    """
    body_lower = body.lower().strip()

    # Negation check — bail immediately if any negative marker present
    negations = ["nah", "nope", "no ", "not ", "rest day", "rest today",
                 "skipping", "skip", "off day", "taking the day", "taking a day",
                 "maybe", "depends", "probably not", "might not", "idk", "not sure"]
    if any(neg in body_lower for neg in negations):
        return False

    # Positive signals — must be specific enough to rule out casual use
    training_signals = [
        "going to gym", "going to the gym", "heading to gym", "heading to the gym",
        "training today", "working out today", "gym today", "yeah gym", "yep gym",
        "lifting today", "hitting the gym", "hitting legs", "hitting chest",
        "hitting back", "hitting arms", "hitting shoulders",
        "legs today", "chest today", "back today", "arms today", "shoulders today",
        "push today", "pull today", "upper today", "lower today",
        "got a workout", "doing a workout", "getting a workout in",
        "yeah training", "yep training", "yeah working out", "yep working out",
    ]
    return any(signal in body_lower for signal in training_signals)


# Fix 1: closing-acknowledgment suppression. Token-based detection so the
# whole message must be ack-only ("ok" / "alr bet") — never just "ack-leading"
# (which would falsely suppress "ok can you help"). Suppression only fires
# post-onboarding, when the coach's last outbound has no open question, and
# when there's been a recent outbound at all (avoids suppressing first
# contact after a long quiet window).
_ACK_TOKENS = frozenset({
    "ok", "okay", "k", "kk", "alr", "aight", "ight", "bet", "word",
    "cool", "coolcool", "got", "gotit", "gotchu", "gotcha", "fasho",
    "facts", "yup", "yep", "yessir", "sg", "alright", "ty", "thanks",
    "damn", "💪", "👍", "🙏",
})
_ACK_PHRASES = frozenset({
    "got it", "sounds good", "will do", "ok cool", "ok bet",
    "thank you", "appreciate it", "ok thanks", "alr thanks",
    # "ig" / "i guess" are deliberately NOT here (founder, 2026-09-13: "ig is not
    # always a conversation ender"). They go to the model, which voice.md tells not
    # to re-deliver its last message — the fix for the live "Ig" pep-talk repeat
    # lives at the prompt layer, not in this list.
})


def is_closing_acknowledgment(body: str) -> bool:
    """True when the body is *exclusively* ack tokens. Token-based, not
    prefix-based — 'ok can you help' must NOT suppress."""
    b = body.lower().strip().rstrip(".!?").strip()
    if not b:
        return False
    if b in _ACK_PHRASES:
        return True
    tokens = b.split()
    if not tokens:
        return False
    return all(t.rstrip(".!?,") in _ACK_TOKENS for t in tokens)


def should_suppress_ack(user_id: int, body: str) -> bool:
    """Full Fix 1 condition. Returns True if the inbound is a pure ack AND
    there's no open question to answer.

    Suppression conditions (ALL must hold):
      1. is_closing_acknowledgment(body)
      2. No question (?) in coach's most recent outbound sent in last 30 min
      3. There WAS at least one outbound in the last 12 hours (avoid
         suppressing first contact after a long quiet window)
    """
    if not is_closing_acknowledgment(body):
        return False

    from datetime import datetime, timezone as _tz, timedelta
    s = get_session()
    try:
        last_out = (
            s.query(Message)
            .filter(Message.user_id == user_id, Message.direction == "out")
            .order_by(Message.created_at.desc())
            .first()
        )
        if not last_out or not last_out.created_at:
            # No prior outbound at all — let the message through (cold start).
            return False
        last_out_ts = last_out.created_at
        if last_out_ts.tzinfo is None:
            last_out_ts = last_out_ts.replace(tzinfo=_tz.utc)
        now = datetime.now(_tz.utc)
        age = now - last_out_ts
        # No outbound in last 12h → treat ack as a conversation opener.
        if age > timedelta(hours=12):
            return False
        # Last outbound within 30 minutes AND contains a '?' anywhere
        # → ack is answering an open question; let it through.
        if age <= timedelta(minutes=30) and "?" in (last_out.body or ""):
            return False
        return True
    finally:
        s.close()


# ─── Part B: workout logging mode ─────────────────────────────────────────
# State machine that puts the coach in silent set-by-set logging mode until
# the user exits. Mode state = {"status": "workout_logging", "started_at": ISO,
# "workout_id": <int>}. The in-progress Workout row has completed=False and
# its exercises JSON gets appended-to per set. On exit, completed=True is set
# and (per default config) only a silent stats line is sent. See plan Part B.

_WORKOUT_LOG_ENTRY_PHRASES = frozenset({
    "workout logging mode", "log mode", "logging mode",
    "start logging", "start workout log",
})

_WORKOUT_LOG_EXIT_TOKENS = ("done", "finished", "that's it", "thats it",
                            "end", "exit", "stop", "wrapping up",
                            "heading out", "leaving gym", "left the gym")

# Lifting vocabulary for the regex pre-pass that gates Haiku.
_LIFT_KEYWORD_RE = re.compile(
    r"\b(bench|squat|deadlift|press|row|curl|dip|pull|push|ohp|rdl|"
    r"chin|lunge|raise|fly|extension|shrug|hip|leg|chest|back|shoulder|"
    r"clean|snatch|jerk|thrust|bridge|crunch|plank|abs)\b",
    re.IGNORECASE,
)

_SET_MARKER_RE = re.compile(
    r"[x×@]|\b(sets?|reps?|lbs?|kg)\b", re.IGNORECASE,
)

_NUMERIC_RE = re.compile(r"\d")


def is_workout_log_entry(body: str) -> bool:
    """Detect the explicit mode-enter trigger. Matches exact phrases like
    'workout logging mode' / 'log mode' / 'start logging', stripped of
    trailing punctuation."""
    b = body.lower().strip().rstrip(":.!?").strip()
    return b in _WORKOUT_LOG_ENTRY_PHRASES


def _looks_like_set_data(body: str) -> bool:
    """Regex pre-pass: numeric tokens + (lift keyword OR set marker x/×/@).
    Gates the Haiku call so we don't waste a call on chitchat AND we don't
    risk Haiku confabulating exercises from non-set messages."""
    if not _NUMERIC_RE.search(body):
        return False
    return bool(_LIFT_KEYWORD_RE.search(body) or _SET_MARKER_RE.search(body))


def _is_workout_log_exit(body: str) -> bool:
    """True if the message looks like an explicit exit signal."""
    bl = body.lower().strip()
    return any(sig in bl for sig in _WORKOUT_LOG_EXIT_TOKENS)


def _logging_session_stale(state: dict) -> bool:
    """True if the in-progress session is older than the timeout or from
    a previous calendar day. Note: get_session_state already has a midnight
    auto-clear (models.py:465), so this is redundant for the day-rollover
    case but defensive."""
    from datetime import datetime, timezone, timedelta
    started_raw = state.get("started_at")
    if not started_raw:
        return True
    try:
        started = datetime.fromisoformat(started_raw)
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if now - started > timedelta(hours=config.WORKOUT_LOG_TIMEOUT_HOURS):
        return True
    if started.date() != now.date():
        return True
    return False


def _create_in_progress_workout(user_id: int) -> int:
    """Create a fresh in-progress Workout row and return its id."""
    from datetime import datetime, timezone
    s = get_session()
    try:
        w = Workout(
            user_id=user_id,
            date=datetime.now(timezone.utc),
            workout_type="logged",
            exercises=[],
            completed=False,
        )
        s.add(w)
        s.commit()
        return w.id
    finally:
        s.close()


def _append_sets_to_workout(workout_id: int, new_exercises: list) -> None:
    """Append parsed exercises to the in-progress Workout's exercises JSON.
    Uses flag_modified per Phase A pattern — SQLAlchemy's JSON column tracker
    treats nested mutations as no-ops without it, so the append would silently
    not persist."""
    from sqlalchemy.orm.attributes import flag_modified
    s = get_session()
    try:
        w = s.query(Workout).filter(Workout.id == workout_id).with_for_update().one_or_none()
        if not w:
            return
        existing = list(w.exercises or [])
        existing.extend(new_exercises)
        w.exercises = existing
        flag_modified(w, "exercises")
        s.commit()
    finally:
        s.close()


def _short_render_sets(parsed_exercises: list) -> str:
    """Render parsed exercises as a brief ack. With WORKOUT_LOG_ACK_VERBOSE
    false (default per plan: Twilio rate concern), returns just '✓' — single
    byte, avoids the 1-msg/sec soft rate limit on rapid-fire set logging."""
    if not config.WORKOUT_LOG_ACK_VERBOSE:
        return "✓"
    # Verbose mode: show the first exercise's parse for confirmation.
    parts = []
    for ex in parsed_exercises[:1]:
        name = ex.get("name", "?")
        sets = ex.get("sets", 1)
        reps = ex.get("reps", "?")
        wt = ex.get("weight")
        if sets and sets > 1:
            parts.append(f"{name} {sets}x{reps}" + (f"@{wt}" if wt else ""))
        else:
            parts.append(f"{name} {reps}" + (f"@{wt}" if wt else ""))
    suffix = f" +{len(parsed_exercises)-1} more" if len(parsed_exercises) > 1 else ""
    return f"✓ {parts[0]}{suffix}" if parts else "✓"


def _finalize_workout_log(user, state: dict, reason: str) -> None:
    """Mark the in-progress Workout completed, clear session_state, and send
    the exit message per config.WORKOUT_LOG_EXIT_SUMMARY.

    Per default ('silent'): send a stats line "✓ saved. N lifts, M sets."
    Stale finalize never sends a summary (user didn't ask for one)."""
    from models import confirm_workout_today
    s = get_session()
    n_ex = 0
    n_sets = 0
    try:
        w = s.query(Workout).filter(Workout.id == state.get("workout_id")).one_or_none()
        if w:
            w.completed = True
            exs = list(w.exercises or [])
            n_ex = len(exs)
            n_sets = sum(int(e.get("sets") or 0) for e in exs) or n_ex
            w.ai_notes = f"Logged via workout_logging mode ({reason}). {n_ex} lifts, {n_sets} sets."
            s.commit()
    finally:
        s.close()
    confirm_workout_today(user.id)
    clear_session_state(user.id)

    if reason == "stale":
        # Stale finalize is silent — the user didn't ask for an exit summary.
        return

    mode = config.WORKOUT_LOG_EXIT_SUMMARY
    if mode == "silent":
        stats = f"✓ saved. {n_ex} lifts, {n_sets} sets." if n_ex else "✓ saved."
        send_sms(user.phone, stats, user_id=user.id, message_type="workout_log_summary")
    elif mode == "brief":
        # Brief mode: stats line + one personality riff. Personality riff is
        # an opt-in Sonnet call — deferred per plan (silent default ships first).
        stats = f"✓ saved. {n_ex} lifts, {n_sets} sets." if n_ex else "✓ saved."
        send_sms(user.phone, stats, user_id=user.id, message_type="workout_log_summary")
    else:  # "full" — hand to the coach for normal commentary
        # Deferred per plan. Falls back to silent.
        stats = f"✓ saved. {n_ex} lifts, {n_sets} sets." if n_ex else "✓ saved."
        send_sms(user.phone, stats, user_id=user.id, message_type="workout_log_summary")


def _handle_logging_mode_message(user, body: str, state: dict) -> bool:
    """Mode intercept. Returns True if the message was handled (caller skips
    coaching + buffer). Always handles the message in some way — never falls
    through to normal flow, because falling through risks creating an orphan
    one-row Workout via the webhook-level workout_log one-shot path.

    Stale path: silently finalize the stale row, open a fresh in-progress
    row, then handle the current message as the start of the new session
    (NOT as fall-through, per plan note on orphan rows)."""

    # Stale-session guard FIRST. Finalize the old session and start fresh
    # WITHOUT returning False — see plan: returning False would let the
    # message fall through to the webhook one-shot path which would create
    # an orphan single-set completed Workout. Instead we open a fresh
    # in-progress workout and treat this message as set 1.
    if _logging_session_stale(state):
        _finalize_workout_log(user, state, reason="stale")
        new_workout_id = _create_in_progress_workout(user.id)
        set_session_state(user.id, "workout_logging", workout_id=new_workout_id)
        # Refresh state so the rest of this call uses the new workout_id.
        state = get_session_state(user.id) or {}

    # Explicit exit
    if _is_workout_log_exit(body):
        _finalize_workout_log(user, state, reason="explicit")
        return True

    # Regex pre-pass: gate Haiku to prevent confabulation on chitchat
    if not _looks_like_set_data(body):
        send_sms(user.phone, "didn't catch sets. send the lift or text 'done' to wrap.",
                 user_id=user.id, message_type="workout_log_clarify")
        return True

    parsed = parse_workout_log(user, body, model=config.HAIKU_MODEL)
    if not parsed or not parsed.get("exercises"):
        send_sms(user.phone, "didn't catch sets. send the lift or text 'done' to wrap.",
                 user_id=user.id, message_type="workout_log_clarify")
        return True

    _append_sets_to_workout(state["workout_id"], parsed["exercises"])
    ack = _short_render_sets(parsed["exercises"])
    send_sms(user.phone, ack, user_id=user.id, message_type="workout_log_ack")
    return True


# ─── Waitlist endpoint helpers ─────────────────────────────────────────────
def _normalize_phone(raw: str, strict: bool = False) -> str:
    """Normalize a US phone to E.164 (+1XXXXXXXXXX).

    strict=True (used by /waitlist): rejects anything that isn't 10 digits
    (after stripping non-digits), 11 digits starting with 1, or already
    properly formatted +1XXXXXXXXXX / +XX...

    strict=False (legacy behavior of /signup and /activate-sms): permissively
    auto-prepends +1 to whatever digits are left. Has a known bug where it
    will mangle international numbers if the user accidentally deletes the +,
    but fixing it touches the chat overlay flow and is out of scope for the
    waitlist work.
    """
    if not raw:
        raise ValueError("Phone number is required.")
    raw = raw.strip()
    if raw.startswith("+"):
        digits = re.sub(r"\D", "", raw)
        if 11 <= len(digits) <= 15:
            return "+" + digits
        if strict:
            raise ValueError("Please enter a valid phone number.")
        return raw
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if strict:
        raise ValueError("Please enter a 10-digit US phone number.")
    return "+1" + digits


def _waitlist_channel_state(u) -> tuple:
    """(label, badge class) for the admin waitlist tab — a code-owned reading of the
    channel columns: opted in > chose SMS > link sent (provisioned, not yet texted)
    > never provisioned. Tells the admin whether Activate will go blue."""
    if u.imessage_opted_in_at:
        return ("iMessage ✓", "badge-green")
    if u.photon_user_id and (u.preferred_channel or "sms") == "sms":
        return ("SMS", "badge-gray")
    if u.photon_user_id:
        return ("link sent", "badge-blue")
    return ("—", "badge-gray")


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

def _looks_like_email(s: str) -> bool:
    """Cheap email format check. Not RFC-perfect; just rejects obvious junk."""
    return bool(s) and len(s) <= 200 and bool(_EMAIL_RE.match(s.strip()))


# Explicit sign-offs: count at any hour. Implicit ones ("night", "gn", "bye",
# "ttyl", "peace out"): only in the evening window — "night" alone at 3pm is
# never a goodnight. Live 2026-09-12 15:43 PDT: "That was last nights dinner"
# matched the substring "night" and the coach sent "Get some rest. Hit me up in
# the morning." to someone who had just woken up (and set quiet_until to the
# next wake time — a mute). Word boundaries + exclusions + the hour gate fix it.
_GOODNIGHT_EXPLICIT = (
    "goodnight", "good night", "going to sleep", "going to bed", "gonna sleep",
    "gonna go to bed", "gts", "ima sleep", "ima gts", "ima go to bed",
    "heading to bed", "off to bed", "crashing now", "night night", "nighty night",
)
_GOODNIGHT_IMPLICIT = ("night", "gn", "ttyt", "talk tomorrow", "ttyl", "bye", "byw", "peace out")
_GOODNIGHT_NOT = ("last night", "tonight", "nights", "night's", "night before", "other night",
                  "all night", "every night", "night shift", "night class", "late night", "night out")
_GOODNIGHT_EVENING = (20, 5)  # implicit forms count only from 8pm to 5am local


def is_goodnight_signal(body: str, *, local_hour: int = None) -> bool:
    """Detect if the user is signaling end-of-conversation. Whole-word matches
    only, never inside "last night" / "tonight" / "nights". Implicit forms need
    the evening window when `local_hour` is given (the webhook passes it)."""
    import re as _re
    body_lower = (body or "").lower().strip().rstrip(".!")
    if len(body_lower) >= 40:
        return False
    if any(ex in body_lower for ex in _GOODNIGHT_NOT):
        return False
    def _has(phrase):
        return _re.search(r"(?<![a-z])" + _re.escape(phrase) + r"(?![a-z])", body_lower) is not None
    if any(_has(p) for p in _GOODNIGHT_EXPLICIT):
        return True
    if any(_has(p) for p in _GOODNIGHT_IMPLICIT):
        if local_hour is None:
            return True
        start, end = _GOODNIGHT_EVENING
        return local_hour >= start or local_hour < end
    return False


# ─── Twilio Webhook (incoming SMS) ──────────────────
def _process_inbound(session, user, from_number, body, message_sid, image_url, image_data,
                     channel="sms", provider_sid=None):
    """The inbound pipeline, from the idempotency claim through the buffer arm.

    Extracted verbatim from the Twilio webhook (Photon migration Phase 4B) so
    the iMessage door (/internal/inbound) dispatches into the SAME code — one
    pipeline, two transports. `user` is already resolved (each route handles the
    unknown-sender case its own way); `from_number` is the buffer key (the
    user's E.164 phone on both channels); `message_sid` is the idempotency key
    (Twilio MessageSid or Photon message id); `image_url` is the has-image
    signal, `image_data` the base64 block the model sees. Returns the TwiML
    tuple the webhook needs; the iMessage route ignores it."""
    # Webhook idempotency (claim-at-top): a Twilio retry of the SAME MessageSid
    # — which a slow synchronous classify_message can trigger by blowing the
    # ~15s webhook timeout — is deduped here before any state write, so we don't
    # double-log the message / double-write meals. Fail-open: a missing sid or
    # an unexpected claim error falls through and processes. The claim is
    # RELEASED in the except handler so a crash-after-claim can be retried.
    if not claim_message_sid(message_sid, user.id):
        logger.info("WEBHOOK_DUPLICATE sid=%s user=%s", message_sid, user.name)
        return get_twiml_response(), 200, {"Content-Type": "text/xml"}

    # Photon migration: breaker reset. An iMessage arriving FROM this user is
    # proof the pipe works for them — clear channel_failed_over so the next
    # outbound goes blue again. No timers, no polling; the user's own message
    # is the health check. A Twilio inbound proves nothing about iMessage and
    # never clears it. preferred_channel (the ask) is untouched.
    if channel == "imessage" and user.channel_failed_over:
        user.channel_failed_over = False
        user.channel_failover_at = None
        session.commit()
        logger.info("IMESSAGE_BREAKER_RESET user_id=%s — inbound iMessage proves the pipe", user.id)

    # Their first inbound iMessage is the proof the shared-pool consent gate is open
    # for this number. Stamped once, in code; the admin waitlist tab reads it and a
    # waitlister's activation relies on it (blue first try, no link).
    if channel == "imessage" and not user.imessage_opted_in_at:
        from datetime import datetime as _dt_in, timezone as _tz_in  # `datetime` is rebound below
        user.imessage_opted_in_at = _dt_in.now(_tz_in.utc).replace(tzinfo=None)
        session.commit()
        logger.info("IMESSAGE_OPTED_IN user_id=%s", user.id)

    # Clear quiet_until if it's passed or if user is texting us
    # quiet_until is stored as naive UTC
    if user.quiet_until:
        from datetime import datetime as _dt, timezone as _tz_clear
        if _dt.now(_tz_clear.utc).replace(tzinfo=None) >= user.quiet_until or body.strip():
            user.quiet_until = None
            session.commit()

    # Log the incoming message immediately. has_image mirrors classify_message's
    # presence signal: the stored row must record that media arrived (the base64
    # never persists), or the window can't distinguish "no image ever came" from
    # "image came, detail not saved" — the tenders-confabulation gap.
    log_incoming(user.id, body, has_image=image_url is not None,
                     channel=channel, provider_sid=provider_sid)

    # Waitlist gate (2026-09-19): a pending user who texts the line — the "hey cued"
    # opt-in tap from the site's success screen, or a curious SMS — is held HERE, in
    # code. One holding line, then silence until the admin activates them: no safety
    # pass, no buffer, no model, and never the hook (awaiting_channel_choice and
    # send_fallback_hooks refuse pending users too). Their inbound is logged above and
    # the opt-in stamp is already written, so activation goes blue first try.
    if user.waitlist_status == "pending":
        from models import Message as _Msg
        from onboarding_agent import waitlist_hold_text
        held_before = (session.query(_Msg.id)
                       .filter(_Msg.user_id == user.id, _Msg.direction == "out",
                               _Msg.message_type == "waitlist_hold").first() is not None)
        if not held_before:
            send_sms(user.phone, waitlist_hold_text(user.name), user_id=user.id, message_type="waitlist_hold")
        logger.info("WAITLIST_INBOUND_HELD user=%s channel=%s replied=%s", user.id, channel, not held_before)
        return get_twiml_response(), 200, {"Content-Type": "text/xml"}

    # STOP opt-out flow (deterministic, flag-gated). Exact "STOP."/"UNSUBSCRIBE." (caps +
    # period) → a confirmation; a second "STOP." opts out; "pause" pauses; ANY inbound
    # resumes an opted-out user. Terminal (no further processing) when it sent a
    # confirmation, opted them out, or paused; otherwise falls through to the coach.
    from optout import handle_optout_flow
    if handle_optout_flow(session, user, body, message_id=message_sid, channel=channel):
        return get_twiml_response(), 200, {"Content-Type": "text/xml"}

    # Fix 5: safety pre-pass runs SYNCHRONOUSLY at the top of the webhook,
    # BEFORE any branch (goodnight, ack-suppression, logging mode, classify,
    # buffer). Closes a pre-existing prod hole where goodnight messages
    # bypassed safety extraction entirely (the daemon spawn in
    # process_buffered_message never fired because goodnight returns
    # before buffer_message). Safe to inline because the task is regex-
    # only (no LLM), idempotent, and dedup-safe per its own docstring.
    # Runs per raw message, before buffer combination — more robust
    # than running on the combined blob.
    apply_safety_signals_task(user.id, body)

    # Fix 3 event floor: synchronous, deterministic detection of the two
    # nudge-critical statuses (went_to_gym, in_class) — same floor pattern as
    # the safety pre-pass, and for the same reason: the failure being fixed is
    # "I *just* said it", so the scheduler gates must be able to read it
    # immediately, without waiting on the buffer or an LLM. Regex only.
    apply_event_signals_task(user.id, body)

    # Reset engagement decay counter on any reply
    reset_unanswered(user.id)

    # Update mirroring style
    maybe_update_style(user.id)

    # Resolve pending clarification
    resolve_pending_clarification(user.id, body)

    # Fix 1: closing-acknowledgment suppression. Post-onboarding, when
    # the user replies with a pure ack ("ok", "alr bet") AND the coach's
    # most recent outbound has no open '?', end the turn silently —
    # log_incoming + reset_unanswered already ran upstream; the safety
    # pre-pass (Fix 5) already ran above. We just need to cancel the
    # buffer and return empty TwiML. This breaks the wind-down loop
    # and skips a Sonnet call.
    if (user.onboarding_step or 0) >= 3 and should_suppress_ack(user.id, body):
        from message_buffer import cancel_buffer
        cancel_buffer(from_number)
        # Rule 1 of reactions, deterministically: a standalone "ok" never reaches the
        # model (this branch drops it before the buffer — live 2026-09-11 the founder's
        # bare "Ok" got nothing), and the only honest reply to a closing ack IS a 👍.
        # No model call; the sidecar resolves message_sid (the Photon id). Never a
        # strike (reaction rows are excluded from every silence gate). SMS: silent, as before.
        reacted = False
        if channel == "imessage" and message_sid:
            try:
                from read_receipts import mark_read
                mark_read(user.id)  # a 👍 without "Read" would look odd
            except Exception:  # noqa: BLE001
                pass
        if config.IMESSAGE_REACTIONS_ENABLED and channel == "imessage" and message_sid:
            try:
                from sms import react_to_message
                reacted = react_to_message(user.id, message_sid, "like")
            except Exception as e:  # noqa: BLE001 — a tapback must never break the webhook
                logger.warning("ACK_REACTION_FAILED user=%s err=%s", user.id, e)
        logger.info(f"Fix 1: suppressed closing ack '{body!r}' for {user.name}"
                    + (" — 👍 tapback sent" if reacted else ""))
        return get_twiml_response(), 200, {"Content-Type": "text/xml"}

    # Classify the message
    message_type = classify_message(body, has_image=image_url is not None)

    # Part B: workout logging mode entry trigger ("workout logging mode" / "log mode" / etc).
    # Bypass buffer, create the in-progress Workout row, set session_state, reply terse.
    if message_type == "workout_log_start" and config.WORKOUT_LOGGING_ENABLED \
            and (user.onboarding_step or 0) >= 3:
        new_workout_id = _create_in_progress_workout(user.id)
        set_session_state(user.id, "workout_logging", workout_id=new_workout_id)
        send_sms(
            user.phone,
            "📋 logging mode on. text your sets — say 'done' when you're finished.",
            user_id=user.id,
            message_type="workout_log_start",
        )
        from message_buffer import cancel_buffer as _cb_start
        _cb_start(from_number)
        logger.info(f"Part B: workout logging mode entered for {user.name} (workout_id={new_workout_id})")
        return get_twiml_response(), 200, {"Content-Type": "text/xml"}

    # Part B: Gate the existing one-shot workout_log row creation and the
    # *_state(at_gym) writes when the user is already in logging mode —
    # otherwise we'd create a duplicate completed Workout (the in-progress
    # row gets the set appended by the mode intercept below), AND we'd
    # clobber workout_logging state with at_gym.
    _pb_state = get_session_state(user.id) if config.WORKOUT_LOGGING_ENABLED else None
    _pb_in_mode = bool(_pb_state and _pb_state.get("status") == "workout_logging")
    # Onboarding users get NO training-state writes from the keyword classifier. Live
    # 2026-09-22 (user 42): a pasted six-day PPL routine matched the lift keywords at
    # 3:46am — workout_confirmed on the day + at_gym — while onboarding was still
    # collecting the profile. Same gate as workout_log_start above.
    _pb_onboarded = (user.onboarding_step or 0) >= 3

    # Track workout intent (skip in logging mode)
    if message_type == "workout_log" and not _pb_in_mode and _pb_onboarded:
        # The one-shot Workout row is the LEGACY writer. With the agent loop on, its
        # log_workout tool is the single writer (read-before-write, split day, sets) —
        # live 2026-09-11 (user 27, "I hit pull") this path wrote a bare
        # workout_type="logged" row with no exercises next to the loop's real one.
        # State writes (confirmed today / at_gym) stay: cheap and idempotent.
        if not config.SINGLE_AGENT_LOOP_ENABLED:
            parsed = parse_workout_log(user, body)
            if parsed:
                workout = Workout(
                    user_id=user.id,
                    workout_type="logged",
                    exercises=parsed.get("exercises", []),
                    user_notes=body,
                    completed=True,
                )
                session.add(workout)
                session.commit()
        confirm_workout_today(user.id)
        set_session_state(user.id, "at_gym")
        threading.Thread(target=maybe_infer_training_days, args=(user.id,), daemon=True).start()

    if message_type == "workout_request" and not _pb_in_mode and _pb_onboarded:
        confirm_workout_today(user.id)
        set_session_state(user.id, "at_gym")
        threading.Thread(target=maybe_infer_training_days, args=(user.id,), daemon=True).start()

    # Catch training-day confirmations that don't look like workout logs —
    # e.g. "yeah hitting legs today" in reply to the morning briefing.
    # Only fires if today's workout hasn't been confirmed yet.
    if message_type == "freeform" and _pb_onboarded and not _pb_in_mode and not is_workout_confirmed_today(user.id):
        if _is_training_day_confirmation(body):
            confirm_workout_today(user.id)
            set_session_state(user.id, "at_gym")
            logger.info(f"Training day confirmed via freeform reply for {user.name}")
            threading.Thread(
                target=maybe_infer_training_days,
                args=(user.id,),
                daemon=True,
            ).start()

    # Check for goodnight signal — handle immediately, skip buffer
    # Never trigger during onboarding — user is answering questions, not signing off
    from zoneinfo import ZoneInfo as _ZI
    try:
        _gn_tz = _ZI(user.user_timezone or "America/Los_Angeles")
    except Exception:
        _gn_tz = _ZI("America/Los_Angeles")
    from datetime import datetime as _gn_dt
    if is_goodnight_signal(body, local_hour=_gn_dt.now(_gn_tz).hour) and (user.onboarding_step or 0) >= 3:
        from datetime import datetime, timedelta, timezone as _tz_store
        from zoneinfo import ZoneInfo
        wake_time = user.wake_time or "07:00"
        wake_h, wake_m = map(int, wake_time.split(":"))
        try:
            user_tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
        except Exception:
            user_tz = ZoneInfo("America/Los_Angeles")
        now_local = datetime.now(user_tz)
        wake_today = now_local.replace(hour=wake_h, minute=wake_m, second=0, microsecond=0)
        if now_local < wake_today:
            quiet_until = wake_today
        else:
            quiet_until = wake_today + timedelta(days=1)
        # Store as UTC so comparisons with datetime.now(utc) are consistent
        user.quiet_until = quiet_until.astimezone(_tz_store.utc).replace(tzinfo=None)
        session.commit()
        clear_session_state(user.id)

        import random
        response = random.choice([
            "Night. Get some real sleep.",
            "Sleep well. Talk tomorrow.",
            "Night, rest up.",
            "Get some rest. Hit me up in the morning.",
        ])
        send_sms(user.phone, response, user_id=user.id, message_type="goodnight")
        # Cancel any pending buffer so it doesn't flush after goodnight
        from message_buffer import cancel_buffer
        cancel_buffer(from_number)
        return get_twiml_response(), 200, {"Content-Type": "text/xml"}

    # Part B: workout logging mode intercept. Bypasses buffer (per plan —
    # 20-30s buffer would combine multiple sets in 60s into one blob,
    # breaking per-set ack). Mode = synchronous logging like goodnight.
    # The _handle_logging_mode_message helper always returns True (never
    # falls through to normal flow — see plan note on orphan-row bug).
    if _pb_in_mode and _pb_state is not None:
        _handle_logging_mode_message(user, body, _pb_state)
        return get_twiml_response(), 200, {"Content-Type": "text/xml"}

    # Adaptive buffer based on conversation momentum
    if (user.onboarding_step or 0) < 3:
        # Onboarding — tight buffer, user is actively engaged
        buffer_delay = (25, 35)
    else:
        # Check time since last inbound message to detect active conversation
        from datetime import datetime, timedelta, timezone as _tz
        last_inbound = (
            session.query(Message)
            .filter(Message.user_id == user.id, Message.direction == "in")
            .order_by(Message.created_at.desc())
            .first()
        )
        if last_inbound and last_inbound.created_at:
            last_msg_age = datetime.now(_tz.utc) - last_inbound.created_at.replace(tzinfo=_tz.utc)
            if last_msg_age < timedelta(minutes=5):
                # Active back-and-forth — respond faster
                buffer_delay = (20, 30)
            else:
                # New conversation thread — full buffer to catch double-texts
                buffer_delay = (90, 150)
        else:
            buffer_delay = (90, 150)

    # A captionless photo is the strongest signal that a caption is coming (people
    # send the pic, then the words). Live 2026-09-15: the image flushed at 20s, the
    # caption landed the same second → two turns, two replies, two questions.
    # Hold a bare image longer so the words join it.
    if image_data and not (body or "").strip():
        buffer_delay = (max(buffer_delay[0], 45), max(buffer_delay[1], 60))

    # Buffer the message — AI call and SMS response happen after the delay
    buffer_message(
        phone=from_number,
        body=body,
        user_id=user.id,
        message_type=message_type,
        image_url=image_data,
        process_callback=process_buffered_message,
        delay_override=buffer_delay,
    )

    # Return empty TwiML immediately — response comes later via the buffer
    return get_twiml_response(), 200, {"Content-Type": "text/xml"}


@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming SMS from Twilio. Buffers messages before processing."""
    from_number = request.form.get("From", "")
    body = request.form.get("Body", "").strip()
    # Twilio's idempotency key. Parsed up here so it's in scope for the release
    # in the except handler even if a crash happens before the claim.
    message_sid = request.form.get("MessageSid", "")

    # Check for MMS image
    num_media = int(request.form.get("NumMedia", 0))
    image_url = None
    image_data = None
    if num_media > 0:
        image_url = request.form.get("MediaUrl0")
        logger.info(f"MMS image received from {from_number}: {image_url}")
        if image_url:
            import requests as http_requests
            import base64
            img_response = http_requests.get(
                image_url,
                auth=(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)
            )
            if img_response.status_code == 200:
                # Decided from the bytes, not the header (image_normalize.py): a
                # HEIC or a mislabeled jpeg becomes a block the API accepts, or
                # None → text-only turn with the stored [image attached] marker.
                from image_normalize import normalize_image
                image_data = normalize_image(img_response.content,
                                             img_response.headers.get("Content-Type"),
                                             name=image_url.rsplit("/", 1)[-1])
            else:
                logger.error(f"Failed to download Twilio image: {img_response.status_code}")

    logger.info(f"Incoming SMS from {from_number}: {body}")

    session = get_session()
    try:
        user = session.query(User).filter(User.phone == from_number).first()

        if not user:
            logger.warning(f"Unknown number: {from_number}")
            return get_twiml_response(
                "Hey! You're not signed up for Cued yet. "
                "Visit cued.fit to get started."
            ), 200, {"Content-Type": "text/xml"}

        return _process_inbound(session, user, from_number, body, message_sid, image_url, image_data,
                                channel="sms", provider_sid=message_sid)

    except Exception as e:
        # Twilio does NOT retry inbound-message webhooks on 5xx or read-timeout by
        # default — its default retry policy is connect-timeout only (verified
        # against Twilio's webhook connection-overrides docs). So a crash here
        # cannot self-heal via an automatic retry; the inbound is dropped. We:
        #   (a) log it at ERROR as WEBHOOK_DROPPED so the drop is OBSERVABLE, and
        #   (b) release the idempotency claim so that IF this sid is genuinely
        #       re-delivered (occasional duplicate deliveries do happen) or the
        #       user resends, it can be reprocessed instead of deduped against a
        #       pass that never finished.
        # (Returning 5xx would only help if Twilio were configured with an rp
        # override / fallback URL — a messaging-rail config decision, not code.)
        logger.error("WEBHOOK_DROPPED sid=%s from=%s err=%s",
                     message_sid, from_number, e, exc_info=True)
        release_message_sid(message_sid)
        return get_twiml_response("Something went wrong on my end — I'll be back shortly."), 200, {"Content-Type": "text/xml"}
    finally:
        session.close()




def _last4(handle: str) -> str:
    """Log-safe handle: never a full phone/email in logs."""
    return "…" + (handle or "")[-4:]


@app.route("/internal/inbound", methods=["POST"])
def internal_inbound():
    """Sidecar → Flask (Photon migration Phase 4B). One inbound iMessage, dispatched
    into the SAME pipeline as the Twilio webhook via _process_inbound.

    Private: X-Internal-Secret required, and a missing configured secret means
    CLOSED. Body is JSON, or multipart with a `payload` JSON field + attachment_N
    files (see spectrum-sidecar/README.md).

    Unknown phone → WARNING (last 4 only) + 200 + an unknown_inbounds row. 200,
    not 4xx: the sidecar must not retry, and this is the Business-tier trigger
    signal (someone texting the line who isn't a user) — a durable count, not a
    memory."""
    secret = config.INTERNAL_SHARED_SECRET
    if not secret or request.headers.get("X-Internal-Secret") != secret:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    files = []
    if (request.content_type or "").startswith("multipart/form-data"):
        try:
            payload = json.loads(request.form.get("payload") or "")
        except ValueError:
            return jsonify({"ok": False, "error": "bad payload"}), 400
        files = [request.files[k] for k in sorted(request.files) if k.startswith("attachment_")]
    else:
        payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not str(payload.get("phone") or "").strip():
        return jsonify({"ok": False, "error": "expected {phone, text, provider_message_id, ...}"}), 400

    raw_handle = str(payload.get("phone")).strip()
    body = str(payload.get("text") or "").strip()
    provider_message_id = str(payload.get("provider_message_id") or "")
    reaction = payload.get("reaction") if isinstance(payload.get("reaction"), dict) else None

    # Same E.164 shape the Twilio webhook receives. An Apple-ID email can never
    # match a phone-keyed User; it falls straight into the unknown path.
    if "@" in raw_handle:
        handle = raw_handle
    else:
        try:
            handle = _normalize_phone(raw_handle)
        except ValueError:
            return jsonify({"ok": False, "error": "bad phone"}), 400

    # First attachment that normalizes to an image → the same base64 block the
    # MMS path builds. Format is decided from the bytes (an iPhone camera photo
    # arrives as image/heic and must become jpeg; live 2026-09-13), never from
    # the declared mime, so a non-image or unreadable attachment leaves
    # image_data None: the turn runs text-only and the stored marker still says
    # a picture came.
    image_name, image_data = None, None
    from image_normalize import normalize_image
    for f in files:
        image_name = image_name or f.filename or "attachment"
        if image_data is None:
            image_data = normalize_image(f.read(), f.mimetype, name=f.filename)

    session = get_session()
    try:
        user = session.query(User).filter(User.phone == handle).first()
        if not user:
            logger.warning("IMESSAGE_UNKNOWN_INBOUND from=%s chars=%d attachments=%d",
                           _last4(handle), len(body), len(files))
            record_unknown_inbound(handle, "imessage", body)
            return jsonify({"ok": True, "known": False}), 200
        if reaction:
            # Phase 5: a 👍 on a per-exercise message marks its sets done. Any other
            # inbound reaction is acknowledged and dropped (not coaching input).
            from workouts.session_ops import apply_tapback
            hit = apply_tapback(user.id, str(reaction.get("target_id") or ""), str(reaction.get("emoji") or ""))
            logger.info("IMESSAGE_REACTION_IN user=%s emoji=%s target=%s workout_hit=%s",
                        user.id, reaction.get("emoji"), str(reaction.get("target_id") or "")[:24], hit)
            return jsonify({"ok": True, "known": True, "reaction": True, "workout_hit": hit}), 200
        if not body and not files:
            return jsonify({"ok": True, "known": True, "ignored": "empty"}), 200
        logger.info("Incoming iMessage from %s: %s", _last4(handle), body[:120])
        _process_inbound(session, user, handle, body, provider_message_id, image_name, image_data,
                         channel="imessage", provider_sid=provider_message_id)
        return jsonify({"ok": True, "known": True}), 200
    except Exception as e:
        # Unlike Twilio, the sidecar DOES retry 5xx (3 attempts). Release the claim
        # so the retry reprocesses instead of deduping against a pass that never
        # finished. Observable as INTERNAL_INBOUND_DROPPED either way.
        logger.error("INTERNAL_INBOUND_DROPPED id=%s from=%s err=%s",
                     provider_message_id, _last4(handle), e, exc_info=True)
        release_message_sid(provider_message_id)
        return jsonify({"ok": False, "error": "internal"}), 500
    finally:
        session.close()


_LIFT_KEYWORDS_RE = re.compile(r"\b(hit|lifted|sets?|reps|bench|squat|deadlift|press)\b")


def classify_message(body: str, has_image: bool = False) -> str:
    """Simple heuristic to classify incoming message type."""
    body_lower = body.lower().strip()

    # Image-based classification
    if has_image:
        if any(kw in body_lower for kw in ["food", "ate", "eating", "lunch", "dinner", "breakfast", "meal", "snack"]):
            return "food_photo"
        if any(kw in body_lower for kw in ["progress", "physique", "body", "mirror", "before", "after"]):
            return "progress_photo"
        if any(kw in body_lower for kw in ["form", "check", "technique", "posture"]):
            return "form_check"
        receipt_keywords = [
            "receipt", "grocery", "groceries", "shopping", "bought", "picked up",
            "tj's", "trader joe", "safeway", "target", "berkeley bowl",
            "grocery outlet", "costco",
        ]
        if any(kw in body_lower for kw in receipt_keywords):
            return "receipt_photo"
        return "food_photo"  # Default assumption for images — most common use case

    # Part B: workout logging mode entry — check BEFORE generic lift-keyword
    # workout_log classifier so "start logging" doesn't get classified as a
    # set report (which would spuriously create a Workout row at the webhook).
    if is_workout_log_entry(body):
        return "workout_log_start"
    if body_lower in ("w", "workout", "send workout"):
        return "workout_request"
    if body_lower in ("m", "menu", "options", "swap"):
        return "meal_swap"
    if body_lower in ("1", "2", "3", "4", "5"):
        return "rating"
    # Whole words only. Live 2026-09-18/19 (user 32): "egg whITes" and "egg whITe"
    # matched "hit" as a substring, so a meal text became workout_log (at_gym state,
    # workout_confirmed on the day). "upSET", "imPRESSive" are the same class.
    if _LIFT_KEYWORDS_RE.search(body_lower):
        return "workout_log"
    return "freeform"


# ─── User Signup Endpoint ───────────────────────────
@app.route("/signup", methods=["GET"])
def signup_form():
    """Simple HTML signup form."""
    return render_template_string(SIGNUP_HTML)


def safe_int(val, default=None):
    try: return int(val) if val else default
    except (ValueError, TypeError): return default

def safe_float(val, default=None):
    try: return float(val) if val else default
    except (ValueError, TypeError): return default


@app.route("/signup", methods=["POST"])
def signup_submit():
    """Handle new user signup. Accepts form-encoded data (old form) or JSON (chat overlay)."""
    session = get_session()
    try:
        # Support both form submissions and JSON from the chat overlay
        if request.is_json:
            d = request.get_json(silent=True) or {}
            def get(key, default=""):
                return d.get(key, default)
        else:
            def get(key, default=""):
                return request.form.get(key, default)

        # Normalize phone
        phone = (get("phone") or "").strip()
        if not phone.startswith("+"):
            phone = "+1" + phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")

        # Check SMS consent — JSON sends boolean true, form sends "on"/"skip"
        raw_consent = get("sms_consent")
        if request.is_json:
            sms_consent = raw_consent is True or raw_consent == "on"
            sms_skipped = raw_consent is False or raw_consent == "skip"
        else:
            sms_consent = raw_consent == "on"
            sms_skipped = raw_consent == "skip"

        if not sms_consent and not sms_skipped:
            return jsonify({"status": "error", "message": "You must agree to receive SMS messages to use Cued."})

        # Duplicate check. A returning user still gets their iMessage opt-in link
        # (a shared Photon user can't be messaged until they text the line once).
        existing = session.query(User).filter(User.phone == phone).first()
        if existing:
            import photon
            return jsonify({"status": "exists", "message": f"{existing.name} is already signed up!",
                            "imessage_link": photon.imessage_link(existing.photon_user_id)})

        # Goals: chat sends array, form sends comma-joined string
        goal_raw = get("goal", "general_fitness")
        if isinstance(goal_raw, list):
            goal_str = ",".join(goal_raw)
        else:
            goal_str = goal_raw or "general_fitness"

        user = User(
            phone=phone,
            name=(get("name") or "").strip(),
            age=safe_int(get("age")),
            gender=get("gender") or "prefer_not_to_say",
            experience=get("experience") or "none",
            goal=goal_str,
            biggest_obstacle=get("biggest_obstacle") or None,
            equipment=get("equipment") or "full_gym",
        )
        session.add(user)
        session.commit()

        # Photon shared-pool consent gate (live 2026-09-12, user 28): a new shared
        # user can't be MESSAGED until they text their assigned line once, so the
        # site needs the opt-in deep link IN this response. Provision synchronously
        # here (one Photon round trip, flag-gated + never raises); start_onboarding's
        # own call is then an idempotent no-op. A Photon failure just means no link.
        imessage_link = None
        hook_deferred = False
        if sms_consent:
            import photon
            try:
                if photon.provision_user(user.id):
                    imessage_link = photon.imessage_link_for_user(user.id)
            except Exception as e:  # noqa: BLE001 — never block signup on Photon
                logger.warning("PHOTON_PROVISION_SKIPPED user=%s err=%s", user.id, e)
            # iMessage-first (founder, 2026-09-14): with a link in hand the hook WAITS
            # for their choice — their first blue text triggers it on iMessage
            # (process_buffered_message), "I don't have an iPhone" sends it by SMS
            # (/signup/channel), and the scheduler falls back to SMS + link after
            # ONBOARDING_HOOK_FALLBACK_MINUTES. Without a link there's no choice to make.
            if imessage_link:
                hook_deferred = True
                logger.info("ONBOARDING_HOOK_DEFERRED user=%s — awaiting channel choice (fallback in %s min)",
                            user.id, config.ONBOARDING_HOOK_FALLBACK_MINUTES)
            else:
                start_onboarding(user)

        logger.info(f"New user signed up: {user.name} ({user.phone}) | SMS consent: {sms_consent} | source: {'json' if request.is_json else 'form'} | imessage_link: {'yes' if imessage_link else 'no'}")
        return jsonify({"status": "ok", "message": f"Welcome {user.name}!", "name": user.name,
                        "imessage_link": imessage_link, "hook_deferred": hook_deferred})

    except Exception as e:
        logger.error(f"Signup error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()


# ─── Channel choice from the signup success screen ───
@app.route("/signup/channel", methods=["POST"])
@limiter.limit("10/minute;60/hour")
def signup_channel():
    """"I don't have an iPhone" on the success screen: put the user on SMS and send
    the deferred hook now. JSON {phone, channel:"sms"}. Idempotent — a user whose
    hook already went out just has their channel flipped. Phone-keyed like
    /activate-sms; rate-limited. A pending WAITLISTER (the same button on the
    waitlist success screen) only gets the flip: their hook waits for activation,
    which then goes straight to SMS."""
    d = request.get_json(silent=True) or {}
    phone = str(d.get("phone") or "").strip()
    channel = str(d.get("channel") or "").strip().lower()
    if channel != "sms":
        return jsonify({"status": "error", "message": "channel must be 'sms'"}), 400
    if not phone.startswith("+"):
        phone = "+1" + phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")
    session = get_session()
    try:
        user = session.query(User).filter(User.phone == phone).first()
        if not user:
            return jsonify({"status": "error", "message": "User not found."}), 404
        user.preferred_channel = "sms"  # photon_user_id stays: the link still works later
        session.commit()
        uid, step = user.id, (user.onboarding_step or 0)
        on_waitlist = user.waitlist_status == "pending"
    finally:
        session.close()
    sent = False
    if step == 0 and not on_waitlist:
        from onboarding_agent import send_onboarding_hook
        sent = send_onboarding_hook(uid, reason="no_iphone")
    logger.info("SIGNUP_CHANNEL user=%s channel=sms hook_sent=%s waitlist=%s", uid, sent, on_waitlist)
    return jsonify({"status": "ok", "channel": "sms", "hook_sent": sent, "waitlist": on_waitlist})


# ─── Workout card, Phase 0: static smoke page for the mini-app install test ───
CARD_TEST_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>bench</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; padding: 12px 14px 12px 14px; font-family: -apple-system, system-ui, sans-serif; font-size: 16px;
         width: 300px; box-sizing: border-box; background: transparent; }
  /* the Spectrum launcher's icon overlays the top-left ~36px of the card */
  h1 { padding-left: 40px; }
  h1 { font-size: 17px; font-weight: 600; margin: 0 0 8px; }
  label { display: flex; align-items: center; justify-content: space-between; min-height: 44px;
          padding: 0 4px; border-top: 1px solid rgba(128,128,128,.25); font-size: 16px; }
  label:first-of-type { border-top: 0; }
  input[type=checkbox] { width: 22px; height: 22px; }
</style></head>
<body>
<h1>bench 185 &times; 5{{ ' · v' ~ v if v else '' }}</h1>
<label>set 1 <input type="checkbox"{{ ' checked' if v else '' }}></label>
<label>set 2 <input type="checkbox"></label>
<label>set 3 <input type="checkbox"></label>
<label>set 4 <input type="checkbox"></label>
</body></html>"""


@app.route("/card/test", methods=["GET"])
def card_test_page():
    """Phase 0 smoke page: plain HTML, no JS, no auth. Sent as a live mini-app card
    (sidecar /send-card) to learn what a phone WITHOUT the Spectrum extension
    shows — the go/no-go for the whole card surface (GATE 0)."""
    v = (request.args.get("v") or "").strip()[:8]
    resp = make_response(render_template_string(CARD_TEST_HTML, v=v))
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/internal/card-test", methods=["POST"])
def card_test_send():
    """Phase 0 driver, secret-gated (same X-Internal-Secret as the sidecar), so the
    founder can run the install-flow experiment with plain curl:
      {"phone": "+1…", "action": "send"}                        → sends /card/test as a live card
      {"phone": "+1…", "action": "update", "card_session": {…}, "v": 2} → edits it in place
    Returns the sidecar's answer verbatim; a refusal (tier, extension) comes back as
    502 with Photon's text — that text IS the experiment's result."""
    if request.headers.get("X-Internal-Secret") != config.INTERNAL_SHARED_SECRET or not config.INTERNAL_SHARED_SECRET:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    d = request.get_json(silent=True) or {}
    phone = str(d.get("phone") or "").strip()
    action = str(d.get("action") or "send").lower()
    if not phone.startswith("+"):
        return jsonify({"ok": False, "error": "phone must be E.164"}), 400
    from photon_cards import send_card, update_card, CardError
    if action == "send_session":
        # A real session card (static layout, tap → overlay) for the founder's tests.
        from workouts.card import send_workout_card
        try:
            r = send_workout_card(int(d.get("session_id") or 0))
            return jsonify({"ok": True, **r})
        except CardError as e:
            return jsonify({"ok": False, "error": str(e)}), 502
        except (ValueError, TypeError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    base = request.url_root.rstrip("/").replace("http://", "https://")
    url = f"{base}/card/test"
    # Optional same-origin override (e.g. a real /card/workout/<token> link) so a
    # Phase 2 session can be tested inside Messages before the Phase 3 coach tool.
    override = str(d.get("url") or "").strip().replace("http://", "https://", 1)
    if override:
        if not override.startswith(base + "/card/"):
            return jsonify({"ok": False, "error": "url must be a /card/ path on this host"}), 400
        url = override
    try:
        if action == "send":
            return jsonify({"ok": True, "url": url, **send_card(phone, url, live=d.get("live", True) is not False)})
        if action == "update":
            cs = d.get("card_session")
            if not isinstance(cs, dict) or not cs.get("id"):
                return jsonify({"ok": False, "error": "update needs card_session from the send"}), 400
            v = d.get("v", 2)
            update_card(phone, cs, f"{url}?v={v}")
            return jsonify({"ok": True, "url": f"{url}?v={v}"})
        return jsonify({"ok": False, "error": "action must be send|update|send_session"}), 400
    except CardError as e:
        logger.warning("CARD_TEST_REFUSED phone_last4=%s action=%s err=%s", phone[-4:], action, e)
        return jsonify({"ok": False, "error": str(e)}), 502


# ─── Activate SMS (for users who skipped consent) ───
@app.route("/activate-sms", methods=["POST"])
def activate_sms():
    """Activate SMS coaching for a user who signed up without consent."""
    session = get_session()
    try:
        phone = request.form.get("phone", "").strip()
        if not phone.startswith("+"):
            phone = "+1" + phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")

        user = session.query(User).filter(User.phone == phone).first()
        if not user:
            return jsonify({"status": "error", "message": "User not found."})

        schedule_user(user)
        start_onboarding(user)

        logger.info(f"SMS activated for existing user: {user.name} ({user.phone})")
        return jsonify({"status": "ok", "name": user.name})

    except Exception as e:
        logger.error(f"Activate SMS error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        session.close()


# ─── Waitlist Endpoint ──────────────────────────────────────────────────
@app.route("/waitlist", methods=["POST"])
@limiter.limit("3/minute;20/hour")
def waitlist_signup():
    """
    Public waitlist signup. JSON-only. CORS-restricted to ALLOWED_ORIGINS
    (must be set to https://cued.fit,https://www.cued.fit in prod env).
    Does NOT send an SMS — admin promotes from waitlist later via
    /admin/user/<id>/activate-waitlist, which sends the first text via
    start_onboarding.

    2026-09-19: the site's waitlist is the chat sign-up, so the body carries the
    same profile /signup takes (age, gender, goal, biggest_obstacle, experience,
    equipment, sms_consent) — stored on the pending row so the coach starts with
    it. The Photon line is provisioned here too (flag-gated, degrades to no link)
    and `imessage_link` comes back so the success screen can offer "Text me on
    iMessage": their tap opens the line while they wait (held by the gate in
    _process_inbound), and activation then goes blue first try.
    """
    if not request.is_json:
        return jsonify({"status": "error", "message": "Expected JSON body."}), 400
    data = request.get_json(silent=True) or {}

    name = (data.get("name") or "").strip()
    raw_phone = (data.get("phone") or "").strip()
    raw_email = (data.get("email") or "").strip() or None
    source = (data.get("source") or "").strip()[:40] or None
    timezone_str = (data.get("timezone") or "America/Los_Angeles").strip()[:50]

    # Validation
    if not name or len(name) > 100:
        return jsonify({"status": "error", "message": "Please enter your name."}), 400
    # The chat asks for the full name (kept in full_name — for us: admin, email).
    # `name` is what the coach SAYS ("yo {name}…" in the hook, every trigger
    # prompt, the transcript labels), so it is the first name only — enforced
    # here, whatever the client sent. Never the full name to the model.
    full_name = (str(data.get("full_name") or "").strip() or (name if " " in name else "")) or None
    if full_name and len(full_name) > 200:
        return jsonify({"status": "error", "message": "That name doesn't look right."}), 400
    name = name.split()[0]
    try:
        phone = _normalize_phone(raw_phone, strict=True)
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    if raw_email and not _looks_like_email(raw_email):
        return jsonify({"status": "error", "message": "That email doesn't look right."}), 400

    # Consent is the point of the list (we text them when their spot opens).
    raw_consent = data.get("sms_consent")
    if not (raw_consent is True or raw_consent == "on"):
        return jsonify({"status": "error",
                        "message": "You must agree to receive texts from Cued to join."}), 400

    # Profile — same shapes and defaults as /signup (goal: list or csv). Over-length
    # values are rejected, never truncated: a silently clipped enum is a wrong fact.
    goal_raw = data.get("goal")
    if isinstance(goal_raw, list):
        goal_str = ",".join(str(g).strip() for g in goal_raw if str(g).strip())
    else:
        goal_str = str(goal_raw).strip() if goal_raw else ""
    profile = {
        "age": safe_int(data.get("age")),
        "gender": str(data.get("gender") or "").strip() or "prefer_not_to_say",
        "goal": goal_str or "general_fitness",
        "biggest_obstacle": str(data.get("biggest_obstacle") or "").strip() or None,
        "experience": str(data.get("experience") or "").strip() or "none",
        "equipment": str(data.get("equipment") or "").strip() or "full_gym",
    }
    for col, cap in (("gender", 20), ("goal", 200), ("biggest_obstacle", 50),
                     ("experience", 20), ("equipment", 100)):
        if profile[col] and len(profile[col]) > cap:
            return jsonify({"status": "error",
                            "message": f"That {col.replace('_', ' ')} doesn't look right."}), 400

    session = get_session()
    try:
        existing = session.query(User).filter(User.phone == phone).first()
        if existing:
            # Log delta for manual admin reconciliation; do NOT mutate the row.
            # This protects against denial-of-modification: an unauthenticated
            # endpoint must never silently rewrite name/email on an existing row.
            logger.info(
                "WAITLIST_DUP phone=%s existing_name=%r existing_email=%r "
                "incoming_name=%r incoming_email=%r incoming_source=%r",
                phone, existing.name, existing.email or "—",
                name, raw_email or "—", source or "—",
            )
            if existing.waitlist_status == "pending":
                # Still pending: hand back their opt-in link (if provisioned) so a
                # re-submit can still open the line. The row itself is untouched.
                import photon
                return jsonify({"status": "exists",
                                "message": "You're already on the waitlist.",
                                "imessage_link": photon.imessage_link(existing.photon_user_id)}), 200
            return jsonify({"status": "exists",
                            "message": "This number is already signed up."}), 200

        user = User(
            phone=phone,
            name=name,
            email=raw_email,
            signup_source=source,
            user_timezone=timezone_str,
            waitlist_status="pending",
            # User.active stays True (default). The spec's request body sends
            # active=false but it's intent-level — the frontend never reads it
            # back. waitlist_status='pending' is the sole waitlist marker so
            # User.active can keep its eventual pause/block semantic.
            onboarding_step=0,
            full_name=full_name,
            **profile,
        )
        session.add(user)
        session.commit()
        uid = user.id
        logger.info(
            "WAITLIST_NEW phone=%s name=%r source=%r email=%r goal=%r experience=%r",
            phone, name, source or "—", raw_email or "—", profile["goal"], profile["experience"],
        )
        # Provision the Photon line now (flag-gated, never raises) so the success
        # screen can offer "Text me on iMessage". No hook — that waits for the admin;
        # a pending user's texts are held by the gate in _process_inbound. A Photon
        # failure (cap, creds) just means no link; the row is saved either way.
        imessage_link = None
        try:
            import photon
            if photon.provision_user(uid):
                imessage_link = photon.imessage_link_for_user(uid)
        except Exception as e:  # noqa: BLE001 — never block the waitlist on Photon
            logger.warning("PHOTON_PROVISION_SKIPPED user=%s err=%s", uid, e)
        logger.info("WAITLIST_PROVISIONED user=%s imessage_link=%s", uid, "yes" if imessage_link else "no")
        return jsonify({"status": "ok", "imessage_link": imessage_link}), 200
    except Exception as e:
        logger.error(f"/waitlist error: {e}", exc_info=True)
        return jsonify({"status": "error",
                        "message": "Something went wrong. Try again."}), 500
    finally:
        session.close()


# ─── Admin Dashboard — cost aggregations from token_usage ─────────────────
@app.route("/profile/<token>", methods=["GET"])
@limiter.limit("30/minute;300/hour")
def profile_read(token):
    """
    Read-only JSON behind the user's profile page (cued.fit/profile.html?t=…).
    The token is the HMAC link the coach texts them (profile_page.py) — a bad or
    tampered token is a plain 404, indistinguishable from a missing user, so the
    endpoint leaks nothing about which ids exist. CORS-restricted to
    ALLOWED_ORIGINS like /waitlist; never cached.
    """
    user_id = verify_profile_token(token)
    if user_id is None:
        return jsonify({"status": "error", "message": "That link isn't valid."}), 404
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return jsonify({"status": "error", "message": "That link isn't valid."}), 404
        payload = build_profile_payload(session, user)
    except Exception as e:
        logger.exception("PROFILE_READ_FAIL user=%s: %s", user_id, e)
        return jsonify({"status": "error", "message": "Couldn't load your profile right now."}), 500
    finally:
        session.close()
    resp = jsonify({"status": "ok", "profile": payload})
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _compute_cost_metrics(session):
    """
    Phase C1.5 — read token_usage and produce the measured-cost dict the
    Finances page consumes. Replaces the pre-C1.5 volume-based heuristic
    (total_sent * 0.006). UTC day bucket for v1 (see plan notes).

    Returns dict with keys: api_cost_today, api_cost_7d, api_cost_30d,
    run_rate_30d, cost_sonnet_7d, cost_haiku_7d, pct_sonnet, pct_haiku,
    cost_by_site (list of {site, calls, cost_usd, avg_cost_per_call}),
    cache_ratio, cache_saved_7d, cost_per_active_user_per_day,
    per_user_30d_cost (dict user_id -> sum_cost_usd).
    """
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func
    from models import TokenUsage

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    seven_days_ago = now - timedelta(days=7)
    thirty_days_ago = now - timedelta(days=30)

    def _sum_cost(since):
        v = (session.query(func.coalesce(func.sum(TokenUsage.cost_usd), 0.0))
             .filter(TokenUsage.created_at >= since)
             .scalar()) or 0.0
        return round(float(v), 4)

    api_cost_today = _sum_cost(today_start)
    api_cost_7d = _sum_cost(seven_days_ago)
    api_cost_30d = _sum_cost(thirty_days_ago)
    run_rate_30d = round((api_cost_7d / 7.0) * 30.0, 2) if api_cost_7d else 0.0

    # Model split — 7d
    model_rows = (session.query(TokenUsage.model,
                                func.coalesce(func.sum(TokenUsage.cost_usd), 0.0))
                  .filter(TokenUsage.created_at >= seven_days_ago)
                  .group_by(TokenUsage.model)
                  .all())
    model_split = {m: float(c or 0) for m, c in model_rows}
    cost_sonnet_7d = round(model_split.get("sonnet", 0.0), 4)
    cost_haiku_7d = round(model_split.get("haiku", 0.0), 4)
    model_total = cost_sonnet_7d + cost_haiku_7d
    pct_sonnet = round(cost_sonnet_7d / model_total * 100) if model_total > 0 else 0
    pct_haiku = round(cost_haiku_7d / model_total * 100) if model_total > 0 else 0

    # Per-site — 7d, ordered by cost desc
    site_rows = (session.query(
                    TokenUsage.site,
                    func.count(TokenUsage.id),
                    func.coalesce(func.sum(TokenUsage.cost_usd), 0.0),
                 )
                 .filter(TokenUsage.created_at >= seven_days_ago)
                 .group_by(TokenUsage.site)
                 .all())
    cost_by_site = sorted([
        {
            "site": s or "(unknown)",
            "calls": int(c or 0),
            "cost_usd": round(float(cost or 0), 4),
            "avg_cost_per_call": round(float(cost or 0) / c, 6) if c else 0.0,
        }
        for s, c, cost in site_rows
    ], key=lambda r: -r["cost_usd"])

    # Cache effectiveness — 7d
    cache_agg = (session.query(
                    func.coalesce(func.sum(TokenUsage.input_tokens), 0),
                    func.coalesce(func.sum(TokenUsage.cache_creation_input_tokens), 0),
                    func.coalesce(func.sum(TokenUsage.cache_read_input_tokens), 0),
                 )
                 .filter(TokenUsage.created_at >= seven_days_ago)
                 .one())
    inp_sum, cc_sum, cr_sum = (int(x or 0) for x in cache_agg)
    denom = inp_sum + cc_sum + cr_sum
    cache_ratio = round(cr_sum / denom * 100, 1) if denom > 0 else 0.0

    # $ saved by cache reads, weighted by per-model input rate
    model_cache_rows = (session.query(TokenUsage.model,
                                      func.coalesce(func.sum(TokenUsage.cache_read_input_tokens), 0))
                        .filter(TokenUsage.created_at >= seven_days_ago)
                        .group_by(TokenUsage.model)
                        .all())
    cache_saved_7d = 0.0
    for m, cr_tokens in model_cache_rows:
        if not m or not cr_tokens:
            continue
        in_rate = config.MODEL_PRICING.get(m, {}).get("input", 0.0)
        # Saving: would-have-paid (1.0 * in_rate) vs paid (0.10 * in_rate) per token
        cache_saved_7d += int(cr_tokens) * in_rate * (1 - config.CACHE_READ_MULTIPLIER) / 1_000_000
    cache_saved_7d = round(cache_saved_7d, 4)

    # Cost / active user / day — distinct users in token_usage over 7d
    active_user_count = (session.query(func.count(func.distinct(TokenUsage.user_id)))
                         .filter(TokenUsage.created_at >= seven_days_ago,
                                 TokenUsage.user_id.isnot(None))
                         .scalar()) or 0
    cost_per_active_user_per_day = (
        round(api_cost_7d / active_user_count / 7.0, 4) if active_user_count > 0 else 0.0
    )

    # Per-user spend over 30d
    per_user_rows = (session.query(TokenUsage.user_id,
                                   func.coalesce(func.sum(TokenUsage.cost_usd), 0.0))
                     .filter(TokenUsage.created_at >= thirty_days_ago,
                             TokenUsage.user_id.isnot(None))
                     .group_by(TokenUsage.user_id)
                     .all())
    per_user_30d_cost = {int(uid): round(float(c or 0), 4) for uid, c in per_user_rows}

    return {
        "api_cost_today": api_cost_today,
        "api_cost_7d": api_cost_7d,
        "api_cost_30d": api_cost_30d,
        "run_rate_30d": run_rate_30d,
        "cost_sonnet_7d": cost_sonnet_7d,
        "cost_haiku_7d": cost_haiku_7d,
        "pct_sonnet": pct_sonnet,
        "pct_haiku": pct_haiku,
        "cost_by_site": cost_by_site,
        "cache_ratio": cache_ratio,
        "cache_saved_7d": cache_saved_7d,
        "cost_per_active_user_per_day": cost_per_active_user_per_day,
        "per_user_30d_cost": per_user_30d_cost,
    }


# ─── Admin Dashboard ────────────────────────────────
@app.route("/admin")
def admin():
    """Metrics dashboard for tracking beta performance."""
    from datetime import datetime, timedelta, timezone
    import pytz
    pst = pytz.timezone("America/Los_Angeles")
    session = get_session()
    try:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        def as_utc(dt):
            if dt is None:
                return None
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt

        def fmt_pst(dt):
            if not dt:
                return "—"
            dt = as_utc(dt)
            return dt.astimezone(pst).strftime("%b %d, %I:%M %p")
 
        # ── USER STATS ──
        # Split waitlist out so it doesn't skew the existing metrics. Pending
        # waitlist users live in the same table but are excluded from response
        # rate, dN_rate, total_users, etc. They get their own table in the
        # admin Waitlist tab.
        _all_users_raw = session.query(User).all()
        pending_waitlist = sorted(
            (u for u in _all_users_raw if u.waitlist_status == "pending"),
            key=lambda u: u.created_at or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        all_users = [u for u in _all_users_raw if u.waitlist_status is None]
        total_users = len(all_users)
        active_users = sum(1 for u in all_users if u.active)
        waitlist_count = len(pending_waitlist)
 
        # ── MESSAGE STATS ──
        all_messages = session.query(Message).all()
        total_sent = sum(1 for m in all_messages if m.direction == "out")
        total_received = sum(1 for m in all_messages if m.direction == "in")
        response_rate = round((total_received / total_sent * 100) if total_sent > 0 else 0)

        # ── TODAY'S ACTIVITY ──
        today_active = 0
        for u in all_users:
            user_msgs_today = [m for m in all_messages
                               if m.user_id == u.id
                               and m.direction == "in"
                               and as_utc(m.created_at) >= today_start]
            if user_msgs_today:
                today_active += 1

        # ── RETENTION COHORTS ──
        d1_responded = 0
        for u in all_users:
            user_incoming = [m for m in all_messages if m.user_id == u.id and m.direction == "in"]
            if user_incoming:
                d1_responded += 1
        d1_rate = round((d1_responded / total_users * 100) if total_users > 0 else 0)

        def active_in_window(days):
            cutoff = now - timedelta(days=days)
            count = 0
            eligible = 0
            for u in all_users:
                if u.created_at and as_utc(u.created_at) <= cutoff:
                    eligible += 1
                    user_msgs = [m for m in all_messages
                                 if m.user_id == u.id
                                 and m.direction == "in"
                                 and as_utc(m.created_at) >= cutoff]
                    if user_msgs:
                        count += 1
                elif not u.created_at:
                    eligible += 1
            return count, eligible

        d7_active, d7_eligible = active_in_window(7)
        d14_active, d14_eligible = active_in_window(14)
        d30_active, d30_eligible = active_in_window(30)
        d7_rate = round((d7_active / d7_eligible * 100) if d7_eligible > 0 else 0)
        d14_rate = round((d14_active / d14_eligible * 100) if d14_eligible > 0 else 0)
        d30_rate = round((d30_active / d30_eligible * 100) if d30_eligible > 0 else 0)

        # ── RATINGS ──
        rating_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        total_rating_sum = 0
        total_ratings = 0
        for m in all_messages:
            if m.direction == "in" and m.body and m.body.strip() in ["1", "2", "3", "4", "5"]:
                r = int(m.body.strip())
                rating_counts[r] += 1
                total_rating_sum += r
                total_ratings += 1
        avg_rating = round(total_rating_sum / total_ratings, 1) if total_ratings > 0 else 0
        max_rating_count = max(rating_counts.values()) if any(rating_counts.values()) else 1

        # ── MEAL STATS ──
        from models import Meal, WeightLog
        all_meals = session.query(Meal).all()
        total_meals = len(all_meals)
        meals_today = sum(1 for m in all_meals if as_utc(m.eaten_at) >= today_start)
        avg_meal_calories = round(
            sum(m.calories or 0 for m in all_meals) / total_meals
        ) if total_meals > 0 else 0
        total_weight_logs = session.query(WeightLog).count()

        # ── USER TABLE DATA ──
        users_data = []
        for u in all_users:
            user_msgs = [m for m in all_messages if m.user_id == u.id]
            user_incoming = [m for m in user_msgs if m.direction == "in"]
            msg_count = len(user_msgs)

            if user_incoming:
                last_msg = max(user_incoming, key=lambda m: m.created_at)
                last_active = fmt_pst(last_msg.created_at)
                days_inactive = (now - as_utc(last_msg.created_at)).days if last_msg.created_at else 99
            else:
                last_active = "Never"
                days_inactive = 99

            workout_count = session.query(Workout).filter(Workout.user_id == u.id).count()
            meal_count = sum(1 for m in all_meals if m.user_id == u.id)

            user_ratings = [int(m.body.strip()) for m in user_incoming
                            if m.body and m.body.strip() in ["1", "2", "3", "4", "5"]]
            user_avg = round(sum(user_ratings) / len(user_ratings), 1) if user_ratings else "—"

            signed_up = fmt_pst(u.created_at) if u.created_at else "—"

            # Phase C1.5: real measured cost from token_usage (30d).
            # Populated post-loop from cost_metrics["per_user_30d_cost"].
            cost_usd = 0.0
            created_ts = int(u.created_at.timestamp()) if u.created_at else 0

            users_data.append({
                "id": u.id,
                "name": u.name,
                "phone": u.phone[-4:] if u.phone else "—",
                "signed_up": signed_up,
                "last_active": last_active,
                "msg_count": msg_count,
                "meal_count": meal_count,
                "workout_count": workout_count,
                "avg_rating": user_avg,
                "days_inactive": days_inactive,
                "onboarding_step": u.onboarding_step or 0,
                "cost_usd": cost_usd,
                "created_ts": created_ts,
            })

        # ── RECENT MESSAGES ──
        recent = sorted(all_messages, key=lambda m: m.created_at if m.created_at else now, reverse=True)[:50]
        user_map = {u.id: u.name for u in all_users}
        recent_messages_data = [{
            "time": fmt_pst(m.created_at),
            "user_name": user_map.get(m.user_id, "Unknown"),
            "user_id": m.user_id,
            "direction": m.direction,
            "body": m.body or "",
            "message_type": m.message_type or "—",
        } for m in recent]

        # ── RECENT MEALS (grouped by day) ──
        from itertools import groupby
        recent_meal_rows = sorted(all_meals, key=lambda m: m.eaten_at if m.eaten_at else now, reverse=True)[:60]

        recent_meals_data = [{
            "user_id": m.user_id,
            "user_name": user_map.get(m.user_id, "Unknown"),
            "eaten_at": fmt_pst(m.eaten_at),
            "description": m.description or "",
            "calories": m.calories or 0,
            "protein_g": m.protein_g or 0,
            "source": m.source or "text",
            "confidence": m.confidence or "medium",
        } for m in recent_meal_rows]

        grouped_meals = []
        for _, meals_iter in groupby(recent_meal_rows, key=lambda m: m.eaten_at.strftime("%Y-%m-%d") if m.eaten_at else "unknown"):
            meals_list = list(meals_iter)
            day_cals = sum(m.calories or 0 for m in meals_list)
            day_protein = sum(m.protein_g or 0 for m in meals_list)
            grouped_meals.append({
                "date": meals_list[0].eaten_at.strftime("%a %b %d") if meals_list[0].eaten_at else "Unknown",
                "total_calories": day_cals,
                "total_protein": round(day_protein),
                "count": len(meals_list),
                "meals": [{
                    "user_id": m.user_id,
                    "user_name": user_map.get(m.user_id, "Unknown"),
                    "time_only": m.eaten_at.strftime("%-I:%M %p") if m.eaten_at else "",
                    "description": m.description or "",
                    "calories": m.calories or 0,
                    "protein_g": m.protein_g or 0,
                    "source": m.source or "text",
                    "confidence": m.confidence or "medium",
                } for m in meals_list],
            })

        # ── AGENT PIPELINE STATS ──
        route_nutrition = sum(1 for m in all_messages if m.direction == "out" and m.message_type and "nutrition" in m.message_type)
        route_training = sum(1 for m in all_messages if m.direction == "out" and m.message_type and "training" in m.message_type)
        route_readiness = sum(1 for m in all_messages if m.direction == "out" and m.message_type and "readiness" in m.message_type)
        route_legacy = total_sent - route_nutrition - route_training - route_readiness

        # Message type breakdown
        from collections import Counter
        type_counts = Counter(m.message_type or "unknown" for m in all_messages if m.direction == "out")
        total_typed = sum(type_counts.values()) or 1
        message_types_data = sorted([
            {"type": t, "count": c, "pct": round(c / total_typed * 100)}
            for t, c in type_counts.most_common(12)
        ], key=lambda x: -x["count"])

        # ── COSTS (Phase C1.5 — measured API from token_usage; Twilio volume estimate) ──
        cost_metrics = _compute_cost_metrics(session)
        total_msg_count = total_sent + total_received
        twilio_cost = round(total_msg_count * config.TWILIO_COST_PER_SEGMENT, 2)
        api_cost = cost_metrics["api_cost_30d"]
        total_cost = round(twilio_cost + api_cost, 2)
        cost_per_user = round(total_cost / active_users, 2) if active_users > 0 else 0
        cost_per_msg = round(total_cost / total_msg_count, 4) if total_msg_count > 0 else 0

        # Patch real per-user 30d cost into users_data (replaces the old heuristic
        # placeholder set inside the user loop).
        per_user_30d = cost_metrics["per_user_30d_cost"]
        for ud in users_data:
            ud["cost_usd"] = round(per_user_30d.get(ud["id"], 0.0), 2)

        # Waitlist tab data (Joined date in PST for admin readability).
        waitlist_data = []
        for wu in pending_waitlist:
            joined_str = "—"
            if wu.created_at:
                ts = wu.created_at if wu.created_at.tzinfo else wu.created_at.replace(tzinfo=timezone.utc)
                joined_str = ts.astimezone(pst).strftime("%b %d, %Y %I:%M %p PST")
            waitlist_data.append({
                "id": wu.id,
                "name": wu.name,
                "phone": wu.phone[-4:] if wu.phone else "—",
                "phone_full": wu.phone or "—",
                "email": wu.email or "—",
                "source": wu.signup_source or "—",
                "joined": joined_str,
                "timezone": wu.user_timezone or "—",
                # Profile from the sign-up chat (2026-09-19) + whether Activate goes blue.
                "full_name": wu.full_name or "—",
                "age": wu.age if wu.age is not None else "—",
                "gender": wu.gender or "—",
                "goal": (wu.goal or "").replace(",", ", ") or "—",
                "experience": wu.experience or "—",
                "equipment": wu.equipment or "—",
                "obstacle": wu.biggest_obstacle or "—",
                "channel": _waitlist_channel_state(wu),
            })

        return render_template_string(ADMIN_HTML,
            now=now.astimezone(pst).strftime("%b %d, %Y %I:%M %p PST"),
            total_users=total_users,
            active_users=active_users,
            total_sent=total_sent,
            total_received=total_received,
            response_rate=response_rate,
            avg_rating=avg_rating,
            total_ratings=total_ratings,
            today_active=today_active,
            total_meals=total_meals,
            meals_today=meals_today,
            avg_meal_calories=avg_meal_calories,
            total_weight_logs=total_weight_logs,
            d1_responded=d1_responded,
            d1_rate=d1_rate,
            d7_active=d7_active, d7_eligible=d7_eligible,
            d7_rate=d7_rate,
            d14_active=d14_active, d14_eligible=d14_eligible,
            d14_rate=d14_rate,
            d30_active=d30_active, d30_eligible=d30_eligible,
            d30_rate=d30_rate,
            users=users_data,
            rating_counts=rating_counts,
            max_rating_count=max_rating_count,
            recent_messages=recent_messages_data,
            recent_meals=recent_meals_data,
            grouped_meals=grouped_meals,
            route_nutrition=route_nutrition,
            route_training=route_training,
            route_readiness=route_readiness,
            route_legacy=route_legacy,
            message_types=message_types_data,
            twilio_cost=twilio_cost,
            api_cost=api_cost,
            total_cost=total_cost,
            cost_per_user=cost_per_user,
            cost_per_msg=cost_per_msg,
            # Phase C1.5 measured-cost vars
            api_cost_today=cost_metrics["api_cost_today"],
            api_cost_7d=cost_metrics["api_cost_7d"],
            api_cost_30d=cost_metrics["api_cost_30d"],
            run_rate_30d=cost_metrics["run_rate_30d"],
            cost_sonnet_7d=cost_metrics["cost_sonnet_7d"],
            cost_haiku_7d=cost_metrics["cost_haiku_7d"],
            pct_sonnet=cost_metrics["pct_sonnet"],
            pct_haiku=cost_metrics["pct_haiku"],
            cost_by_site=cost_metrics["cost_by_site"],
            cache_ratio=cost_metrics["cache_ratio"],
            cache_saved_7d=cost_metrics["cache_saved_7d"],
            cost_per_active_user_per_day=cost_metrics["cost_per_active_user_per_day"],
            # Waitlist
            waitlist=waitlist_data,
            waitlist_count=waitlist_count,
        )
    finally:
        session.close()
 


# ─── Manual Send (admin override) ───────────────────
# Double-submit guard: live 2026-09-18 two identical "hey, just checking in" bubbles
# landed 1 ms apart from two POSTs (a double click / double form handler). The same
# (user, body) inside ADMIN_SEND_DEDUPE_SECONDS is a no-op, not a second text.
ADMIN_SEND_DEDUPE_SECONDS = 10
_admin_send_recent: dict = {}   # (user_id, body) -> monotonic seconds


def _admin_send_is_duplicate(user_id: int, body: str) -> bool:
    import time as _time
    now = _time.monotonic()
    for k, ts in list(_admin_send_recent.items()):
        if now - ts > ADMIN_SEND_DEDUPE_SECONDS:
            _admin_send_recent.pop(k, None)
    key = (user_id, body)
    if key in _admin_send_recent:
        return True
    _admin_send_recent[key] = now
    return False


@app.route("/admin/send", methods=["POST"])
def admin_send():
    """Manually send a message to a user (admin override for when AI messes up)."""
    session = get_session()
    try:
        user_id = int(request.form.get("user_id"))
        body = request.form.get("body", "").strip()
        user = session.get(User, user_id)
        if user and body:
            if _admin_send_is_duplicate(user.id, body):
                logger.info("ADMIN_SEND_DUPLICATE user=%s body=%r (within %ss — not resent)",
                            user.id, body[:40], ADMIN_SEND_DEDUPE_SECONDS)
                return jsonify({"status": "duplicate"})
            send_sms(user.phone, body, user_id=user.id, message_type="admin")
            return jsonify({"status": "ok"})
        return jsonify({"status": "error"}), 400
    finally:
        session.close()


# ─── Activate Waitlist User (admin) ──────────────────────
@app.route("/admin/user/<int:user_id>/activate-waitlist", methods=["POST"])
def admin_activate_waitlist(user_id):
    """Promote a waitlisted user to active. Sends the first SMS via
    start_onboarding. Idempotent guard: returns 400 if user is not on
    the waitlist (already activated or never was on it)."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return jsonify({"status": "error", "message": "User not found."}), 404
        if user.waitlist_status != "pending":
            return jsonify({"status": "error",
                            "message": "User is not on the waitlist."}), 400
        user.waitlist_status = None
        user.activated_at = datetime.now(timezone.utc)
        session.commit()
        # Refresh from a new session so start_onboarding sees the committed state.
        fresh = session.get(User, user_id)
        # start_onboarding ONLY. Do NOT also call schedule_user — wake_time is
        # None until onboarding completes, so schedule_user would just log a
        # warning and no-op. The existing onboarding-completion path re-calls
        # schedule_user once wake_time is set.
        start_onboarding(fresh)
        logger.info("WAITLIST_ACTIVATE user_id=%s phone=%s name=%r",
                    fresh.id, fresh.phone, fresh.name)
        return jsonify({"status": "ok",
                        "message": f"{fresh.name} activated."}), 200
    except Exception as e:
        logger.error(f"/admin/user/{user_id}/activate-waitlist error: {e}",
                     exc_info=True)
        return jsonify({"status": "error",
                        "message": "Activation failed."}), 500
    finally:
        session.close()


# ─── Delete User (admin) ────────────────────────────
def _purge_user_rows(session, user_id: int) -> None:
    """Delete every child row that does NOT cascade from users (see models.py:
    Message/Meal/Workout/DailyLog/WeightLog/Signal/PantryItem/Place/QueueTicket/
    WorkoutSession(+SetLog)/TargetAdjustment carry plain FKs). The newer tables
    (events, heartbeat_ticks, episodic, token_usage, processed_messages) cascade
    or SET NULL on their own. Caller deletes the User row and commits."""
    from models import (Meal, WeightLog, Signal, PantryItem, Place, QueueTicket,
                        WorkoutSession, SetLog, TargetAdjustment)
    session_ids = [sid for (sid,) in session.query(WorkoutSession.id)
                   .filter(WorkoutSession.user_id == user_id).all()]
    if session_ids:
        session.query(SetLog).filter(SetLog.session_id.in_(session_ids)) \
               .delete(synchronize_session=False)
    for model in (WorkoutSession, QueueTicket, Place, PantryItem, Signal, TargetAdjustment,
                  Message, Meal, WeightLog, Workout, DailyLog):
        session.query(model).filter(model.user_id == user_id).delete(synchronize_session=False)


@app.route("/admin/user/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id):
    """Permanently delete a user and all their data."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return jsonify({"status": "error", "message": "User not found"}), 404
        name = user.name
        _purge_user_rows(session, user_id)
        session.delete(user)
        session.commit()
        logger.info(f"Admin deleted user: {name} (id={user_id})")
        return jsonify({"status": "ok", "message": f"{name} deleted."})
    finally:
        session.close()


@app.route("/admin/user/<int:user_id>/remove-waitlist", methods=["POST"])
def admin_remove_waitlist(user_id):
    """Drop a PENDING waitlister for good (spam, duplicate, changed their mind).
    Guarded to waitlist_status == 'pending' so the waitlist tab can never delete
    an activated user; those go through /delete from the Users tab. Sends
    nothing. Their Photon line (if provisioned) is left in the pool — there is
    no deprovision call — so a re-signup on the same number gets it back via
    find_user."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return jsonify({"status": "error", "message": "User not found."}), 404
        if user.waitlist_status != "pending":
            return jsonify({"status": "error",
                            "message": "User is not on the waitlist."}), 400
        name, phone = user.name, user.phone
        _purge_user_rows(session, user_id)
        session.delete(user)
        session.commit()
        logger.info("WAITLIST_REMOVE user_id=%s phone=%s name=%r", user_id, phone, name)
        return jsonify({"status": "ok", "message": f"{name} removed from the waitlist."}), 200
    except Exception as e:
        logger.error(f"/admin/user/{user_id}/remove-waitlist error: {e}", exc_info=True)
        return jsonify({"status": "error", "message": "Remove failed."}), 500
    finally:
        session.close()


# ─── Admin User Detail ──────────────────────────────
@app.route("/admin/user/<int:user_id>")
def admin_user(user_id):
    """Per-user detail page — profile, metrics, meals, weight, conversation."""
    from datetime import datetime, timezone, timedelta
    import pytz
    from models import Meal, WeightLog
    pst = pytz.timezone("America/Los_Angeles")
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return "User not found", 404

        now = datetime.now(timezone.utc)

        def as_utc(dt):
            if dt is None: return None
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

        def fmt_pst(dt):
            if not dt: return "—"
            return as_utc(dt).astimezone(pst).strftime("%b %d, %I:%M %p")

        def fmt_date(dt):
            if not dt: return "—"
            return as_utc(dt).astimezone(pst).strftime("%b %d, %Y")

        # Messages
        messages = session.query(Message).filter(Message.user_id == user_id).order_by(Message.created_at).all()
        total_sent = sum(1 for m in messages if m.direction == "out")
        total_received = sum(1 for m in messages if m.direction == "in")
        response_rate = round((total_received / total_sent * 100) if total_sent > 0 else 0)

        msgs_data = [{
            "direction": m.direction,
            "body": m.body or "",
            "message_type": m.message_type or "—",
            "time": fmt_pst(m.created_at),
        } for m in messages]

        # Last active
        inbound = [m for m in messages if m.direction == "in"]
        if inbound:
            last_msg = max(inbound, key=lambda m: m.created_at)
            days_inactive = (now - as_utc(last_msg.created_at)).days
            last_active = fmt_pst(last_msg.created_at)
        else:
            days_inactive = 99
            last_active = "Never"

        # Activity over time — messages per day for last 30 days
        activity = {}
        for m in messages:
            if m.created_at:
                d = as_utc(m.created_at).astimezone(pst).strftime("%Y-%m-%d")
                activity[d] = activity.get(d, 0) + 1
        activity_days = []
        for i in range(29, -1, -1):
            d = (now - timedelta(days=i)).astimezone(pst).strftime("%Y-%m-%d")
            activity_days.append({"date": d, "count": activity.get(d, 0)})

        # Meals
        meals = session.query(Meal).filter(Meal.user_id == user_id).order_by(Meal.eaten_at.desc()).all()
        total_meals = len(meals)
        total_calories_logged = sum(m.calories or 0 for m in meals)
        total_protein_logged = sum(m.protein_g or 0 for m in meals)
        meals_data = [{
            "eaten_at": fmt_pst(m.eaten_at),
            "description": m.description or "",
            "calories": m.calories or 0,
            "protein_g": m.protein_g or 0,
            "carbs_g": m.carbs_g or 0,
            "fat_g": m.fat_g or 0,
            "source": m.source or "text",
            "log_type": m.log_type or "—",
            "confidence": m.confidence or "medium",
            "notes": m.notes or "",
        } for m in meals]

        # Weight logs
        weight_logs = session.query(WeightLog).filter(WeightLog.user_id == user_id).order_by(WeightLog.weighed_at.desc()).all()
        weight_data = [{
            "weighed_at": fmt_pst(w.weighed_at),
            "weight_lbs": w.weight_lbs,
            "notes": w.notes or "",
        } for w in weight_logs]
        weight_change = None
        if len(weight_logs) >= 2:
            weight_change = round(weight_logs[0].weight_lbs - weight_logs[-1].weight_lbs, 1)

        # Daily logs
        daily_logs = session.query(DailyLog).filter(DailyLog.user_id == user_id).order_by(DailyLog.date.desc()).limit(30).all()
        daily_logs_data = [{
            "date": fmt_date(dl.date),
            "sleep_hours": dl.sleep_hours or "—",
            "energy_level": dl.energy_level or "—",
            "daily_rating": dl.daily_rating or "—",
            "workout_confirmed": dl.workout_confirmed,
        } for dl in daily_logs]

        # Ratings
        ratings = [int(m.body.strip()) for m in inbound if m.body and m.body.strip() in ["1","2","3","4","5"]]
        avg_rating = round(sum(ratings) / len(ratings), 1) if ratings else None

        # Cost estimate
        user_cost = round((total_sent + total_received) * 0.015 + total_sent * 0.006, 2)

        # Onboarding label
        step_labels = {0: "Not started", 1: "Hook sent", 2: "Collecting", 3: "Complete"}
        onboarding_label = step_labels.get(user.onboarding_step or 0, f"Step {user.onboarding_step}")

        return render_template_string(USER_DETAIL_HTML,
            user=user,
            messages=msgs_data,
            total_sent=total_sent,
            total_received=total_received,
            response_rate=response_rate,
            last_active=last_active,
            days_inactive=days_inactive,
            activity_days=activity_days,
            meals=meals_data,
            total_meals=total_meals,
            total_calories_logged=total_calories_logged,
            total_protein_logged=total_protein_logged,
            weight_logs=weight_data,
            weight_change=weight_change,
            daily_logs=daily_logs_data,
            avg_rating=avg_rating,
            user_cost=user_cost,
            onboarding_label=onboarding_label,
            signed_up=fmt_date(user.created_at),
            profile_link=profile_url(user),
            coach_memory=build_memory_block(user, "admin"),
        )
    finally:
        session.close()


USER_DETAIL_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ user.name }} — Cued Admin</title>
<style>
:root{--bg:#050506;--surface:#111114;--card:#19191D;--border:#1F1F24;--text:#F5F5F7;--text2:#A1A1A6;--text3:#6E6E73;--accent:#7C6EFF;--green:#30D158;--yellow:#FFD60A;--red:#FF453A}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
a{color:var(--accent);text-decoration:none}
a:hover{opacity:.75}

/* Header */
.header{padding:20px 28px 0;border-bottom:1px solid var(--border);background:var(--surface)}
.back{font-size:12px;color:var(--text3);display:inline-flex;align-items:center;gap:5px;margin-bottom:14px}
.header-top{display:flex;align-items:flex-end;justify-content:space-between;padding-bottom:0}
.user-title h1{font-size:20px;font-weight:700;letter-spacing:-.4px}
.user-title .meta{font-size:12px;color:var(--text3);margin-top:3px}
.status-badge{font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;background:rgba(124,110,255,.15);color:var(--accent)}

/* Tabs */
.tabs{display:flex;gap:0;margin-top:16px}
.tab{padding:10px 18px;font-size:13px;font-weight:500;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.tab:hover{color:var(--text)}
.tab.active{color:var(--text);border-bottom-color:var(--accent)}

/* Content */
.content{padding:24px 28px;max-width:1100px}
.tab-pane{display:none}
.tab-pane.active{display:block}

/* Stat cards */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat-label{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:6px}
.stat-value{font-size:22px;font-weight:700;letter-spacing:-.5px}
.stat-sub{font-size:11px;color:var(--text3);margin-top:3px}

/* Activity heatmap */
.heatmap-wrap{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin-bottom:24px}
.heatmap-title{font-size:12px;color:var(--text3);text-transform:uppercase;letter-spacing:1px;margin-bottom:12px}
.heatmap{display:flex;gap:4px;align-items:flex-end;height:48px}
.heatmap-bar{flex:1;border-radius:3px 3px 0 0;min-height:4px;transition:opacity .15s;cursor:default}
.heatmap-bar:hover{opacity:.7}

/* Tables */
.table-wrap{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:20px}
.table-header{padding:14px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.table-header span{font-size:13px;font-weight:600;color:var(--text2)}
.table-header .count{font-size:12px;color:var(--text3)}
table{width:100%;border-collapse:collapse}
th{font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;padding:10px 16px;text-align:left;border-bottom:1px solid var(--border)}
td{font-size:13px;color:var(--text);padding:10px 16px;border-bottom:1px solid var(--border)}
tr:last-child td{border-bottom:none}
tr:hover td{background:rgba(255,255,255,.02)}

/* Profile grid */
.profile-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px}
.profile-section{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 20px}
.profile-section h3{font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:1.2px;margin-bottom:14px}
.prow{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-bottom:1px solid var(--border)}
.prow:last-child{border-bottom:none}
.prow .pl{font-size:12px;color:var(--text3)}
.prow .pv{font-size:13px;color:var(--text);text-align:right;max-width:60%}
.confirmed-tag{font-size:10px;color:var(--green);margin-left:6px;font-weight:600}
.memory-box{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:16px 20px;margin-bottom:20px}
.memory-box h3{font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:1.2px;margin-bottom:10px}
.memory-box p{font-size:13px;color:var(--text2);line-height:1.6;white-space:pre-wrap}

/* Conversation */
.convo-wrap{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.messages{padding:16px;display:flex;flex-direction:column;gap:10px;max-height:560px;overflow-y:auto}
.msg-row{display:flex;flex-direction:column}
.msg-row.out{align-items:flex-end}
.msg-row.in{align-items:flex-start}
.bubble{max-width:78%;padding:10px 14px;border-radius:14px;font-size:13px;line-height:1.55;word-break:break-word}
.bubble.out{background:var(--accent);color:#fff;border-radius:14px 14px 4px 14px}
.bubble.in{background:var(--surface);color:var(--text);border-radius:14px 14px 14px 4px;border:1px solid var(--border)}
.bubble-meta{font-size:10px;color:var(--text3);margin-top:3px}
.send-wrap{padding:14px 16px;border-top:1px solid var(--border);display:flex;gap:8px}
.send-wrap textarea{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:9px 12px;font-size:13px;font-family:inherit;resize:none}
.send-wrap textarea:focus{outline:none;border-color:var(--accent)}
.send-wrap button{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:9px 18px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
.send-wrap button:hover{opacity:.88}

/* Badges */
.badge{display:inline-block;font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px}
.badge.photo{background:rgba(124,110,255,.2);color:var(--accent)}
.badge.text{background:rgba(48,209,88,.15);color:var(--green)}
.badge.high{background:rgba(48,209,88,.15);color:var(--green)}
.badge.medium{background:rgba(255,214,10,.15);color:var(--yellow)}
.badge.low{background:rgba(255,69,58,.15);color:var(--red)}

/* Weight chart */
.weight-list{display:flex;flex-direction:column;gap:0}

@media(max-width:700px){
  .profile-grid{grid-template-columns:1fr}
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  .content{padding:16px}
}
</style>
</head>
<body>

<div class="header">
  <a href="/admin" class="back">← Dashboard</a>
  <div class="header-top">
    <div class="user-title">
      <h1>{{ user.name }}</h1>
      <div class="meta">{{ user.phone }} &nbsp;·&nbsp; ID {{ user.id }} &nbsp;·&nbsp; Signed up {{ signed_up }}</div>
      <div class="meta">Profile link: <a href="{{ profile_link }}" target="_blank" rel="noopener">{{ profile_link }}</a></div>
    </div>
    <div style="display:flex;align-items:center;gap:12px">
      <a href="/admin/user/{{ user.id }}/debug" style="font-size:12px">Debug view →</a>
      <span class="status-badge">{{ onboarding_label }}</span>
    </div>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('overview')">Overview</div>
    <div class="tab" onclick="showTab('profile')">Profile</div>
    <div class="tab" onclick="showTab('conversation')">Conversation</div>
    <div class="tab" onclick="showTab('meals')">Meals</div>
    <div class="tab" onclick="showTab('logs')">Weight & Logs</div>
  </div>
</div>

<div class="content">

  <!-- OVERVIEW TAB -->
  <div id="tab-overview" class="tab-pane active">
    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Messages Sent</div>
        <div class="stat-value">{{ total_sent }}</div>
        <div class="stat-sub">coach → user</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Messages Received</div>
        <div class="stat-value">{{ total_received }}</div>
        <div class="stat-sub">user → coach</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Response Rate</div>
        <div class="stat-value">{{ response_rate }}%</div>
        <div class="stat-sub">replies / coach msgs</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Meals Logged</div>
        <div class="stat-value">{{ total_meals }}</div>
        <div class="stat-sub">{{ total_calories_logged }} cal · {{ total_protein_logged }}g protein</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Avg Rating</div>
        <div class="stat-value">{% if avg_rating %}{{ avg_rating }}/5{% else %}—{% endif %}</div>
        <div class="stat-sub">daily check-ins</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Est. Spend</div>
        <div class="stat-value">${{ user_cost }}</div>
        <div class="stat-sub">API + SMS cost</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Last Active</div>
        <div class="stat-value" style="font-size:14px">{{ last_active }}</div>
        <div class="stat-sub">{{ days_inactive }}d ago</div>
      </div>
      {% if weight_change is not none %}
      <div class="stat-card">
        <div class="stat-label">Weight Change</div>
        <div class="stat-value" style="color:{% if weight_change < 0 %}var(--green){% elif weight_change > 0 %}var(--yellow){% else %}var(--text){% endif %}">
          {% if weight_change > 0 %}+{% endif %}{{ weight_change }} lbs
        </div>
        <div class="stat-sub">first → latest log</div>
      </div>
      {% endif %}
    </div>

    <div class="heatmap-wrap">
      <div class="heatmap-title">30-Day Activity</div>
      <div class="heatmap" id="heatmap"></div>
    </div>
  </div>

  <!-- PROFILE TAB -->
  <div id="tab-profile" class="tab-pane">
    <div class="profile-grid">
      <div class="profile-section">
        <h3>Basic Info</h3>
        {% for label, val in [
          ('Age', user.age), ('Gender', user.gender), ('Occupation', user.occupation),
          ('Height', (user.height_ft|string + "'" + (user.height_in|string or '0') + '"') if user.height_ft else None),
          ('Weight', (user.weight_lbs|string + ' lbs') if user.weight_lbs else None),
          ('Body Fat', (user.body_fat_pct|string + '%') if user.body_fat_pct else None),
          ('Wearable', user.wearable)
        ] %}{% if val %}
        <div class="prow"><span class="pl">{{ label }}</span><span class="pv">{{ val }}</span></div>
        {% endif %}{% endfor %}
      </div>
      <div class="profile-section">
        <h3>Goals & Training</h3>
        {% for label, val, confirmed in [
          ('Goal', user.goal, user.confirmed_goal_priority),
          ('Experience', user.experience, None),
          ('Equipment', user.equipment, None),
          ('Training Split', user.confirmed_training_split, user.confirmed_training_split),
          ('Training Days', user.confirmed_training_days, user.confirmed_training_days),
          ('Workout Time', user.confirmed_workout_time, user.confirmed_workout_time),
          ('Workout Days', user.workout_days, None),
          ('Injuries', user.injuries, None),
          ('Activity Level', user.activity_level, None),
        ] %}{% if val %}
        <div class="prow">
          <span class="pl">{{ label }}</span>
          <span class="pv">{{ val }}{% if confirmed %}<span class="confirmed-tag">✓</span>{% endif %}</span>
        </div>
        {% endif %}{% endfor %}
      </div>
      <div class="profile-section">
        <h3>Nutrition Targets</h3>
        {% for label, val, confirmed in [
          ('Calorie Target', (user.calorie_target|string + ' cal') if user.calorie_target else None, user.calorie_target),
          ('Protein Target', (user.protein_target|string + 'g') if user.protein_target else None, user.protein_target),
          ('Diet', user.diet, None),
          ('Restrictions', user.restrictions, None),
          ('Cooking', user.cooking_situation, None),
          ('Food Context', user.food_context, None),
        ] %}{% if val %}
        <div class="prow">
          <span class="pl">{{ label }}</span>
          <span class="pv">{{ val }}{% if confirmed %}<span class="confirmed-tag">✓</span>{% endif %}</span>
        </div>
        {% endif %}{% endfor %}
      </div>
      <div class="profile-section">
        <h3>Lifestyle & Sleep</h3>
        {% for label, val in [
          ('Wake Time', user.wake_time), ('Bedtime', user.sleep_time),
          ('Sleep Quality', user.sleep_quality), ('Stress Level', user.stress_level),
          ('Motivation', user.motivation), ('Obstacle', user.biggest_obstacle),
          ('Existing Tools', user.existing_tools), ('Tools Decision', user.tools_decision),
          ('Weigh-in Day', user.weigh_in_day),
        ] %}{% if val %}
        <div class="prow"><span class="pl">{{ label }}</span><span class="pv">{{ val }}</span></div>
        {% endif %}{% endfor %}
      </div>
    </div>
    {% if coach_memory %}
    <div class="memory-box">
      <h3>Coach Memory</h3>
      <p>{{ coach_memory }}</p>
    </div>
    {% endif %}
    {% if user.delivered_coaching_points %}
    <div class="memory-box">
      <h3>Delivered Coaching Points</h3>
      <p>{{ user.delivered_coaching_points }}</p>
    </div>
    {% endif %}
  </div>

  <!-- CONVERSATION TAB -->
  <div id="tab-conversation" class="tab-pane">
    <div class="convo-wrap">
      <div class="messages" id="msg-container">
        {% for m in messages %}
        <div class="msg-row {{ m.direction }}">
          <div class="bubble {{ m.direction }}">{{ m.body }}</div>
          <div class="bubble-meta">{{ m.time }}{% if m.message_type and m.message_type != '—' %} · {{ m.message_type }}{% endif %}</div>
        </div>
        {% endfor %}
        {% if not messages %}<p style="color:var(--text3);font-size:13px;text-align:center;padding:24px">No messages yet.</p>{% endif %}
      </div>
      <div class="send-wrap">
        <textarea id="msg-body" placeholder="Send a manual message as coach..." rows="2"></textarea>
        <button onclick="sendMsg()">Send</button>
      </div>
    </div>
  </div>

  <!-- MEALS TAB -->
  <div id="tab-meals" class="tab-pane">
    <div class="stats-grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:20px">
      <div class="stat-card">
        <div class="stat-label">Total Meals</div>
        <div class="stat-value">{{ total_meals }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total Calories</div>
        <div class="stat-value">{{ total_calories_logged }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Total Protein</div>
        <div class="stat-value">{{ total_protein_logged }}g</div>
      </div>
    </div>
    <div class="table-wrap">
      <div class="table-header">
        <span>Logged Meals</span>
        <span class="count">{{ total_meals }} entries</span>
      </div>
      <table>
        <thead><tr>
          <th>Time</th><th>Description</th><th>Cal</th><th>Protein</th><th>Carbs</th><th>Fat</th><th>Source</th><th>Confidence</th>
        </tr></thead>
        <tbody>
        {% for m in meals %}
        <tr>
          <td style="white-space:nowrap;color:var(--text3)">{{ m.eaten_at }}</td>
          <td>{{ m.description }}</td>
          <td>{{ m.calories }}</td>
          <td>{{ m.protein_g }}g</td>
          <td>{{ m.carbs_g }}g</td>
          <td>{{ m.fat_g }}g</td>
          <td><span class="badge {{ m.source }}">{{ m.source }}</span></td>
          <td><span class="badge {{ m.confidence }}">{{ m.confidence }}</span></td>
        </tr>
        {% endfor %}
        {% if not meals %}<tr><td colspan="8" style="text-align:center;color:var(--text3);padding:24px">No meals logged yet.</td></tr>{% endif %}
        </tbody>
      </table>
    </div>
  </div>

  <!-- WEIGHT & LOGS TAB -->
  <div id="tab-logs" class="tab-pane">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <div class="table-wrap">
          <div class="table-header">
            <span>Weight Log</span>
            <span class="count">{{ weight_logs|length }} entries</span>
          </div>
          <table>
            <thead><tr><th>Date</th><th>Weight</th><th>Notes</th></tr></thead>
            <tbody>
            {% for w in weight_logs %}
            <tr>
              <td style="color:var(--text3);white-space:nowrap">{{ w.weighed_at }}</td>
              <td style="font-weight:600">{{ w.weight_lbs }} lbs</td>
              <td style="color:var(--text3)">{{ w.notes }}</td>
            </tr>
            {% endfor %}
            {% if not weight_logs %}<tr><td colspan="3" style="text-align:center;color:var(--text3);padding:20px">No weight logs yet.</td></tr>{% endif %}
            </tbody>
          </table>
        </div>
      </div>
      <div>
        <div class="table-wrap">
          <div class="table-header">
            <span>Daily Logs</span>
            <span class="count">Last 30 days</span>
          </div>
          <table>
            <thead><tr><th>Date</th><th>Sleep</th><th>Energy</th><th>Rating</th><th>Trained</th></tr></thead>
            <tbody>
            {% for dl in daily_logs %}
            <tr>
              <td style="color:var(--text3);white-space:nowrap">{{ dl.date }}</td>
              <td>{% if dl.sleep_hours != '—' %}{{ dl.sleep_hours }}h{% else %}—{% endif %}</td>
              <td>{% if dl.energy_level != '—' %}{{ dl.energy_level }}/5{% else %}—{% endif %}</td>
              <td>{% if dl.daily_rating != '—' %}{{ dl.daily_rating }}/5{% else %}—{% endif %}</td>
              <td>{% if dl.workout_confirmed %}<span style="color:var(--green)">✓</span>{% else %}—{% endif %}</td>
            </tr>
            {% endfor %}
            {% if not daily_logs %}<tr><td colspan="5" style="text-align:center;color:var(--text3);padding:20px">No daily logs yet.</td></tr>{% endif %}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  </div>

</div><!-- /content -->

<script>
const userId = {{ user.id }};
const activityDays = {{ activity_days | tojson }};

function showTab(name) {
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'conversation') {
    setTimeout(() => {
      const c = document.getElementById('msg-container');
      if (c) c.scrollTop = c.scrollHeight;
    }, 50);
  }
}

async function sendMsg() {
  const body = document.getElementById('msg-body').value.trim();
  if (!body) return;
  if (window._sendInFlight) return;   // double-click / double-handler guard
  window._sendInFlight = true;
  try {
    await fetch('/admin/send', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'user_id=' + userId + '&body=' + encodeURIComponent(body)
    });
    document.getElementById('msg-body').value = '';
    location.reload();
  } finally {
    window._sendInFlight = false;
  }
}

// Build heatmap
(function() {
  const hm = document.getElementById('heatmap');
  if (!hm) return;
  const max = Math.max(...activityDays.map(d => d.count), 1);
  activityDays.forEach(d => {
    const bar = document.createElement('div');
    bar.className = 'heatmap-bar';
    const pct = d.count / max;
    const h = Math.max(4, Math.round(pct * 48));
    bar.style.height = h + 'px';
    bar.style.background = d.count === 0 ? 'var(--border)' : `rgba(124,110,255,${0.2 + pct * 0.8})`;
    bar.title = d.date + ': ' + d.count + ' msg' + (d.count !== 1 ? 's' : '');
    hm.appendChild(bar);
  });
})();

// Scroll conversation to bottom on load if that tab is active
const msgC = document.getElementById('msg-container');
if (msgC) msgC.scrollTop = msgC.scrollHeight;
</script>
</body>
</html>
"""


# ─── Health Check ───────────────────────────────────
@app.route("/")
def health():
    return jsonify({"status": "ok", "app": "cued", "version": "0.1.0"})


# ─── HTML Templates ─────────────────────────────────
SIGNUP_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Cued — Sign Up</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, system-ui, sans-serif; background: #18181B; color: #FAFAFA; padding: 20px; }
        .container { max-width: 480px; margin: 48px auto; padding-bottom: 60px; }
        h1 { font-size: 28px; font-weight: 700; margin-bottom: 4px; letter-spacing: -.5px; }
        .sub { color: #A1A1AA; margin-bottom: 10px; font-size: 15px; }
        .intro { color: #71717A; font-size: 13px; margin-bottom: 36px; line-height: 1.6; }
        .section-label { font-size: 10px; font-weight: 700; color: #6D5CFF; text-transform: uppercase; letter-spacing: 2px; margin-top: 32px; margin-bottom: 14px; padding-top: 18px; border-top: 1px solid #27272A; }
        .section-label:first-of-type { border-top: none; margin-top: 0; padding-top: 0; }
        label { display: block; font-size: 13px; color: #A1A1AA; margin-bottom: 5px; margin-top: 16px; }
        input, select { width: 100%; padding: 12px 14px; background: #27272A; border: 1px solid #3F3F46; border-radius: 8px; color: #FAFAFA; font-size: 15px; font-family: inherit; }
        input:focus, select:focus { outline: none; border-color: #6D5CFF; }
        select option { background: #27272A; }
        .row { display: flex; gap: 12px; }
        .row > div { flex: 1; }
        /* Pill toggle groups */
        .pill-group { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
        .pill-group label { display: flex; align-items: center; justify-content: center; gap: 6px; background: #27272A; border: 1px solid #3F3F46; border-radius: 20px; padding: 9px 16px; cursor: pointer; font-size: 13px; color: #A1A1AA; margin: 0; transition: all .15s; white-space: nowrap; }
        .pill-group label:has(input:checked) { border-color: #6D5CFF; background: rgba(109,92,255,.15); color: #FAFAFA; }
        .pill-group input { display: none; }
        /* Consent block */
        .consent-box { background: #27272A; border: 1px solid #3F3F46; border-radius: 8px; padding: 14px 16px; margin-top: 8px; }
        .consent-box label { display: flex; align-items: flex-start; gap: 10px; cursor: pointer; margin: 0; color: #FAFAFA; font-size: 13px; line-height: 1.55; }
        .consent-box input[type="checkbox"] { width: 16px; height: 16px; margin-top: 1px; flex-shrink: 0; accent-color: #6D5CFF; cursor: pointer; }
        #consent-error { display: none; color: #EF4444; font-size: 12px; margin-top: 6px; }
        button[type="submit"] { width: 100%; padding: 14px; background: #6D5CFF; color: #fff; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; cursor: pointer; margin-top: 32px; transition: background .15s; }
        button[type="submit"]:hover { background: #5B4DE6; }
        button[type="submit"]:disabled { opacity: .6; cursor: not-allowed; }
        #result { margin-top: 16px; padding: 12px 14px; border-radius: 8px; display: none; font-size: 13px; }
    </style>
</head>
<body>
<div class="container">
    <h1>cued</h1>
    <p class="sub">Your AI coach, right in your messages.</p>
    <p class="intro">Takes 60 seconds. Your coach will handle the rest over text.</p>

    <form id="signup" onsubmit="return handleSubmit(event)">

        <div class="section-label">You</div>

        <div class="row">
            <div>
                <label>First name</label>
                <input name="name" required placeholder="Alex">
            </div>
            <div>
                <label>Age</label>
                <input name="age" type="number" min="13" max="99" placeholder="21">
            </div>
        </div>

        <label>Phone number</label>
        <input name="phone" required placeholder="(555) 867-5309" inputmode="tel">

        <label>Gender</label>
        <select name="gender">
            <option value="male">Male</option>
            <option value="female">Female</option>
            <option value="non_binary">Non-binary</option>
            <option value="prefer_not_to_say" selected>Prefer not to say</option>
        </select>

        <div class="section-label">Your Goal</div>

        <label>What are you working toward?</label>
        <div class="pill-group" id="goal-group">
            <label><input type="checkbox" name="goals" value="fat_loss"> Lose fat</label>
            <label><input type="checkbox" name="goals" value="muscle_building"> Build muscle</label>
            <label><input type="checkbox" name="goals" value="strength"> Get stronger</label>
            <label><input type="checkbox" name="goals" value="general_fitness"> General fitness</label>
        </div>

        <label style="margin-top:20px;">Biggest obstacle</label>
        <select name="biggest_obstacle">
            <option value="" selected disabled>Pick one</option>
            <option value="consistency">Staying consistent</option>
            <option value="nutrition">Nutrition / what to eat</option>
            <option value="knowledge">Not knowing what to do</option>
            <option value="time">Not enough time</option>
            <option value="motivation">Motivation / accountability</option>
            <option value="injuries">Injuries holding me back</option>
        </select>

        <div class="section-label">Training</div>

        <label>Experience level</label>
        <div class="pill-group" id="exp-group">
            <label><input type="radio" name="experience" value="none"> Just starting out</label>
            <label><input type="radio" name="experience" value="beginner"> Under 6 months</label>
            <label><input type="radio" name="experience" value="intermediate" checked> 6 months – 2 years</label>
            <label><input type="radio" name="experience" value="advanced"> 2+ years</label>
        </div>

        <label style="margin-top:20px;">Equipment access</label>
        <div class="pill-group" id="equip-group">
            <label><input type="radio" name="equipment" value="full_gym" checked> Full gym</label>
            <label><input type="radio" name="equipment" value="limited_gym"> Limited gym</label>
            <label><input type="radio" name="equipment" value="home_gym"> Home gym</label>
            <label><input type="radio" name="equipment" value="bodyweight"> Bodyweight only</label>
        </div>

        <div class="section-label">Consent</div>

        <div class="consent-box">
            <label>
                <input type="checkbox" name="sms_consent" id="sms_consent">
                <span>I agree to receive automated SMS coaching messages from Cued. Message &amp; data rates may apply. Reply STOP at any time to unsubscribe.</span>
            </label>
        </div>
        <div id="consent-error">You must agree to receive SMS messages to use Cued.</div>

        <button type="submit" id="submit-btn">Start coaching →</button>
    </form>
    <div id="result"></div>
</div>

<script>
async function handleSubmit(e) {
    e.preventDefault();

    // Validate SMS consent
    if (!document.getElementById('sms_consent').checked) {
        const err = document.getElementById('consent-error');
        const box = document.querySelector('.consent-box');
        err.style.display = 'block';
        box.style.borderColor = '#EF4444';
        box.scrollIntoView({ behavior: 'smooth', block: 'center' });
        return false;
    }
    document.getElementById('consent-error').style.display = 'none';
    document.querySelector('.consent-box').style.borderColor = '#3F3F46';

    // Validate at least one goal selected
    const goals = Array.from(document.querySelectorAll('input[name="goals"]:checked')).map(i => i.value);
    if (!goals.length) {
        document.getElementById('goal-group').scrollIntoView({ behavior: 'smooth', block: 'center' });
        alert('Please select at least one goal.');
        return false;
    }

    const btn = document.getElementById('submit-btn');
    btn.disabled = true;
    btn.textContent = 'Sending…';

    const form = e.target;
    const fd = new FormData(form);

    // Build the payload with exactly the 8 fields the backend expects
    const payload = {
        name: (fd.get('name') || '').trim(),
        phone: (fd.get('phone') || '').trim(),
        age: fd.get('age') || '',
        gender: fd.get('gender') || 'prefer_not_to_say',
        experience: fd.get('experience') || 'none',
        goal: goals.join(','),
        biggest_obstacle: fd.get('biggest_obstacle') || '',
        equipment: fd.get('equipment') || 'full_gym',
        sms_consent: true,
    };

    try {
        const res = await fetch('/signup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await res.json();

        if (data.status === 'ok') {
            document.querySelector('.container').innerHTML = `
                <div style="text-align:center;padding:80px 0 60px;">
                    <div style="font-size:52px;margin-bottom:24px;">✓</div>
                    <h1 style="font-size:26px;margin-bottom:12px;letter-spacing:-.4px;">You're in, ${data.name || 'friend'}.</h1>
                    <p style="color:#A1A1AA;font-size:15px;line-height:1.7;max-width:340px;margin:0 auto 20px;">
                        Your coach is putting together your plan. You'll get a text shortly with a few quick questions to get things dialed in.
                    </p>
                    <p style="color:#6E6E73;font-size:13px;">Keep your phone nearby.</p>
                </div>`;
            window.scrollTo(0, 0);
        } else {
            const el = document.getElementById('result');
            el.style.display = 'block';
            el.style.background = data.status === 'exists' ? 'rgba(109,92,255,.15)' : 'rgba(220,38,38,.15)';
            el.style.color = data.status === 'exists' ? '#A78BFA' : '#FCA5A5';
            el.textContent = data.message;
            btn.disabled = false;
            btn.textContent = 'Start coaching →';
        }
    } catch (err) {
        const el = document.getElementById('result');
        el.style.display = 'block';
        el.style.background = 'rgba(220,38,38,.15)';
        el.style.color = '#FCA5A5';
        el.textContent = 'Something went wrong. Please try again.';
        btn.disabled = false;
        btn.textContent = 'Start coaching →';
    }
}
</script>
</body>
</html>
"""

_UNUSED_OLD_ADMIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Baseline Admin</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, system-ui, sans-serif; background: #0F0F10; color: #FAFAFA; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; }
        h1 { margin-bottom: 24px; }
        .user-card { background: #18181B; border-radius: 12px; padding: 20px; margin-bottom: 20px; }
        .user-name { font-size: 18px; font-weight: 600; }
        .user-meta { color: #A1A1AA; font-size: 13px; margin-top: 4px; }
        .messages { margin-top: 16px; max-height: 400px; overflow-y: auto; }
        .msg { padding: 8px 12px; margin: 4px 0; border-radius: 8px; font-size: 14px; max-width: 85%; }
        .msg.out { background: #6D5CFF; margin-left: auto; text-align: right; color: white; }
        .msg.in { background: #27272A; }
        .msg .time { font-size: 11px; color: #71717A; margin-top: 2px; }
        .msg.out .time { color: #C4B5FF; }
        .send-form { display: flex; gap: 8px; margin-top: 12px; }
        .send-form input { flex: 1; padding: 10px; background: #27272A; border: 1px solid #3F3F46; border-radius: 8px; color: #FAFAFA; font-size: 14px; }
        .send-form button { padding: 10px 20px; background: #6D5CFF; color: white; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Baseline Admin</h1>
        {% for ud in users %}
        <div class="user-card">
            <div class="user-name">{{ ud.user.name }}</div>
            <div class="user-meta">{{ ud.user.phone }} · {{ ud.user.goal }} · {{ ud.user.experience }} · wake {{ ud.user.wake_time }}</div>
            <div class="messages">
                {% for m in ud.messages %}
                <div class="msg {{ m.direction }}">
                    {{ m.body }}
                    <div class="time">{{ m.created_at.strftime('%b %d %I:%M %p') }} · {{ m.message_type }}</div>
                </div>
                {% endfor %}
            </div>
            <form class="send-form" onsubmit="return adminSend(event, {{ ud.user.id }})">
                <input name="body" placeholder="Manual override message...">
                <button type="submit">Send</button>
            </form>
        </div>
        {% endfor %}
        {% if not users %}
        <p style="color: #A1A1AA;">No active users yet. Share your signup link!</p>
        {% endif %}
    </div>
    <script>
    async function adminSend(e, userId) {
        e.preventDefault();
        const body = e.target.body.value;
        if (!body.trim() || window._sendInFlight) return false;   // double-submit guard
        window._sendInFlight = true;
        try {
            await fetch('/admin/send', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'user_id=' + userId + '&body=' + encodeURIComponent(body)
            });
            e.target.body.value = '';
            location.reload();
        } finally {
            window._sendInFlight = false;
        }
        return false;
    }
    </script>
</body>
</html>
"""


# ─── App Startup ────────────────────────────────────
if __name__ == "__main__":
    logger.info("Starting Cued...")
    start_scheduler()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
