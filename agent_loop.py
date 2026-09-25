"""
Phase 2 — the single agent loop (inbound only).

One model call per inbound, one voice, full context. Replaces
classifier→specialists→merge behind SINGLE_AGENT_LOOP_ENABLED; the webhook falls
back to the legacy pipeline (orchestrator.route_message) on any exception.

Context is UNIFIED — all memory categories rendered (not the per-agent slice), so
a fact told in one domain is available in another (fixes failure 1). Safety
constraints stay universal via render_categories(include_safety_universal=True).
Model: claude-opus-4-8 (config.AGENT_LOOP_MODEL), no sampling params, adaptive
thinking + low effort held constant (Opus 4.8 rejects temperature/budget_tokens;
switching thinking modes would break the messages cache — see INVESTIGATION §5).
"""

from __future__ import annotations

import logging
import re
import os
from datetime import datetime, timezone

import anthropic
import config
from cost_tracking import track
from memory import render_categories, CATEGORIES, render_body_line, render_dietary_line
from models import get_session, User, Message, Workout, Meal, active
from events import todays_events
from split_pointer import get_split_pointer
from llm_client import make_client

logger = logging.getLogger("cued.agent_loop")
client = make_client()

_IDENTITY_PATH = os.path.join(os.path.dirname(__file__), "prompts", "identity.md")
_VOICE_PATH = os.path.join(os.path.dirname(__file__), "prompts", "voice.md")
_identity_cache: str | None = None
_voice_cache: str | None = None

_MEAL_ESTIMATION_PATH = os.path.join(os.path.dirname(__file__), "prompts", "meal_estimation.md")
_meal_estimation_cache: str | None = None

_MEAL_ROUTING_PATH = os.path.join(os.path.dirname(__file__), "prompts", "meal_routing.md")
_meal_routing_cache: str | None = None


def identity_prompt() -> str:
    """prompts/identity.md — the ONE identity (a friend at Berkeley who knows
    training and food cold). Loaded first on every surface: coach loop, heartbeat
    (via _voice_prompt) and onboarding (directly — it never gets the tool rules)."""
    global _identity_cache
    if _identity_cache is None:
        with open(_IDENTITY_PATH, "r", encoding="utf-8") as f:
            _identity_cache = f.read()
    return _identity_cache


def _voice_prompt() -> str:
    """identity.md + voice.md as ONE stable, cacheable prefix. The heartbeat
    composes its system from this same string, so both surfaces stay one person."""
    global _voice_cache
    if _voice_cache is None:
        with open(_VOICE_PATH, "r", encoding="utf-8") as f:
            _voice_cache = identity_prompt() + "\n\n---\n\n" + f.read()
    return _voice_cache


def _meal_estimation_prompt() -> str:
    global _meal_estimation_cache
    if _meal_estimation_cache is None:
        with open(_MEAL_ESTIMATION_PATH, "r", encoding="utf-8") as f:
            _meal_estimation_cache = f.read()
    return _meal_estimation_cache


def _meal_routing_prompt() -> str:
    global _meal_routing_cache
    if _meal_routing_cache is None:
        with open(_MEAL_ROUTING_PATH, "r", encoding="utf-8") as f:
            _meal_routing_cache = f.read()
    return _meal_routing_cache


def _known_gaps(user) -> list[str]:
    """What the coach does NOT know — models ask good follow-ups when they see gaps."""
    gaps = []
    if not (user.confirmed_workout_time or user.workout_time):
        gaps.append("workout time is unconfirmed")
    if not get_split_pointer(user.id):
        gaps.append("no known split day yet — hasn't logged a workout")
    from food_logger import known_gap_line
    fl = known_gap_line(user)
    if fl:
        gaps.append(fl)
    return gaps


def build_loop_context(user, session) -> str:
    """The VOLATILE per-user block, injected AFTER the cached voice prefix.

    UNIFIED memory render (all categories + universal safety) is the failure-1 fix:
    the single loop sees every domain's facts, not a per-agent slice.
    """
    profile = user.user_profile_memory or {}
    parts: list[str] = []

    # Render every timestamp in the user's LOCAL zone (burn-in fix) — the model has no
    # local clock to reason "tomorrow/later" against otherwise, and bare UTC is 7h ahead.
    from timefmt import render_time, render_date, now_anchor, local_day_bounds, to_local
    _local = config.CONTEXT_LOCAL_TIME_ENABLED

    def _t(dt, *, relative=True):
        return render_time(dt, user, relative=relative) if _local else f"{dt:%H:%M}Z"

    def _d(dt):
        return render_date(dt, user) if _local else f"{dt:%m-%d}"

    # 1. Unified memory (ALL categories, safety appended universally).
    mem_text, _ids = render_categories(profile, CATEGORIES, include_safety_universal=True)
    if mem_text:
        parts.append(f"## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n{mem_text}")

    # 2. Typed-column profile (source of truth for body/diet/targets).
    if user.profile_summary:
        parts.append(f"## PROFILE\n{user.profile_summary}")
    body = render_body_line(user)
    diet = render_dietary_line(user)
    if body:
        parts.append(body)
    if diet:
        parts.append(diet)
    if (user.food_context or "").strip():
        parts.append(f"Food context: {user.food_context.strip()}")
    if config.SAVE_MENU_TOOL_ENABLED:
        from saved_menus import render_menus_block
        menus = render_menus_block(getattr(user, "saved_menus", None))
        if menus:
            parts.append(menus)

    # 3. Events, lifecycle-aware (memory-freshness Fix 1). Upcoming vs passed is a
    # FACT computed from the row's datetimes — the model must never infer it from a
    # timeless string (that's how a Jul 31 interview resurfaced Aug 3 as upcoming).
    from events import upcoming_events, recently_passed_events, event_end
    _now_utc = datetime.now(timezone.utc).replace(tzinfo=None)

    def _is_all_day(e):
        if getattr(e, "all_day", False):     # synced calendar all-day events set this
            return True
        if not e.occurred_at or not e.ends_at:
            return False
        s_l, e_l = to_local(e.occurred_at, user), to_local(e.ends_at, user)
        return (s_l.hour, s_l.minute) == (0, 0) and (e_l.hour, e_l.minute) == (23, 59)

    # Calendar-ish sources (log_event, gcal, bcourses) carry a title in raw_text and
    # render the same way; only the regex floor (went_to_gym/in_class) uses event_type.
    _CAL_SRC = ("model", "gcal", "bcourses", "canvas")

    # 3a. Today's events (local-day). Regex floor (went_to_gym / in_class) AND
    # model-logged dated schedule items (log_event) — the latter carry a description
    # in raw_text + a start/end window, so a timely check-in can reference them.
    evs = todays_events(user.id)
    if evs:
        def _fmt_event(e):
            if e.source in _CAL_SRC:
                label = (e.raw_text or e.title or e.event_type or "").strip()
                if _is_all_day(e):
                    span = " (all day)"
                elif e.occurred_at:
                    span = f" {_t(e.occurred_at, relative=False)}" + (
                        f"–{_t(e.ends_at, relative=False)}" if e.ends_at else "")
                elif e.ends_at:
                    span = f" until {_t(e.ends_at, relative=False)}"
                else:
                    span = ""
            else:
                label = e.event_type
                span = f" until {_t(e.ends_at, relative=False)}" if e.ends_at else ""
            # [id N] so the model can reference/correct/delete an event via manage_log,
            # the same way it can for meals and workouts.
            line = f"[id {e.id}] {label}{span}"
            # A same-day 2:15pm event at 6pm must not read like one at 9pm.
            if e.source in _CAL_SRC and e.occurred_at and event_end(e) < _now_utc:
                line += (" — PASSED (already happened; never treat as upcoming, "
                         "at most one natural follow-up)")
            return line
        ev_txt = "; ".join(_fmt_event(e) for e in evs)
        parts.append(f"## TODAY'S EVENTS\n{ev_txt}")

    # 3b. Forward visibility — the reader gap: an event for Friday used to be
    # invisible until Friday, so "interview tomorrow, get some sleep" was impossible.
    ups = upcoming_events(user.id)
    if ups:
        def _fmt_upcoming(e):
            label = (e.raw_text or e.event_type or "").strip()
            when = _d(e.occurred_at)
            if not _is_all_day(e):
                when += f" {_t(e.occurred_at, relative=False)}"
                # Include the end the same way today's events do — otherwise a synced
                # calendar event's end time (ends_at) never reaches the coach, so "when
                # does my friday quiz end" reads as "the calendar has no end time" when it
                # does (live: founder 2026-09-24).
                if e.ends_at:
                    when += f"–{_t(e.ends_at, relative=False)}"
            return f"[id {e.id}] {label} — {when}"
        parts.append("## UPCOMING EVENTS (next 7 days — logged ahead of time; you may "
                     "reference or prep them)\n"
                     + "\n".join(_fmt_upcoming(e) for e in ups))

    # 3c. Bounded follow-up window (48h), then the event retires. Once-ness comes
    # from the anti-repetition machinery (tick history / RECENT PROACTIVE), and
    # manage_log delete is the explicit retire path — no stored lifecycle state.
    passed = recently_passed_events(user.id)
    if passed:
        parts.append("## RECENTLY PASSED (happened, not followed up on yet — at most "
                     "ONE natural \"how'd it go\", then let it go; NEVER mention as "
                     "still upcoming)\n"
                     + "\n".join(f"[id {e.id}] {(e.raw_text or e.event_type or '').strip()}"
                                 f" — {_d(e.occurred_at)}" for e in passed))

    # 4. Their split as a day cycle (their own grouping when they gave one), then the
    # pointer WITH provenance — the model hedges on inferred days. Live 2026-09-22
    # (user 43): the loop never saw the split at all, so "bro split" was invisible.
    from split_pointer import cycle_for
    from workouts.templates import day_label
    cycle = cycle_for(user)
    split_label = (user.current_split or user.confirmed_training_split or "").strip()
    if cycle:
        parts.append(
            f"## SPLIT\n{split_label or 'their days'}: " + " → ".join(day_label(d) for d in cycle)
            + ". start_workout_session picks the next day in this order unless they name one "
              "(template_key accepts these day keys: " + ", ".join(cycle) + ")."
        )
    elif split_label and split_label.lower() not in ("none",):
        parts.append(
            f"## SPLIT\n{split_label} — but WHICH days they run isn't saved, so the card can't "
            f"be built. When they mention their days (\"chest and bis, back and tris, legs\"), "
            f"call save_routine with split_days."
        )
    p = get_split_pointer(user.id)
    if p:
        parts.append(
            f"## SPLIT POINTER\nlast completed: {day_label(p['day'])} ({p['source']}). "
            f"Derive today's likely day from this and the split; if the source is "
            f"'inferred', hedge (ask/confirm) rather than assert."
        )
    # What they've told us they lift — the first card's numbers come from this. When
    # nothing is on file the card is estimated from their stats; a stated weight in
    # conversation ("i bench 135") belongs in set_lift_anchors, not remember.
    try:
        from workouts.calibrate import describe_anchors
        anchors_line = describe_anchors(user)
    except Exception:  # noqa: BLE001
        anchors_line = None
    if anchors_line:
        parts.append(f"## LIFTS THEY'VE STATED\n{anchors_line} — a new number they say replaces it (set_lift_anchors).")
    elif config.START_WORKOUT_TOOL_ENABLED and (user.equipment or "").strip().lower() not in ("bodyweight", "none", "no_equipment"):
        parts.append("## LIFTS THEY'VE STATED\nnone yet — if they mention what they bench / squat / "
                     "deadlift / press ('i bench 135'), set_lift_anchors so their card starts there.")
    # Whether the card has ever rendered on their phone (= the iMessage extension is
    # installed). Drives the honest answer to "what is this" / "it won't open".
    try:
        from workouts.card_setup import context_line as _card_line
        card_line = _card_line(user)
    except Exception:  # noqa: BLE001
        card_line = None
    if card_line:
        parts.append(card_line)

    # 5. Coaching summary + delivered points.
    if (user.coaching_summary or "").strip():
        parts.append(f"## COACHING SUMMARY\n{user.coaching_summary.strip()}")
    if (user.delivered_coaching_points or "").strip():
        parts.append(f"## ALREADY TOLD THEM (don't repeat)\n{user.delivered_coaching_points.strip()}")

    # 5b. Recent episodic notes (Phase 5) — non-fitness life context worth following
    # up on. The heartbeat's raw material ("how'd the midterm go"); the reactive loop
    # sees it too. Flag-gated; distinct ground from the coaching summary above.
    if config.EPISODIC_ENABLED:
        from episodic import recent_episodic
        notes = recent_episodic(user.id)
        if notes:
            nl = "\n".join(f"- {_d(n.occurred_on)}: {n.text}" for n in notes)
            parts.append(f"## RECENT LIFE CONTEXT (personal — follow up naturally)\n{nl}")

    # 5c. Connected integrations — the one-line status, NEVER a token. Lets the
    # coach know what it can see (calendar, strava) and mention a disconnect once.
    if (config.GCAL_ENABLED or config.STRAVA_READ_ENABLED or config.BCOURSES_ENABLED
            or config.CANVAS_ENABLED or config.GOOGLE_HEALTH_ENABLED):
        try:
            from integrations.base import status_line
            sl = status_line(user.id)
            if sl:
                parts.append(f"## INTEGRATIONS\n{sl}")
        except Exception:
            logger.exception("INTEGRATIONS_STATUS_FAILED user=%s", user.id)

    # 6. Recent conversation window (reuse the watermark boundary — no overlap with summary).
    watermark = user.last_compressed_message_id or 0
    msgs = (session.query(Message)
            .filter(Message.user_id == user.id, Message.id > watermark)
            .order_by(Message.created_at.desc())
            .limit(config.CONVERSATION_HISTORY_LIMIT)
            .all())
    msgs.reverse()
    if msgs:
        def _line(m):
            if m.direction == "out":
                return f"Coach: {m.body}"
            # Their iMessages carry a ref the react/reply tools can target.
            tag = f" [m{m.id}]" if (m.channel == "imessage" and m.provider_sid) else ""
            return f"{user.name}{tag}: {m.body}"
        window = "\n".join(_line(m) for m in msgs)
        parts.append(f"## RECENT CONVERSATION\n{window}")

    # 7. Today's logged meals (ACTIVE only) — with short IDs + macros. This is the
    # "read" for read-before-write: the model sees what's already logged and decides
    # log vs skip vs a legitimate second serving, instead of a deterministic dedup
    # silently dropping real calories. It also lets the model reference/correct an
    # entry by id via manage_log.
    _start, _end = local_day_bounds(user)   # ONE shared local-day window (timefmt)
    meals = (active(session, Meal, user_id=user.id)
             .filter(Meal.eaten_at >= _start, Meal.eaten_at < _end)
             .order_by(Meal.eaten_at).all())
    if meals:
        # Provenance markers: a guessed portion is the row a correction lands on; an
        # app-sourced row is printed truth a screenshot re-send edits, never re-adds.
        def _prov(m):
            if m.source == "app":
                return " (from their app)"
            return " (portion guessed)" if m.confidence == "low" else ""
        ml = "\n".join(
            f"[id {m.id}] {_t(m.eaten_at)} {m.description} — {m.calories or 0}cal/{m.protein_g or 0}g protein{_prov(m)}"
            for m in meals)
        parts.append("## TODAY'S LOGGED MEALS (already recorded — reference by id; do not "
                     "double-log the same serving; a genuine second serving is fine)\n" + ml)

    # Code-computed totals — the model must NOT re-add these (LLM arithmetic drifts).
    # Authoritative source: the SUM over the already-fetched active meals (soft-delete
    # filtered via active(), local-day windowed). ALWAYS rendered — even at 0 — so the
    # first meal of a new day reads against 0, and the model never falls back to a stale
    # running total from earlier in the thread (the live 2026-09-17/18 bug: yesterday's
    # total, still in RECENT CONVERSATION, got carried across midnight). Denormalized
    # user.calories_today counters exist for legacy use but deliberately do NOT reach here.
    tot_cal = sum(m.calories or 0 for m in meals)
    tot_pro = sum(m.protein_g or 0 for m in meals)
    tot_carb = sum(m.carbs_g or 0 for m in meals)
    tot_fat = sum(m.fat_g or 0 for m in meals)
    lines = [f"calories: {tot_cal} | protein: {tot_pro}g | carbs: {tot_carb}g | fat: {tot_fat}g"]
    if user.calorie_target:
        lines.append(f"calories remaining vs target: {user.calorie_target - tot_cal} "
                     f"({tot_cal}/{user.calorie_target})")
    if user.protein_target:
        lines.append(f"protein remaining vs target: {user.protein_target - tot_pro}g "
                     f"({tot_pro}/{user.protein_target}g)")
    parts.append(
        "## TODAY'S TOTALS (authoritative — the ONLY source for today's running total; "
        "it resets to 0 at local midnight)\n" + "\n".join(lines) +
        "\nWhen you state the day's total, use THESE numbers as they are — read them here, "
        "don't recompute or add meals up yourself, and don't carry forward a total from "
        "earlier in the thread (it may be a previous day). Just after you log or edit a meal "
        "THIS turn, the tool result gives you the updated day total — use that number "
        "instead, since this block was built before the change. State the number plainly, as "
        "your own knowledge; NEVER say \"quote from context\", \"per the totals\", or "
        "otherwise announce that you're reading it — that narration is a bug, not a reply.")

    # 7b. YESTERDAY's meals — ids for corrections, never for today's math. Live
    # 2026-09-19: the muffin she disputed was yesterday's row; with only today's ids in
    # context the coach re-estimated it out loud and had nothing to edit. A past day is
    # reference-only: its total is stated so it can't be re-summed into today.
    from datetime import timedelta as _td
    _ystart = _start - _td(days=1)
    ymeals = (active(session, Meal, user_id=user.id)
              .filter(Meal.eaten_at >= _ystart, Meal.eaten_at < _start)
              .order_by(Meal.eaten_at).all())
    if ymeals:
        yl = "\n".join(f"[id {m.id}] {m.description} — {m.calories or 0}cal/{m.protein_g or 0}g protein"
                       for m in ymeals)
        ycal = sum(m.calories or 0 for m in ymeals)
        ypro = sum(m.protein_g or 0 for m in ymeals)
        parts.append("## YESTERDAY'S LOGGED MEALS (a PAST day — reference only; its total was "
                     f"{ycal}cal/{ypro}g; NEVER add any of these into today's total; if a "
                     "re-estimate changes one, edit it by id with manage_log before you quote "
                     "the new number)\n" + yl)

    # 8. Recent training log (active only).
    workouts = (active(session, Workout, user_id=user.id)
                .order_by(Workout.date.desc()).limit(5).all())
    if workouts:
        wl = "\n".join(f"[id {w.id}] {_d(w.date)} ({w.workout_type})" for w in workouts)
        parts.append(f"## RECENT WORKOUTS\n{wl}")

    # 7c. Logger bridge — a coexisting user's empty day is not an unlogged day
    # (food_logger.context_block; everything in it is code-computed).
    try:
        from food_logger import context_block as _fl_block
        _flb = _fl_block(user, session)
        if _flb:
            parts.append(_flb)
    except Exception as e:  # noqa: BLE001
        logger.warning("FOOD_LOGGER_CONTEXT_FAILED user=%s err=%s", user.id, e)

    # 7d. Reminders code WILL send / sent today (reminders.context_block — the same
    # block the heartbeat sees). Live 2026-09-23: without it the loop had no idea
    # Alex's tue/thu run ping existed and planned to "set the standing reminder" again.
    try:
        from reminders import context_block as _rem_block
        _rb = _rem_block(user, session)
        if _rb:
            parts.append(_rb)
    except Exception as e:  # noqa: BLE001
        logger.warning("REMINDER_CONTEXT_FAILED user=%s err=%s", user.id, e)

    # 8. Known gaps + follow-up permission.
    gaps = _known_gaps(user)
    if gaps:
        parts.append(
            "## KNOWN GAPS\nYou don't currently know: " + "; ".join(gaps) +
            ". If one matters to this reply, you may ask at most ONE follow-up."
        )

    # 9. Their profile page — a fact the code knows, so the coach never has to
    # "remember" it. Live 2026-09-11: "can you send me that link again to my
    # profile" → "i don't have a profile link to send you tbh" — the kickoff that
    # sent it had rolled to the edge of the history window. This is the ONE link
    # the coach may always send.
    if getattr(user, "phone", None):
        # profile_page.profile_url(user) is the single link builder (PR #34: a signed
        # token, the phone number never in the URL) — the same one the kickoff sends.
        from profile_page import profile_url as _profile_url
        _link = _profile_url(user)
        parts.append("## THEIR PROFILE PAGE\n"
                     f"{_link}\n"
                     "This is their own profile/settings page. When they ask for their profile, "
                     "their link, or want to double-check what you have on them, send this URL "
                     "(the one link you may always send). Never say you don't have it.")

    # Series §1.5: what they have at home (receipts / text), so dinner suggestions
    # prefer it. Flag-gated inside pantry_context.
    if (getattr(user, "onboarding_step", 0) or 0) >= 3:
        try:
            from receipts import pantry_context
            _pc = pantry_context(user.id)
            if _pc:
                parts.append(_pc)
        except Exception as e:  # noqa: BLE001
            logger.warning("PANTRY_CONTEXT_FAILED user=%s err=%s", user.id, e)

    # Adaptive targets: the weight trend, and today's cycle result if there is one.
    if (getattr(user, "onboarding_step", 0) or 0) >= 3:
        try:
            from adaptive_targets import weight_context, todays_adjustment_context
            for _blk in (weight_context(user, session), todays_adjustment_context(user, session)):
                if _blk:
                    parts.append(_blk)
        except Exception as e:  # noqa: BLE001
            logger.warning("ADAPTIVE_CONTEXT_FAILED user=%s err=%s", user.id, e)

    # Wearable (Part 2a): last night / steps / HR vs baseline from a connected Fitbit /
    # Pixel Watch (Google Health API).
    # Next to WEIGHT on purpose — same "act on it, don't recite it" contract. The
    # heartbeat wraps this builder, so its ticks see the block too.
    if config.GOOGLE_HEALTH_ENABLED and (getattr(user, "onboarding_step", 0) or 0) >= 3:
        try:
            from integrations.google_health_sync import wearable_context
            _wb = wearable_context(user, session)
            if _wb:
                parts.append(_wb)
        except Exception as e:  # noqa: BLE001
            logger.warning("WEARABLE_CONTEXT_FAILED user=%s err=%s", user.id, e)

    # Contextual reveals: capabilities they haven't touched yet (capabilities.py).
    # Post-onboarding only; the voice rule limits it to one clause when it fits.
    if (getattr(user, "onboarding_step", 0) or 0) >= 3:
        try:
            from capabilities import unused_context
            _uc = unused_context(user, session)
            if _uc:
                parts.append(_uc)
        except Exception as e:  # noqa: BLE001 — never break a turn over a hint
            logger.warning("UNUSED_CAPABILITIES_CONTEXT_FAILED user=%s err=%s", user.id, e)

    if _local:
        parts.append(f"## NOW\n{now_anchor(user)}")
    else:
        now = datetime.now(timezone.utc)
        parts.append(f"## NOW\n{now:%A %Y-%m-%d %H:%M}Z (resolve times against the user's "
                     f"timezone: {user.user_timezone or 'America/Los_Angeles'})")

    return "\n\n".join(parts)


# Returned when the loop can't produce a clean reply but the turn's tool writes have
# already persisted — a neutral ack beats raising into legacy (which would re-answer and
# ignore the work done) or asking to resend (which would re-log). Rare with the raised cap.
_LOOP_DEGRADE_REPLY = "got it."


def _join_text(content) -> str:
    """The reply is EVERY text block in the response, concatenated in order — not just
    the first. A single Sonnet 5 response interleaves blocks: thinking, then text, then
    (for inline server-side web_search) server_tool_use + web_search_tool_result, then
    MORE text; citations likewise split the answer across multiple text blocks. Taking
    only the first text block sent a lead-in ("from what I can find:") and silently
    dropped the substance that came after the tool result (burn-in finding). Non-text
    blocks (thinking / tool_use / server_tool_use / *_tool_result) contribute nothing."""
    return "".join(
        b.text for b in content
        if getattr(b, "type", None) == "text" and getattr(b, "text", None)
    ).strip()


_GYM_RE = re.compile(r"\b(gym|rsf|weight ?room|lift(?:ing)?|workout|train(?:ing)?|push|pull|legs|bench|squat|crowded|busy|packed|line)\b", re.I)


def _gym_mentioned(text: str, user) -> bool:
    if text and _GYM_RE.search(text):
        return True
    try:
        from workouts.session_ops import active_session_id
        return active_session_id(user.id) is not None
    except Exception:  # noqa: BLE001
        return False


# "240 cal", "27g", "27 g protein", "~1,250 kcal" — a stated macro number in a reply.
_MACRO_NUMBER_RE = re.compile(r"\b\d[\d,]{0,4}\s?(?:k?cal(?:ories)?|g\b|grams?\b)", re.IGNORECASE)


def run_agent_loop(user, combined_body: str, message_type: str, image_data: dict = None,
                   message_id: str = None, image_data_list: list = None) -> str:
    """One agentic turn → the reply text. Raises only on genuine anomalies (caller
    falls back to legacy); truncation and the iteration bound degrade gracefully."""
    # Multi-image: the user may have sent several photos (product + its label). The
    # model sees ALL of them; image_data stays the PRIMARY (first) for the single-image
    # signals below (estimation prompt, receipt pre-classifier, has_image marker).
    images = image_data_list if image_data_list else ([image_data] if image_data else [])
    image_data = images[0] if images else None
    session = get_session()
    try:
        context = build_loop_context(user, session)
        # Series §2.4: when a workout is discussed or the gym comes up, the coach
        # gets the meter as ONE code line — it phrases it, never invents a number.
        if config.RSF_METER_ENABLED and _gym_mentioned(combined_body, user):
            try:
                import occupancy as _occ
                from gym_beats import rsf_context_block
                context += rsf_context_block(_occ.now())   # '' when no fresh reading
            except Exception as e:  # noqa: BLE001
                logger.warning("RSF_CONTEXT_FAILED user=%s err=%s", user.id, e)
    finally:
        session.close()

    voice = _voice_prompt()

    # Macro-accuracy Phase A: portion/label estimation guidance rides ONLY on turns
    # where the model can actually see an image — a separate block, never a voice.md
    # edit (voice.md is the heartbeat's shared cached prefix), appended after the
    # cache breakpoint so the voice prefix cache is unaffected.
    estimation = None
    if image_data and config.READ_IMAGE_ENABLED and config.MEAL_ESTIMATION_PROMPT_ENABLED:
        estimation = _meal_estimation_prompt()

    # Phase E routing (label → history → dining → USDA → web → ask) rides EVERY
    # reactive turn: meals arrive mostly as text and there's no pre-classifier. It's
    # stable text, so it extends the CACHED prefix — [voice, routing] — keeping the
    # per-turn cost at cache-read rates. Heartbeat composes its own system; unaffected.
    routing = _meal_routing_prompt() if config.MEAL_ROUTING_PROMPT_ENABLED else None

    if config.PROMPT_CACHING_ENABLED:
        system = [{"type": "text", "text": voice, "cache_control": {"type": "ephemeral"}}]
        if routing:
            system.append({"type": "text", "text": routing,
                           "cache_control": {"type": "ephemeral"}})
        system.append({"type": "text", "text": context})
        if estimation:
            system.append({"type": "text", "text": estimation})
    else:
        system = f"{voice}\n\n{routing}\n\n{context}" if routing else f"{voice}\n\n{context}"
        if estimation:
            system += f"\n\n{estimation}"

    if image_data and config.READ_IMAGE_ENABLED and config.RECEIPTS_ENABLED:
        # Series §1.2: a cheap pre-classifier. A receipt is itemized into the pantry
        # and answered in code — the meal path never sees it. meal/other → unchanged.
        from receipts import handle_receipt_image
        receipt_reply = handle_receipt_image(user.id, image_data)
        if receipt_reply is not None:
            return receipt_reply
    if image_data and config.READ_IMAGE_ENABLED:
        # The model sees the image and routes it in-call (food/calendar/whiteboard/
        # other) — no pre-classifier beyond the receipt check. Log the receipt as corpus.
        logger.info("AGENT_LOOP_IMAGE user=%s images=%d — vision routing (read_image corpus marker)",
                    user.id, len(images))
        caption = combined_body or ("(the user sent an image)" if len(images) == 1
                                    else f"(the user sent {len(images)} images)")
        user_content = [*images, {"type": "text", "text": caption}]
    elif image_data:
        # read_image off: don't send vision; note it so the coach can ask, never invent.
        user_content = ((combined_body or "")
                        + "\n[the user sent an image you can't see — ask them what it shows]")
    else:
        user_content = combined_body

    # Assemble the enabled tool set (each behind its own flag, added one at a time).
    tools = []
    if config.REMEMBER_TOOL_ENABLED:
        from agent_tools import REMEMBER_TOOL
        tools.append(REMEMBER_TOOL)
    if config.LOG_WORKOUT_TOOL_ENABLED:
        from agent_tools import LOG_WORKOUT_TOOL
        tools.append(LOG_WORKOUT_TOOL)
    if config.MANAGE_LOG_TOOL_ENABLED:
        from agent_tools import MANAGE_LOG_TOOL
        tools.append(MANAGE_LOG_TOOL)
    if config.LOG_MEAL_TOOL_ENABLED:
        from agent_tools import LOG_MEAL_TOOL
        tools.append(LOG_MEAL_TOOL)
    if config.SET_FOOD_LOGGER_TOOL_ENABLED:
        from agent_tools import SET_FOOD_LOGGER_TOOL
        tools.append(SET_FOOD_LOGGER_TOOL)
    if config.SET_TARGETS_TOOL_ENABLED:
        from agent_tools import SET_TARGETS_TOOL
        tools.append(SET_TARGETS_TOOL)
    if config.LOG_WEIGHT_TOOL_ENABLED:
        from agent_tools import LOG_WEIGHT_TOOL
        tools.append(LOG_WEIGHT_TOOL)
    if config.SET_DAY_RESET_TOOL_ENABLED:
        from agent_tools import SET_DAY_RESET_TOOL
        tools.append(SET_DAY_RESET_TOOL)
    if config.SAVE_MENU_TOOL_ENABLED:
        from agent_tools import SAVE_MENU_TOOL
        tools.append(SAVE_MENU_TOOL)
    if config.START_WORKOUT_TOOL_ENABLED:
        from agent_tools import START_WORKOUT_SESSION_TOOL, SAVE_ROUTINE_TOOL, SET_LIFT_ANCHORS_TOOL
        tools.extend([START_WORKOUT_SESSION_TOOL, SAVE_ROUTINE_TOOL, SET_LIFT_ANCHORS_TOOL])
        if config.CARD_LINK_FALLBACK_ENABLED:
            from agent_tools import SET_CARD_DELIVERY_TOOL
            tools.append(SET_CARD_DELIVERY_TOOL)
    if config.LOG_EVENT_TOOL_ENABLED:
        from agent_tools import LOG_EVENT_TOOL
        tools.append(LOG_EVENT_TOOL)
    if config.LOOKUP_EVENTS_TOOL_ENABLED:
        from agent_tools import LOOKUP_EVENTS_TOOL
        tools.append(LOOKUP_EVENTS_TOOL)
    if config.REMINDERS_ENABLED:
        from agent_tools import SET_REMINDER_TOOL, CANCEL_REMINDER_TOOL, set_reminder_tool  # noqa: F401
        # set_reminder_tool() = SET_REMINDER_TOOL, plus the every_hours (water) affordance
        # when WATER_REMINDERS_ENABLED.
        tools.extend([set_reminder_tool(), CANCEL_REMINDER_TOOL])
    if config.SET_CHECKIN_LEVEL_TOOL_ENABLED:
        from agent_tools import SET_CHECKIN_LEVEL_TOOL
        tools.append(SET_CHECKIN_LEVEL_TOOL)
    if config.GET_DINING_MENU_TOOL_ENABLED:
        from agent_tools import GET_DINING_MENU_TOOL
        tools.append(GET_DINING_MENU_TOOL)
    if config.MEAL_HISTORY_TOOL_ENABLED:
        from agent_tools import MATCH_MEAL_HISTORY_TOOL
        tools.append(MATCH_MEAL_HISTORY_TOOL)
    if config.DINING_MATCH_TOOL_ENABLED:
        from agent_tools import MATCH_DINING_ITEM_TOOL
        tools.append(MATCH_DINING_ITEM_TOOL)
    if config.USDA_LOOKUP_TOOL_ENABLED:
        from agent_tools import USDA_FOOD_LOOKUP_TOOL
        tools.append(USDA_FOOD_LOOKUP_TOOL)
    if config.IMESSAGE_REACTIONS_ENABLED:
        # Tapbacks + threaded replies exist only on iMessage: offered by the SAME
        # router the send uses (a tripped breaker = no tools), so an SMS user's
        # model never sees an affordance it can't deliver.
        from sms import _resolve_channel
        if _resolve_channel(user.id) == "imessage":
            from agent_tools import REACT_TOOL, THREAD_REPLY_TOOL
            tools.extend([REACT_TOOL, THREAD_REPLY_TOOL])
    if config.WEB_SEARCH_TOOL_ENABLED:
        # Server-side tool: Anthropic runs the search inline and returns results as
        # content blocks; no client handler. When-to-search + output/query hygiene
        # are prompt rules in identity.md / voice.md. One shared definition for
        # every surface lives in agent_tools (cap = WEB_SEARCH_MAX_USES per reply).
        from agent_tools import WEB_SEARCH_TOOL
        tools.append(WEB_SEARCH_TOOL)
    if config.SEND_CONNECT_LINK_TOOL_ENABLED:
        # Texts a one-tap OAuth connect link (gcal/strava). Reveal rule lives in
        # identity/voice.md; the link bubble is an allowed URL exception.
        from agent_tools import SEND_CONNECT_LINK_TOOL
        tools.append(SEND_CONNECT_LINK_TOOL)

    from agent_tools import begin_turn, peek_turn_state
    begin_turn(user.id)  # react/reply_in_thread record into this; the caller pops it
    # Source provenance for log_meal (photo vs text) — the turn knows, the tool doesn't.
    peek_turn_state(user.id)["has_image"] = bool(image_data and config.READ_IMAGE_ENABLED)

    messages = [{"role": "user", "content": user_content}]
    last_text = ""
    for i in range(config.AGENT_LOOP_MAX_TOOL_ITERS):
        kwargs = dict(
            model=config.AGENT_LOOP_MODEL,
            max_tokens=config.AGENT_LOOP_MAX_TOKENS,  # room for thinking + tool JSON + reply
            thinking={"type": "adaptive"},  # held constant; switching modes breaks the messages cache
            output_config={"effort": "low"},
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = tools
        resp = client.messages.create(**kwargs)

        try:
            track(user.id, "agent_loop.run", config.AGENT_LOOP_MODEL, resp)
        except Exception as e:  # cost telemetry must never break the reply
            logger.warning("AGENT_LOOP_COST_TRACK_FAILED user=%s err=%s", user.id, e)

        stop = getattr(resp, "stop_reason", None)
        block_types = [getattr(b, "type", None) for b in resp.content]

        # Every search the model issued this call, logged with the user id
        # (WEB_SEARCH_QUERY) — the line that shows what the coach reaches for.
        from agent_tools import log_web_search_queries
        log_web_search_queries(user.id, resp.content, "agent_loop.run")

        # Server-side tool (web_search) hit its iteration limit — re-send to resume.
        if stop == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue

        # The model requested client tools: execute them (code-mediated), feed results back.
        if stop == "tool_use":
            from agent_tools import dispatch_tool
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    out = dispatch_tool(block.name, block.input, user.id, message_id=message_id)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
            messages.append({"role": "user", "content": results})
            continue

        # ALL text blocks, concatenated (adaptive thinking + inline web_search / citations
        # split the reply across several — see _join_text). This is the burn-in fix.
        text = _join_text(resp.content)
        if text:
            last_text = text

        # Truncation is its OWN branch: the output cap was hit (thinking + tools + reply
        # didn't fit). Log it BY NAME with the blocks we actually got — the observability
        # that "no text block" was missing — and degrade gracefully instead of raising
        # into fallback (which would re-answer via legacy and ignore any tool writes).
        if stop == "max_tokens":
            logger.warning("AGENT_LOOP_TRUNCATED user=%s iter=%d stop=max_tokens blocks=%s "
                           "max_tokens=%d — degrading, not raising",
                           user.id, i, block_types, config.AGENT_LOOP_MAX_TOKENS)
            return last_text or _LOOP_DEGRADE_REPLY

        # Safety refusal (Sonnet 5 classifiers, HTTP 200 + stop_reason=refusal): content
        # is empty pre-output or partial mid-stream. Don't raise into legacy — it would
        # re-answer the same declined request. Return what we have (or a neutral line).
        if stop == "refusal":
            logger.warning("AGENT_LOOP_REFUSAL user=%s iter=%d blocks=%s", user.id, i, block_types)
            return text or _LOOP_DEGRADE_REPLY

        # Normal terminal turn (end_turn / stop_sequence) with a reply — unless the
        # reply is the reaction-only sentinel (or the "no text needed" note a model
        # drifts to): then the tapback WAS the reply and nothing is sent.
        from agent_tools import is_reaction_only_text, is_single_emoji_text
        state = peek_turn_state(user.id)
        if text and is_reaction_only_text(text, state.get("reacted")):
            logger.info("AGENT_LOOP_REACTION_ONLY user=%s iter=%d swallowed=%r", user.id, i, text[:40])
            return ""
        from agent_tools import leaked_tool_call
        leak = leaked_tool_call(text) if (text and tools) else None
        if leak and leak[0] == "react_to_message" and not state.get("reacted"):
            from agent_tools import latest_inbound_imessage_sid
            from sms import react_to_message, _resolve_channel
            sid = latest_inbound_imessage_sid(user.id) if _resolve_channel(user.id) == "imessage" else None
            if sid and react_to_message(user.id, sid, leak[1]):
                logger.warning("AGENT_LOOP_TOOL_CALL_IN_TEXT user=%s executed=%s %r", user.id, leak[0], leak[1])
                return ""
            logger.warning("AGENT_LOOP_TOOL_CALL_IN_TEXT user=%s dropped=%r", user.id, text[:60])
            return ""  # never text a tool name to the user
        if leak and leak[0] == "reply_in_thread":
            logger.warning("AGENT_LOOP_TOOL_CALL_IN_TEXT user=%s dropped=%r", user.id, text[:60])
            return ""
        # Narration guard: the model wrote its PLAN as the reply (live 2026-09-23, Alex:
        # "react to this — it's a simple decline, just acknowledge. ... Let me set the
        # standing Tue/Thu reminder." — end_turn, no tool call, texted verbatim). ONE
        # forced follow-up with a path for both branches: act with the tools, then send
        # the real words (or the silent sentinel). A repeat is dropped, never sent.
        from agent_tools import looks_like_narration, REACTION_ONLY_SENTINEL
        if text and looks_like_narration(text):
            if not state.get("narration_nudged"):
                state["narration_nudged"] = True
                logger.warning("AGENT_LOOP_NARRATION_NUDGE user=%s iter=%d text=%r", user.id, i, text[:80])
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": (
                    "[code check — NOT from the user, do not answer it: that text reads as your own "
                    f"planning notes, not a message to {user.name}. It was NOT sent. If you meant to "
                    "act (react, set a reminder, log something), call the tool NOW — check the "
                    "REMINDERS block first, a reminder listed there already exists. Then send only "
                    f"the words you'd actually text {user.name}; if a reaction was the whole reply, "
                    f"reply with exactly {REACTION_ONLY_SENTINEL}.]")})
                continue
            logger.warning("AGENT_LOOP_NARRATION_DROPPED user=%s iter=%d text=%r", user.id, i, text[:80])
            return ""
        # Write-back guard (honesty invariant, code side). usda_food_lookup named rows
        # that are ALREADY LOGGED (turn state: pending_writeback); if the reply quotes a
        # macro number and none of those rows was edited, the correction exists only
        # in the chat (live 2026-09-19: "the muffin's ~240 cal, 27g", row still 260/22).
        # ONE forced follow-up: edit now, or reply without a new number. Never loops.
        pending = state.get("pending_writeback") or {}
        if (pending and text and tools and _MACRO_NUMBER_RE.search(text)
                and not state.get("writeback_nudged")):
            state["writeback_nudged"] = True
            ids = "; ".join(f"id {k} ({v})" for k, v in pending.items())
            logger.info("AGENT_LOOP_WRITEBACK_NUDGE user=%s iter=%d ids=%s", user.id, i, list(pending))
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": (
                "[code check — NOT from the user, do not answer it: you looked up an item that is "
                f"ALREADY LOGGED ({ids}) and your reply states a number for it, but you did not edit "
                "that row. If its numbers changed, call manage_log edit (entity meal, that id, fields "
                "calories / protein_g) NOW, then send the reply. If the row is already right, send the "
                "reply as it was. A corrected number that isn't written stays wrong tomorrow.]")})
            continue
        if text and is_single_emoji_text(text) and tools and not state.get("reacted"):
            # A bare ❤️ as a TEXT on iMessage is a tapback that lost its way — send it
            # as the reaction on their latest message instead (never as a bubble).
            from agent_tools import latest_inbound_imessage_sid
            from sms import react_to_message, _resolve_channel
            sid = latest_inbound_imessage_sid(user.id) if _resolve_channel(user.id) == "imessage" else None
            if sid and react_to_message(user.id, sid, text.strip()):
                logger.info("AGENT_LOOP_EMOJI_TEXT_AS_REACTION user=%s emoji=%r", user.id, text.strip())
                return ""
        if text:
            return text

        # A reaction-only turn: the tapback WAS the reply. Empty text is the correct
        # outcome, not an anomaly — the caller sends nothing and clears the bubble.
        if state.get("reacted"):
            logger.info("AGENT_LOOP_REACTION_ONLY user=%s iter=%d", user.id, i)
            return ""

        # A terminal stop with no text is a genuine anomaly — log stop_reason + block
        # types (not a token-count guessing game) and fall back to legacy.
        logger.error("AGENT_LOOP_NO_TEXT user=%s iter=%d stop_reason=%s blocks=%s",
                     user.id, i, stop, block_types)
        raise RuntimeError(f"agent loop returned no text block (stop_reason={stop}, blocks={block_types})")

    # Iteration bound reached: the tools ran (writes persisted) but the model never
    # emitted a final reply. Degrade with what we have — never raise into fallback.
    logger.warning("AGENT_LOOP_MAX_ITERS user=%s iters=%d — returning best-effort reply",
                   user.id, config.AGENT_LOOP_MAX_TOOL_ITERS)
    return last_text or _LOOP_DEGRADE_REPLY
