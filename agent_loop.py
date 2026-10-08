"""
Phase 2 — the single agent loop (inbound only).

One model call per inbound, one voice, full context. Replaced the legacy
classifier→specialists→merge pipeline, which was deleted in Phase 6 Commit C.
The loop is the SOLE responder now: on any exception the webhook sends one safe
minimal line and logs at ERROR (there is no legacy fallback). Gated by
SINGLE_AGENT_LOOP_ENABLED, kept as a permanently-on lever.

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
from datetime import datetime, timezone, timedelta

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


# Late-hour / near-sleep detection for the REACTIVE nudging priority-flip. Small-hours
# default when the user has no parseable sleep pattern: 1am–6am local. A late-night
# sleep_time only shifts the START later (someone who sleeps 2am isn't "past sleep" at
# 1am); an evening sleep_time (10pm) does NOT make 10pm "too late to eat dinner" — the
# 1am floor governs then. See config.LATE_HOUR_SLEEP_FIRST_ENABLED.
LATE_HOUR_DEFAULT_START = 1   # 1am local
LATE_HOUR_DEFAULT_END = 6     # 6am local


def _after_midnight_bed_wake(user, session=None) -> tuple[int, int | None] | None:
    """(bed_hour, wake_hour) local when their usual bedtime is AFTER midnight (0–6am) —
    measured (watch) first, else the profile clock values. None when bed is in the evening
    or unknown: then the clock's idea of "late" is roughly right and no signal is owed."""
    from heartbeat import _parse_hour
    measured = _measured_late_hours(user, session)
    if measured is not None:
        hs, hw = measured
    else:
        hs = _parse_hour(getattr(user, "sleep_time", None))
        hw = _parse_hour(getattr(user, "wake_time", None))
    if hs is None or not (0 <= hs <= LATE_HOUR_DEFAULT_END):
        return None
    return hs, hw


def _hour_words(h: int | None, *, fallback: str) -> str:
    if h is None:
        return fallback
    if h == 0:
        return "midnight"
    if h == 12:
        return "noon"
    return f"{h}am" if h < 12 else f"{h - 12}pm"


def _evening_not_late_block(user, session=None, *, now: datetime = None) -> str | None:
    """The EVENING FOR THEM, NOT LATE signal: 9pm → their (after-midnight) bed hour, so the
    coach doesn't read 10:56pm as bedtime for someone who sleeps at 3. Live 2026-10-05
    (user 48): "it's almost 11, maybe just sleep" / "it's past 11, u eating or calling it?".
    Flag-gated, fail-open (None)."""
    if not config.EVENING_NOT_LATE_SIGNAL_ENABLED:
        return None
    try:
        bw = _after_midnight_bed_wake(user, session)
        if not bw:
            return None
        bed, wake = bw
        from timefmt import resolve_tz
        ref = now if now is not None else _late_clock()
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        local = ref.astimezone(resolve_tz(user))
        if not (local.hour >= 21 or local.hour < bed):
            return None
        clock = local.strftime("%I:%M%p").lstrip("0").lower()
        return (
            "## EVENING FOR THEM, NOT LATE\n"
            f"It's {clock}; their usual bed is ~{_hour_words(bed, fallback='after midnight')} "
            f"(up ~{_hour_words(wake, fallback='late morning')}). This is mid-evening on THEIR clock. "
            "Do NOT call it late, suggest sleep, or 'call it a night' — eating now is normal for them "
            "and still today's food. Late is defined by their rhythm, not the wall clock: when it is "
            "actually past their bedtime, the past-bedtime sleep-first block appears instead."
        )
    except Exception:  # noqa: BLE001 — a clock/tz hiccup must never break a turn
        return None


def _late_clock() -> datetime:
    """The reactive loop's clock (aware UTC). Patched in tests to freeze a wall time so
    the late-hour signal is deterministic (never assert against real now — date-fragile)."""
    return datetime.now(timezone.utc)


def _measured_late_hours(user, session):
    """(bed_hour, wake_hour) local from the MEASURED sleep window, or None → static
    profile times. Flag-gated by MEASURED_SLEEP_WINDOW_ENABLED; fail-open."""
    if session is None or not config.MEASURED_SLEEP_WINDOW_ENABLED:
        return None
    try:
        from wearable_read import measured_sleep_window
        w = measured_sleep_window(user, session)
        if not w:
            return None
        return w.bed_hm[0], w.wake_hm[0]
    except Exception:  # noqa: BLE001
        return None


def _late_hour_window(user, session=None) -> tuple[int, int]:
    """(start_hour, end_hour) local — the small-hours 'past sleep' window. Uses the user's
    sleep_time as the start when it parses to a genuine late-night hour (midnight–6am) and
    wake_time as the end when it parses to a morning hour; otherwise the 1am–6am default.
    When a MEASURED sleep window is available (typical bed/wake from the watch), its hours
    REPLACE the static profile hours — same small-hours constraints. Always returns a clean
    small-hours window (falls back to the default if the derived bounds would be
    degenerate)."""
    from heartbeat import _parse_hour
    start, end = LATE_HOUR_DEFAULT_START, LATE_HOUR_DEFAULT_END
    measured = _measured_late_hours(user, session)
    if measured is not None:
        hs, hw = measured
    else:
        hs = _parse_hour(getattr(user, "sleep_time", None))
        hw = _parse_hour(getattr(user, "wake_time", None))
    # Only a small-hours sleep_time moves the start (a 2am sleeper isn't "past sleep" at
    # 1am). An evening/late-evening sleep_time is ignored here — the 1am floor still holds.
    if hs is not None and 0 <= hs <= LATE_HOUR_DEFAULT_END:
        start = hs
    # A morning wake_time (up to 8am) tightens the end to it; a late waker keeps the 6am
    # cap so we never tell someone who's clearly awake and texting at, say, 10am to sleep.
    if hw is not None and 0 <= hw <= 8:
        end = hw
    if not (0 <= start < end <= 12):
        start, end = LATE_HOUR_DEFAULT_START, LATE_HOUR_DEFAULT_END
    return start, end


def _is_late_hour(user, *, now: datetime = None, session=None) -> bool:
    """True when the user's LOCAL time is in the small hours past their sleep pattern —
    the moment reactive nutrition nudging should prioritize sleep over macro-completion.
    When a `session` is given the window prefers the user's MEASURED bed/wake (flag-gated),
    else the static profile times. Flag-gated + fail-open: any error → False (today's
    behavior)."""
    if not config.LATE_HOUR_SLEEP_FIRST_ENABLED:
        return False
    try:
        from timefmt import resolve_tz
        ref = now if now is not None else _late_clock()
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=timezone.utc)
        local_hour = ref.astimezone(resolve_tz(user)).hour
        start, end = _late_hour_window(user, session)
        return start <= local_hour < end
    except Exception:  # noqa: BLE001 — a clock/tz hiccup must never break a turn
        return False


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

    # 1. Unified memory (ALL categories, safety appended universally). Fix 2: expose
    # each fact's [id:…] so the model can update/invalidate a specific entry precisely
    # (invalidate needs an entry_id; without ids shown it could only use the fragile
    # substring update). The ids are threaded into the injected block via render_categories.
    # coaching_lessons are the coach's notes about ITSELF, not facts about the user —
    # they render as their own authoritative block below, never inside this one.
    _fact_cats = tuple(c for c in CATEGORIES if c != "coaching_lessons")
    mem_text, _ids = render_categories(
        profile, _fact_cats, include_safety_universal=True,
        show_ids=config.MEMORY_ENTRY_IDS_IN_PROMPT_ENABLED)
    if mem_text:
        parts.append(f"## WHAT YOU REMEMBER ABOUT {user.name.upper()}\n{mem_text}")

    # 1b. Lessons from coaching them (lessons.py) — flag-gated, fail-open; also reaches
    # the heartbeat, whose _proactive_context begins with this builder.
    try:
        from lessons import lessons_block
        _lb, _ = lessons_block(user)
        if _lb:
            parts.append(_lb)
    except Exception as e:  # noqa: BLE001 — never break a turn over a hint
        logger.warning("LESSONS_CONTEXT_FAILED user=%s err=%s", user.id, e)

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

    # Cross-turn image persistence: what recent inbound photos showed (the image itself
    # is gone next turn — this is the durable trace so a later turn references it instead
    # of mislabeling it, denying it was sent, or re-asking what it already answered).
    if config.RECENT_MEDIA_ENABLED:
        from recent_media import render_recent_photos_block
        photos = render_recent_photos_block(getattr(user, "recent_photos", None))
        if photos:
            parts.append(photos)

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
    # 4a. Their own routine (custom_templates) if they've given one — so the coach never
    # re-asks and can name the real exercises — else the no-routine state, so a "send my
    # push day" gets a captured routine instead of a generic default silently passed off as
    # theirs (live incident user 31). Only the day cycle they've stated is defaults-only.
    try:
        from workouts.routine import describe_routine
        routine_desc = describe_routine(getattr(user, "custom_templates", None))
    except Exception:  # noqa: BLE001
        routine_desc = None
    if routine_desc:
        parts.append("## THEIR ROUTINE (on their workout cards — the real exercises; don't re-ask)\n"
                     + routine_desc)
    elif config.ROUTINE_CAPTURE_OFFER_ENABLED and config.START_WORKOUT_TOOL_ENABLED:
        parts.append("## THEIR ROUTINE\nnone on file — their workout cards fall back to GENERIC default "
                     "exercises for each day. When they ask for a card / start a session, tell them it's "
                     "starting defaults and offer to save what they actually run (save_routine with "
                     "routine_text). Don't pass a default day off as their real routine.")
    # 4a-i. Even with NO saved routine, their PAST sessions and every set ARE logged and
    # can be reconstructed — so the coach never confabulates "I don't have your old card's
    # exercises" (live incident user 31: 8 completed push sessions on record, coach said
    # the data was gone). Cheap DISTINCT of the day keys they've actually finished; fail-open.
    if config.START_WORKOUT_TOOL_ENABLED:
        try:
            from workouts.session_ops import completed_template_keys
            from workouts.templates import day_label as _day_label
            _done_keys = completed_template_keys(user.id)
        except Exception:  # noqa: BLE001 — a context note must never break the turn
            _done_keys = []
        if _done_keys:
            _labels = ", ".join(_day_label(k) for k in _done_keys[:8])
            parts.append(
                "## PRIOR SESSIONS (their real logged workouts — reconstructable)\n"
                f"they have completed sessions logged for: {_labels}. Every set is on record. "
                "If they ask what they did on a past day, or you need their REAL exercises for a "
                "day with no saved routine, call reconstruct_routine_from_history (template_key = "
                "the day) — it reads the actual movements back. NEVER say you don't have their "
                "previous workout's exercises; you do. After showing them, offer to save it "
                "(save_routine)."
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

    # 4a-ii. A one-time form-demo link for a movement on their card they haven't been
    # shown yet (exercise_demos.py). Resolver is PURE (no DB write); we mark seen in a
    # background thread ONLY for the demo we actually inject, so it never repeats. Flag-
    # gated + fail-open: off flag or any error injects nothing and never breaks the turn.
    if config.EXERCISE_DEMOS_ENABLED:
        try:
            from exercise_demos import unseen_demos_for, mark_demos_seen, _resolve
            names = []
            for _day_exs in (getattr(user, "custom_templates", None) or {}).values():
                if isinstance(_day_exs, list):
                    for _ex in _day_exs:
                        if isinstance(_ex, dict):
                            # label first so the human-readable name is what we show if it
                            # resolves; slug/name are fallbacks (unseen_demos_for dedups by
                            # the resolved canonical key, so the first form to resolve wins).
                            for _k in ("label", "slug", "name"):
                                if _ex.get(_k):
                                    names.append(str(_ex[_k]))
            demos = unseen_demos_for(user, names, limit=1)
            if demos:
                _lines = "\n".join(f"{ex}: {url}" for ex, url in demos.items())
                parts.append(
                    "## FORM DEMO (share naturally if it fits — one time only)\n"
                    f"{_lines}\n"
                    "Drop this link in your own voice ONLY when it fits — programming this "
                    "movement, sending/discussing the card, or if they ask how to do it. "
                    "Don't force it, don't list it robotically, and offer it just this once."
                )
                _keys = [k for k in (_resolve(ex) for ex in demos) if k]
                mark_demos_seen(user.id, _keys)
        except Exception as e:  # noqa: BLE001 — a form-demo hint must never break a turn
            logger.warning("EXERCISE_DEMOS_CONTEXT_FAILED user=%s err=%s", user.id, e)

    # 4b. The in-progress session's REAL type + start + logged-set count. Kills the
    # confabulation where the coach called the pull session it had just created "a push
    # session from earlier" — it never saw the active session's actual template_key.
    if config.ACTIVE_SESSION_CONTEXT_ENABLED:
        try:
            from workouts.session_ops import active_session_brief
            brief = active_session_brief(user)
        except Exception:  # noqa: BLE001 — never let this block break the turn
            brief = None
        if brief:
            parts.append(brief)

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
            from connect_offers import context_line as _google_account_line, integrations_block
            # Every enabled provider, connected OR NOT — and the block is the only truth.
            # Live 2026-10-02 (founder, mid demo): the coaching summary said "Connected
            # feeds: Google Calendar…" while the row had been removed; the block listed
            # only what WAS connected, so the model trusted the summary, said "yeah i can
            # see it", and argued when corrected. Absence has to be stated, not implied.
            parts.append(integrations_block(user, status_line(user.id), _google_account_line(user)))
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
    from timefmt import describe_day_reset
    reset_txt = describe_day_reset(user)
    parts.append(
        "## TODAY'S TOTALS (authoritative — the ONLY source for today's running total; "
        f"it resets to 0 at {reset_txt})\n" + "\n".join(lines) +
        "\nWhen you state the day's total, use THESE numbers as they are — read them here, "
        "don't recompute or add meals up yourself, and don't carry forward a total from "
        "earlier in the thread (it may be a previous day). Just after you log or edit a meal "
        "THIS turn, the tool result gives you the updated day total — use that number "
        "instead, since this block was built before the change. State the number plainly, as "
        "your own knowledge; NEVER say \"quote from context\", \"per the totals\", or "
        "otherwise announce that you're reading it — that narration is a bug, not a reply.")

    # 7a2. Late-hour / near-sleep priority flip (LATE_HOUR_SLEEP_FIRST). Live 2026-09-28
    # ~4am: with an unmet protein target the coach kept pushing "eat some actual protein"
    # toward the daily number at 4am. The reasoning to say "go to sleep, protein can wait"
    # existed (the coach agreed when asked) but wasn't applied PROACTIVELY. In the user's
    # small hours this compact signal flips the priority: nudge SLEEP, frame remaining
    # macros as a tomorrow thing, don't push a big meal. Advisory only — logging still
    # works (never refuse a log) and a direct food/macro question is still answered
    # honestly. Placed right after TODAY'S TOTALS so it reframes the "protein remaining"
    # line that would otherwise drive the 4am harping.
    if _is_late_hour(user, session=session):
        unmet = ""
        if user.protein_target and (user.protein_target - tot_pro) > 0:
            unmet = (f" They're ~{user.protein_target - tot_pro}g of protein short of "
                     f"today's target — that's fine, it resets at {reset_txt.split(' (')[0]}.")
        parts.append(
            "## LATE / PAST SLEEP WINDOW — PRIORITIZE SLEEP OVER MACRO-COMPLETION\n"
            "It's the small hours for them (past their sleep pattern)." + unmet +
            " Do NOT harp on unmet macros or push more food / a big meal at this hour — "
            "protein can wait till tomorrow. The nudge here is SLEEP, not eating. If they "
            "report food, still LOG it normally (never refuse a log); just change the "
            "nudging PRIORITY. If they explicitly ask about food or their macros, answer "
            "honestly and plainly — don't proactively push them to eat to hit a target now.")

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

    # Recent-activity awareness (advisory): today's measured steps/active minutes so the
    # coach acknowledges real movement and doesn't imply they've been sedentary or over-
    # push exercise. Flag-gated + fail-open (inert with no wearable / no today row).
    if config.WEARABLE_ACTIVITY_CONTEXT_ENABLED and (getattr(user, "onboarding_step", 0) or 0) >= 3:
        try:
            from wearable_read import activity_context
            _act = activity_context(user, session)
            if _act:
                parts.append(_act)
        except Exception as e:  # noqa: BLE001 — never break a turn over an advisory hint
            logger.warning("WEARABLE_ACTIVITY_CONTEXT_FAILED user=%s err=%s", user.id, e)

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

    # Anti-nagging: which standing nudges the coach has ALREADY raised today. Every
    # proactive path (reactive replies AND the heartbeat) writes an outbound Message
    # row, so scanning recent outbound catches them all in one place. Live 2026-09-28
    # (user 31): the coach re-derived "eat some protein" ~5× in a day with no memory it
    # had said it. Surfacing the already-raised topics lets it vary the angle or let a
    # gap rest instead of restating the same line. Flag-gated + fail-open inside; also
    # reaches the heartbeat, whose _proactive_context begins with build_loop_context.
    # Deferred tasks in flight (agent_tasks.py) — so the coach doesn't re-promise or
    # pre-empt what code is already doing. Flag-gated, fail-open.
    if config.TASKS_ENABLED:
        try:
            from agent_tasks import context_block as _tasks_block
            _tb = _tasks_block(user, session)
            if _tb:
                parts.append(_tb)
        except Exception as e:  # noqa: BLE001
            logger.warning("TASKS_CONTEXT_FAILED user=%s err=%s", user.id, e)

    if config.NUDGE_REPETITION_GUARD_ENABLED:
        try:
            from nudge_guard import nudge_guard_block
            _ng = nudge_guard_block(user, session)
            if _ng:
                parts.append(_ng)
        except Exception as e:  # noqa: BLE001 — never break a turn over a hint
            logger.warning("NUDGE_GUARD_CONTEXT_FAILED user=%s err=%s", user.id, e)

    if _local:
        parts.append(f"## NOW\n{now_anchor(user)}")
    else:
        now = datetime.now(timezone.utc)
        parts.append(f"## NOW\n{now:%A %Y-%m-%d %H:%M}Z (resolve times against the user's "
                     f"timezone: {user.user_timezone or 'America/Los_Angeles'})")

    # EVENING FOR THEM, NOT LATE — rides with the clock line: 9pm → their (after-midnight)
    # bed hour, so 10:56pm isn't read as bedtime for a 3am sleeper. Its hours are disjoint
    # from the LATE / PAST SLEEP WINDOW block above (that one starts AT their bed hour).
    _ev = _evening_not_late_block(user, session)
    if _ev:
        parts.append(_ev)

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


# A schedule question, asked outright. Pre-building the rundown for these (below) is what
# makes "send me my week" complete every time — the tool stays for everything else.
_WEEK_ASK_RE = re.compile(
    r"\b(?:my|this|next|the|ur|your)\s+week\b"
    r"|\brest\s+of\s+(?:the|my|this)\s+week\b"
    r"|\bweek\s+look\b"
    r"|\bwhat(?:'?s|\s+is|\s+do\s+i\s+(?:have|got))\b[^.?!\n]{0,40}\b(?:due|coming\s+up|on\s+(?:my\s+)?(?:calendar|schedule)|"
    r"tomorrow|today|tonight|(?:mon|tues?|wednes|thurs?|fri|satur|sun)(?:day)?)\b"
    r"|\bwhat(?:'?s|\s+is)\s+(?:on\s+)?(?:the\s+)?schedule\b"
    r"|\banything\s+due\b|\bwhat'?s\s+due\b",
    re.I)


# An academic task they mention (quiz, readings, hw, pset…) — the thing the coach tends to
# pin to the wrong course when none is named.
_ACADEMIC_TASK_RE = re.compile(
    r"\b(quiz(?:zes)?|exam|midterm|final|readings?|homework|hw\d*|pset\d*|problem\s*set|essay|paper|"
    r"lab\s*report|project|warm-?ups?|assignment|study(?:ing)?|review\s+session)\b", re.I)
# A course named outright: "cs70", "data c104", "engin 183", "61c", "math 54", "ochem", "stat 20"
_COURSE_TOKEN_RE = re.compile(
    r"\b(?:[a-z]{2,8}\s?c?\d{1,3}[a-z]?\b|\d{2,3}[a-z]\b|o-?chem|physics|chem(?:istry)?|bio(?:logy)?|"
    r"calc(?:ulus)?|econ(?:omics)?|stats?\b|linear\s+algebra)", re.I)
_CLASS_KIND_RE = re.compile(r"\b(discussion|disc|section|lecture|lab|seminar|recitation)\b", re.I)
_CLASS_TITLE_RE = re.compile(r"\b(lecture|discussion|section|lab|seminar|recitation)\b", re.I)


def _which_class_block(user, text: str, session, *, now: datetime = None) -> str | None:
    """They named an academic task but not its course → this week's classes from the
    calendar, plus the code's own match when they pointed at a kind of class ("discussion
    today" → the one discussion on today's calendar). Tells the coach to pair the task
    with THAT, or ask — never with another course's item. None when it doesn't apply.
    Read-only, fail-open."""
    if not config.ACADEMIC_COURSE_MATCH_ENABLED or not text or session is None:
        return None
    try:
        if not _ACADEMIC_TASK_RE.search(text) or _COURSE_TOKEN_RE.search(text):
            return None
        from schedule import collect_rundown, _fmt_span, _tz, _ref_local
        from timefmt import to_local
        local = _ref_local(user, now)
        tz = _tz(user)
        day0 = local.replace(hour=0, minute=0, second=0, microsecond=0)
        lo = day0.astimezone(timezone.utc).replace(tzinfo=None)
        hi = (day0 + timedelta(days=7)).astimezone(timezone.utc).replace(tzinfo=None)
        # class-titled events, INCLUDING a section the radar also reads as a quiz day
        # ("cs70 Discussion (friday = quiz)") — it is still the class they may point at
        events = [e for e in collect_rundown(user.id, lo=lo, hi=hi, now=now, session=session)
                  if _CLASS_TITLE_RE.search(e.title or "")]
        if not events:
            return None
        today = local.date()
        kinds = {k.lower() for k in _CLASS_KIND_RE.findall(text)}
        kinds = {"discussion" if k in ("disc", "section", "recitation") else k for k in kinds}
        says_today = bool(re.search(r"\b(today|tonight|this morning|this afternoon|rn|right now)\b", text, re.I))
        match = None
        if kinds:
            cands = [e for e in events if any(k in (e.title or "").lower() or (k == "discussion" and "section" in (e.title or "").lower()) for k in kinds)]
            if says_today:
                cands = [e for e in cands if to_local(e.start, user).date() == today]
            if cands:
                match = sorted(cands, key=lambda e: e.start)[0]
        lines = []
        for e in sorted(events, key=lambda e: e.start)[:14]:
            d = to_local(e.start, user)
            lines.append(f"- {d.strftime('%a')}{' (today)' if d.date() == today else ''} {_fmt_span(user, e)} — {e.title}")
        head = "## WHICH CLASS? (they named a task but not the course)\n"
        if match:
            head += (f"Code match: they pointed at a {', '.join(sorted(kinds))}{' today' if says_today else ''} and the "
                     f"calendar has exactly that: {match.title} ({to_local(match.start, user).strftime('%a')} "
                     f"{_fmt_span(user, match)}). Treat the task as belonging to THAT course unless they say otherwise.\n")
        else:
            head += ("No course named and nothing they said pins it to one of these — ask which class in one short "
                     "line if it matters to the reply, or stay neutral.\n")
        head += ("Never pair the task with a DIFFERENT course's calendar item (live: 'weekly warmup quiz' readings "
                 "were pinned to the CS70 quiz; they were for the Data C104 discussion). Their classes this week:\n")
        return head + "\n".join(lines)
    except Exception as e:  # noqa: BLE001 — a hint must never break a turn
        logger.warning("WHICH_CLASS_BLOCK_FAILED user=%s err=%s", getattr(user, "id", "?"), e)
        return None


def _week_asked(text: str) -> bool:
    return bool(text and _WEEK_ASK_RE.search(text))


def _week_ask_block(user, combined_body: str, session) -> str:
    """The code-built rundown for an outright schedule question, as a context block the
    model relays. '' when it isn't one, the flag is off, or the builder returns nothing."""
    if not (config.WEEK_ASK_RUNDOWN_IN_CONTEXT_ENABLED and config.SCHEDULE_RUNDOWN_ENABLED):
        return ""
    if not _week_asked(combined_body):
        return ""
    try:
        from schedule import build_rundown
        text = build_rundown(user.id, combined_body, session=session)
    except Exception as e:  # noqa: BLE001 — never break a turn over a hint
        logger.warning("WEEK_ASK_RUNDOWN_FAILED user=%s err=%s", user.id, e)
        return ""
    if not (text or "").strip():
        return ""
    logger.info("WEEK_ASK_RUNDOWN_PREBUILT user=%s chars=%d", user.id, len(text))
    return (
        "\n\n## SCHEDULE RUNDOWN (built in code for THIS question — relay it, don't rebuild it)\n"
        "They just asked about their schedule. This is the complete rundown for the window they "
        "named: every day, every class/meeting/event, every deadline. Relay ALL of it in your voice — "
        "do NOT trim it to deadlines or to the first few days, do NOT call schedule_rundown again, "
        "and never say \"that's it\" / \"that's the week\" unless this is all of it. Lightly reword "
        "the opening line if you like; keep every day and every item.\n" + text
    )


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

# U+FFFC OBJECT REPLACEMENT CHARACTER — what Photon leaves behind when an inline image
# is stripped upstream (attachments=0). A message that is ESSENTIALLY only these marks
# is a failed image, not text: the picture didn't come through.
_OBJ_REPLACEMENT = "￼"


def _persist_recent_photo(user_id: int, image_data, caption: str, reply: str) -> None:
    """Cross-turn image persistence: on an image turn, store a compact note of what the
    coach read off the photo (its caption + this turn's reply as the read), so a later
    turn can reference it. Meal/fact writes already persist on their own rows; this covers
    the ambiguous / conversational-only photo. Never breaks a turn."""
    if not (image_data and config.READ_IMAGE_ENABLED and config.RECENT_MEDIA_ENABLED):
        return
    try:
        from recent_media import record_photo
        record_photo(user_id, caption or "", reply or "")
    except Exception as e:  # noqa: BLE001 — persistence is best-effort, never fatal
        logger.warning("RECENT_MEDIA_PERSIST_FAILED user=%s err=%s", user_id, e)


def _is_failed_image_text(text: str) -> bool:
    """True when `text` is essentially just ￼ placeholder(s) with no real words — an
    image that didn't come through. Must contain at least one ￼ and, once ￼ and
    whitespace/punctuation are stripped, have no alphanumeric content left."""
    if not text or _OBJ_REPLACEMENT not in text:
        return False
    stripped = text.replace(_OBJ_REPLACEMENT, "")
    return not re.search(r"[A-Za-z0-9]", stripped)


# ─── Restatement guard (layer B of the text+photo double-reply fix) ─────────────
# Live 2026-10-02 13:16: texts "did not finish it / left like half / [image] / also log
# this banana" → the text turn replied "cut it to 450, banana's in · 555 cal, 20g protein
# so far"; the photo (it reached Flask after that flush) became a second turn that
# wrote nothing and replied "That banana's already in — logged it at 105 cal. Ur at 555
# for the day." Correct, and redundant. The send-time near-dup guard (0.85) misses it:
# same outcome, different words. This guard runs where the reply is finalized, with the
# one fact the sender can't see — whether the turn WROTE anything.

# Every integer in a reply ("1,250" → 1250): the figures it states.
_ANY_FIGURE_RE = re.compile(r"\d[\d,]*")
# A DAY-TOTAL figure: "ur at 555", "you're at 1,070", "sitting at 980", "puts u at 1400",
# "555 cal for the day", "137g so far", "1070 total", "1400 today". Item-level numbers
# ("logged it at 105 cal") deliberately do NOT match — in a no-write turn they are
# read-backs of rows that already exist, not new information.
_DAY_TOTAL_RE = re.compile(
    r"(?:\b(?:ur|u'?re|you'?re|you are|u are|now|sitting|that puts (?:u|you)|puts (?:u|you)|"
    r"brings (?:u|you) to|total(?:'?s| is|:)?)\s*(?:at\s+)?~?(\d[\d,]*)\b"
    r"|\b(\d[\d,]*)\s*(?:k?cal(?:ories|s)?|cals|g|grams?)?\s*(?:for the day|so far|today|"
    r"on the day|total|for today)\b)",
    re.IGNORECASE)
# A correction is never muted — the second line IS the point. ("fixed"/"updated" are
# NOT markers: in a turn that wrote nothing they are claims about the last turn's work.)
_CORRECTION_RE = re.compile(
    r"\b(?:actually|my bad|scratch that|correction|wait,?|wrong|not \d|should be)\b", re.IGNORECASE)
# The user asked something (no '?' needed): the reply is an answer, never an ack.
_INBOUND_QUESTION_RE = re.compile(
    r"\?|^\s*(?:what|whats|what's|how|when|where|why|which|who|did|do|does|is|are|am|can|could|"
    r"should|would|was|were|will|any|got)\b", re.IGNORECASE)


def _figures(text: str) -> set:
    out = set()
    for m in _ANY_FIGURE_RE.findall(text or ""):
        try:
            out.add(int(m.replace(",", "")))
        except ValueError:
            pass
    return out


def _day_totals(text: str) -> set:
    out = set()
    for a, b in _DAY_TOTAL_RE.findall(text or ""):
        raw = a or b
        try:
            n = int(raw.replace(",", ""))
        except ValueError:
            continue
        if n >= 100:   # a day total is calories- or grams-sized, never "2 eggs"
            out.add(n)
    return out


def _recent_outbound_text(user_id: int, window_s: int) -> str:
    """Bodies of the coach's text sends to this user inside the window (newest first),
    reactions excluded — the restatement candidate's reference. '' when none."""
    from datetime import timedelta
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=window_s)
    session = get_session()
    try:
        rows = (session.query(Message.body)
                .filter(Message.user_id == user_id, Message.direction == "out",
                        Message.created_at >= since,
                        Message.message_type != "reaction")
                .order_by(Message.id.desc()).limit(5).all())
    finally:
        session.close()
    return "\n".join(r[0] for r in rows if r[0] and not r[0].startswith("[reacted"))


def _restatement_figure(reply: str, inbound: str, prev: str):
    """The day-total figure (or 'neardup') that makes `reply` a restatement of `prev`,
    else None. Pure: no I/O, no state."""
    if not reply or not prev:
        return None
    if "?" in reply or _CORRECTION_RE.search(reply):
        return None
    if _INBOUND_QUESTION_RE.search(inbound or ""):
        return None
    prev_figs = _figures(prev)
    totals = _day_totals(reply)
    if totals - prev_figs:
        return None          # a NEW day total is new information
    if totals:
        return sorted(totals)[-1]
    # No day total stated: fall back to the near-dup score, but only when the reply
    # brings no figure the previous one didn't.
    if _figures(reply) - prev_figs:
        return None
    from sms import _norm_body, _similarity
    try:
        score = _similarity(_norm_body(prev), _norm_body(reply))
    except Exception:  # noqa: BLE001 — fail open
        return None
    if score >= config.OUTBOUND_RESTATEMENT_NEAR_DUP_THRESHOLD:
        return "neardup"
    return None


def _apply_restatement_guard(user, text: str, combined_body: str, state: dict) -> str:
    """Final reply → the reply to send. When the turn wrote nothing and `text` restates
    the outbound sent inside OUTBOUND_RESTATEMENT_WINDOW_S, send a minimal ack instead:
    a 👍 tapback on iMessage (returns '' — the caller treats it as a reaction-only turn),
    else 'got it'. Anything that changed state, answers, asks, or corrects passes
    through untouched. Fail-open on any error."""
    if not config.OUTBOUND_RESTATEMENT_GUARD_ENABLED or not text:
        return text
    try:
        from agent_tools import turn_wrote
        if turn_wrote(user.id):
            return text
        prev = _recent_outbound_text(user.id, config.OUTBOUND_RESTATEMENT_WINDOW_S)
        figure = _restatement_figure(text, combined_body, prev)
        if figure is None:
            return text
        logger.info("AGENT_LOOP_RESTATEMENT_SUPPRESSED user=%s figure=%s reply=%r",
                    user.id, figure, text[:80])
        if state.get("reacted"):
            return ""        # a tapback already went up this turn — that IS the ack
        if config.IMESSAGE_REACTIONS_ENABLED:
            from agent_tools import latest_inbound_imessage_sid
            from sms import react_to_message, _resolve_channel
            if _resolve_channel(user.id) == "imessage":
                sid = latest_inbound_imessage_sid(user.id)
                if sid and react_to_message(user.id, sid, "like"):
                    state["reacted"] = True
                    return ""
        return "got it"
    except Exception as e:  # noqa: BLE001 — the guard must never cost a reply
        logger.warning("AGENT_LOOP_RESTATEMENT_GUARD_FAILED user=%s err=%s", user.id, e)
        return text


# Image-path register. The photo turn's system ends with the long, analytical
# estimation block, and live replies on that path drifted to "That banana's already in —
# … Ur at 555 for the day." (sentence caps, support-rep register) while text turns stayed
# in voice. One line, after the estimation block (outside the cached prefix), restating
# the identity rule the text path already lives by. Register only — no behaviour change.
_IMAGE_REGISTER_REMINDER = (
    "Register check for this photo reply: it is still a text from a friend — lowercase "
    "default (capitalize for emphasis only), short, plain, no sentence-case paragraphs; "
    "the numbers exact, everything else loose. Same voice as your text replies."
)


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
        # A schedule question asked outright → the complete rundown is built in code now
        # and relayed, instead of hoping the model calls schedule_rundown (live 2026-10-06
        # 05:44: "Send me my week" → two deadlines, ten classes missing).
        context += _week_ask_block(user, combined_body, session)
        # An academic task without a course → this week's classes + the code's own match.
        _wc = _which_class_block(user, combined_body, session)
        if _wc:
            context += "\n\n" + _wc
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

    # Photo turns end with a one-line register reminder (_IMAGE_REGISTER_REMINDER) — the
    # text path's lowercase friend voice, restated. It rides the tail of the Phase A
    # block (which stays the LAST, uncached segment), or stands alone when that's off.
    if image_data and config.READ_IMAGE_ENABLED:
        estimation = (f"{estimation}\n\n{_IMAGE_REGISTER_REMINDER}" if estimation
                      else _IMAGE_REGISTER_REMINDER)

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
        # Series §1.2: a cheap pre-classifier. A receipt is itemized and answered in
        # code — grocery → pantry, restaurant → one grouped meal, unsure → one question
        # (the caption is a kind signal). The meal path never sees it. meal/other → unchanged.
        from receipts import handle_receipt_image
        receipt_reply = handle_receipt_image(user.id, image_data, caption=combined_body)
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
    elif (config.INLINE_IMAGE_PLACEHOLDER_GUARD_ENABLED
          and _is_failed_image_text(combined_body)):
        # An image arrived as inline ￼ (U+FFFC) placeholder(s) with attachments=0 — the
        # picture was stripped upstream (Photon), so there is nothing to see. Replace the
        # ￼ body with an explicit note so the coach says the pic didn't come through and
        # asks for a resend, NEVER confabulates its contents.
        logger.info("AGENT_LOOP_FAILED_IMAGE user=%s — ￼ placeholder, attachments=0", user.id)
        user_content = ("[a picture didn't come through — their message was just an image "
                        "placeholder with no attachment (stripped before it reached you). "
                        "Tell them the pic didn't come through and ask them to resend it; do "
                        "NOT describe or guess what it might have shown.]")
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
    if config.WEATHER_ENABLED:
        # Reactive weather answer + correctable location. The morning-brief weather line
        # rides the heartbeat brief, not a tool. open-meteo, no key, fail-open.
        from agent_tools import GET_WEATHER_TOOL, SET_WEATHER_LOCATION_TOOL
        tools.extend([GET_WEATHER_TOOL, SET_WEATHER_LOCATION_TOOL])
    if config.START_WORKOUT_TOOL_ENABLED:
        from agent_tools import (START_WORKOUT_SESSION_TOOL, SAVE_ROUTINE_TOOL, SET_LIFT_ANCHORS_TOOL,
                                  RECONSTRUCT_ROUTINE_TOOL)
        tools.extend([START_WORKOUT_SESSION_TOOL, SAVE_ROUTINE_TOOL, SET_LIFT_ANCHORS_TOOL,
                      RECONSTRUCT_ROUTINE_TOOL])
        if config.RESET_SESSION_TOOL_ENABLED:
            from agent_tools import RESET_WORKOUT_SESSION_TOOL
            tools.append(RESET_WORKOUT_SESSION_TOOL)
        if config.CARD_LINK_FALLBACK_ENABLED:
            from agent_tools import SET_CARD_DELIVERY_TOOL
            tools.append(SET_CARD_DELIVERY_TOOL)
    if config.LOG_EVENT_TOOL_ENABLED:
        from agent_tools import LOG_EVENT_TOOL
        tools.append(LOG_EVENT_TOOL)
    if config.LOOKUP_EVENTS_TOOL_ENABLED:
        from agent_tools import LOOKUP_EVENTS_TOOL
        tools.append(LOOKUP_EVENTS_TOOL)
    if config.SCHEDULE_RUNDOWN_ENABLED:
        # Deterministic, complete, deadline-safe rundown for range questions ("what's my
        # week / rest of the week / what's due") — completeness computed in code so the
        # model can't truncate the week and drop a Friday deadline (live 2026-09-28).
        from agent_tools import SCHEDULE_RUNDOWN_TOOL
        tools.append(SCHEDULE_RUNDOWN_TOOL)
    if config.CALENDAR_WRITE_ENABLED:
        # Create-only Google Calendar write-back. OFF by default — needs the write scope
        # on the consent screen + a user re-consent (readonly grants 403 on insert, which
        # the tool surfaces as an honest reconnect prompt).
        from agent_tools import CREATE_CALENDAR_EVENT_TOOL
        tools.append(CREATE_CALENDAR_EVENT_TOOL)
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
    if config.TASKS_ENABLED:
        # Deferred work ("find out and text me tonight") kept by code — agent_tasks.py.
        from agent_tools import SCHEDULE_TASK_TOOL, CANCEL_TASK_TOOL
        tools.extend([SCHEDULE_TASK_TOOL, CANCEL_TASK_TOOL])
    if config.FETCH_PAGE_TOOL_ENABLED:
        # Client-side page READ (web_search only finds). Course sites, syllabi, hours,
        # a link the user texted. Envelope in webfetch.py; per-turn cap on the turn state.
        from agent_tools import FETCH_PAGE_TOOL
        tools.append(FETCH_PAGE_TOOL)
    if config.SEND_CONNECT_LINK_TOOL_ENABLED:
        # Texts a one-tap OAuth connect link (gcal/strava). Reveal rule lives in
        # identity/voice.md; the link bubble is an allowed URL exception.
        from agent_tools import SEND_CONNECT_LINK_TOOL, SET_GOOGLE_ACCOUNT_TOOL
        tools.append(SEND_CONNECT_LINK_TOOL)
        tools.append(SET_GOOGLE_ACCOUNT_TOOL)   # the Google account first, while OAuth is in Testing
    if config.SEND_GYM_LINE_LINK_TOOL_ENABLED:
        # On-demand RSF virtual-line JOIN link (the same Waitwell link the automatic
        # heading-out flow sends) so the coach never fakes 'here's the line link'.
        from agent_tools import SEND_GYM_LINE_LINK_TOOL
        tools.append(SEND_GYM_LINE_LINK_TOOL)
    if config.FIND_STUDY_SPACE_TOOL_ENABLED:
        # Campus libraries: bookable study rooms (LibCal grid) + library hours/tags, and
        # the booking link as its own bubble (needs CalNet — we never book for them).
        from agent_tools import FIND_STUDY_SPACE_TOOL, SEND_STUDY_ROOM_LINK_TOOL
        tools.extend([FIND_STUDY_SPACE_TOOL, SEND_STUDY_ROOM_LINK_TOOL])
    if config.STAT_CARD_TOOL_ENABLED:
        # rsf / macros / week picture cards (stat_cards.py), queued on the turn and sent
        # by app.py right after the reply text.
        from agent_tools import SEND_STAT_CARD_TOOL
        tools.append(SEND_STAT_CARD_TOOL)

    from agent_tools import begin_turn, peek_turn_state
    begin_turn(user.id)  # react/reply_in_thread record into this; the caller pops it
    # Source provenance for log_meal (photo vs text) — the turn knows, the tool doesn't.
    _st = peek_turn_state(user.id)
    _st["has_image"] = bool(image_data and config.READ_IMAGE_ENABLED)
    # The caption stays on the turn so the photo-reread delete guard (manage_log) can tell
    # a deliberate "delete this" from a bare re-interpreted photo (config guard).
    _st["caption"] = combined_body or ""

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
        # standing Tue/Thu reminder." — end_turn, no tool call, texted verbatim). Order:
        # (1) SALVAGE — when the plan is followed by the real line in its own paragraph
        # (live 2026-10-02, founder msg 5656: two paragraphs of plan, then "Bad day.
        # I'll back off."), keep just that line and send it as a normal reply; else
        # (2) ONE forced follow-up with a path for both branches: act with the tools,
        # then send the real words (or the silent sentinel); (3) a repeat is dropped,
        # never sent.
        from agent_tools import looks_like_narration, salvage_direct_reply, REACTION_ONLY_SENTINEL
        if text and looks_like_narration(text):
            kept = salvage_direct_reply(text) if config.NARRATION_SALVAGE_ENABLED else None
            if kept:
                logger.warning("AGENT_LOOP_NARRATION_SALVAGED user=%s iter=%d kept=%r dropped_chars=%d",
                               user.id, i, kept, len(text) - len(kept))
                text = kept
            elif not state.get("narration_nudged"):
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
            else:
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
            _persist_recent_photo(user.id, image_data, combined_body, text)
            if config.LOWERCASE_REPLIES_ENABLED:
                from voice_norm import lowercase_lead
                lowered = lowercase_lead(text)
                if lowered != text:
                    logger.info("AGENT_LOOP_LOWERCASED user=%s before=%r", user.id, text[:60])
                    text = lowered
            # Restatement guard (layer B): a no-write turn that restates the reply sent
            # moments ago becomes an ack, never a second "already in — ur at 555".
            return _apply_restatement_guard(user, text, combined_body, state)

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
