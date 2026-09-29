"""
Phase 4 — the heartbeat: a dumb clock and a smart decision. Per active user, on a
tick, guardrails run IN CODE first (a violating tick never reaches a model); a cheap
rules pre-gate resolves obvious silence; then one full-context decision call answers
"would a good coach say something right now, or stay silent?" Default silent.

Guardrails are limits, not behavior. The model composes freely when it speaks; it
never talks its way past a guardrail. Every tick logs its decision + reason — and the
recent tick history + today's outbound feed the next tick so the coach can't
re-conclude and re-send the same nudge (the anti-repetition signal, distinct from the
daily cap).
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import anthropic
import config
from cost_tracking import track
from models import get_session, User, Message, HeartbeatTick, Workout, WorkoutSession, Meal, Event, active
from sms import send_sms
from agent_loop import build_loop_context, _voice_prompt, _join_text
from llm_client import make_client

logger = logging.getLogger("cued.heartbeat")
client = make_client()

STAY_SILENT_TOOL = {
    "name": "stay_silent",
    "description": ("Call this to stay silent on this tick — a good coach mostly says "
                    "nothing. Pass a one-line reason (e.g. 'nothing new to add', "
                    "'already nudged about the skip today')."),
    "input_schema": {"type": "object",
                     "properties": {"reason": {"type": "string"}},
                     "required": ["reason"]},
}

# Speaking is a tool call too, NOT bare text. Burn-in finding: with only a
# stay_silent tool offered, a model that had DECIDED to speak still reflexively
# called stay_silent (reason: "actually should speak — but tool forces silence"),
# because "emit text" fought its strong prior to use an available tool. Making both
# outcomes explicit tools removes the trap — the model picks the action, it never
# has to route a speak decision through the silence channel. (Response-shape seam.)
SEND_TEXT_TOOL = {
    "name": "send_text",
    "description": ("Call this to send ONE proactive SMS to the user right now. Pass the "
                    "exact message to send in `message` — in your voice, nothing else, no "
                    "preamble. Use this whenever you've decided a text is warranted."),
    "input_schema": {"type": "object",
                     "properties": {"message": {"type": "string"}},
                     "required": ["message"]},
}

HEARTBEAT_PROMPT = """It's a quiet moment — a heartbeat tick, NOT a reply. The user did not just text you.

Decide: would a genuinely good friend-who-coaches text right now, or stay silent? DEFAULT SILENT — mostly you say nothing. But silence is the default, not the goal, and the question is NOT only "is something wrong?" — it's "would a real friend reach out here?" A friend calls out a slip AND marks a win, checks in on your week, passes along something that's actually relevant to you. You're a presence they're glad to hear from, not a tracker that only pings on failures.

A HEARTBEAT HAS NO NEW MESSAGE — that is what makes it proactive. Do NOT wait for something new to have happened before you speak. A STANDING CONDITION is a valid reason to text on its own: a days-long training gap, a broken pattern the user asked to be held to, an open thread still unanswered, a win worth marking. The longer a warranted nudge goes unsent, the MORE worth sending it is, not less. "Nothing new since the last tick" is NOT a reason to stay silent — that is true on every tick by design. Neither is "too early — better closer to the day" for a real upcoming event you haven't acknowledged: you cannot schedule a future text, so this tick is the one you have; the anti-repetition machinery already guarantees speaking now can't cost you a double-text later.

SPEAK when — accountability (the core wedge, non-negotiable):
- a multi-day skip or broken pattern for someone working toward consistency, or who asked to be called out / held accountable — an obvious yes
- a MEAL GAP block (when present): an unlogged stretch of their day is a valid reason to speak on its own — once per gap, one short line about food, never a second ask for the same gap

SPEAK when — warmth & presence (a friend, not a nag):
- a GENUINE win worth marking — a real, specific streak or goal hit (see MOMENTUM when present): "5 sessions this week, that's the consistency that actually sticks." Real progress, never participation-trophy praise.
- a RELEVANT bit of their world — something tied to their known goals, schedule, or interests that a friend would actually pass along. Grounded in what you remember about them.
- a TIMELY check-in — following up on something THEY mentioned (an event, a hard week, a stated intention): "how'd the summit go." BEFORE a known event counts the same: "how's the pitch prep going" / "good luck tomorrow." An upcoming real event you haven't acknowledged yet is check-in material NOW, not "closer to the day" — you cannot schedule a future text, so a deferred check-in is one that mostly never happens. Only where X is real and known.
- LEVITY or ease on a hard day — when the context shows a rough stretch, a friend lightens or backs off, doesn't pile on.
- a MORNING OPEN or EVENING CLOSE block (when present): one short line — a friend's morning text or evening check, never a briefing; once per day each

THE BAR FOR WARMTH IS HIGHER, NOT LOWER. A warranted accountability nudge is almost never unwelcome; generic warmth is exactly how proactive bots become insufferable ("hope you're having a great day!", "fun fact: bananas are berries"). So reach for a warm text ONLY when there is specific, real material about THIS person to make it land — their win, their event, their week, grounded in memory. If all you have is a generic pleasantry with nothing specific behind it, STAY SILENT. "How'd the summit go" (grounded in a known event) speaks; "hope your day's going well" (grounded in nothing) does not.

STAY SILENT when:
- a quiet, on-track day with nothing standing, no notable win, and no specific warm material — the honest default (a modest, unremarkable few workouts is NOT a win to text about)
- the user is mid-conversation — THEIR last message is minutes old (reactive territory: reply in-thread, don't proactively double-text). Judge this ONLY by TIME SINCE THEIR LAST MESSAGE below, never by where the transcript ends: a tick only reaches you at all when their last text is over half an hour old. A question YOU asked that has sat unanswered for HOURS is not a live exchange — it is an open thread, which is SPEAK material (nudge it, or say the next real thing), and "waiting on their reply" copied forward from TICK HISTORY does not become truer with time.
- you already sent this thought, or recently decided to stay silent on it (see RECENT PROACTIVE MESSAGES / TICK HISTORY below when present) — never send the same nudge twice, and never open like your last few texts. But re-VERIFY a held-over reason against the timestamps below before reusing it: a reason like "mid-conversation" or "waiting on reply" expires as hours pass.
- an OTHER FOOD LOGGER block is present and today's food log is empty: for them an empty day is NOT a gap and never a standing condition. The only food nudge allowed is ONE evening ask ("send me ur day when ur done") if no screenshot arrived today — never a morning/midday "what have you eaten".
- NOT because of an OPEN THREAD marked EXPIRED: a question you asked on a previous day is over — it is neither a reason to stay silent ("still their turn") nor something to re-ask. Judge today on today's merits (a TRAINING GAP or a real check-in still speaks); when you do speak, never open by reviving yesterday's detail.

DON'T RATION YOURSELF. The limits on over-texting are enforced in CODE, not by you: at most one unanswered proactive nudge at a time (an anti-stack window) and a hard daily cap. You cannot over-text past those. So do NOT hold back out of fear of nagging — that is already handled. Your only job is the single judgment call: is THIS worth a text right now? Answer that honestly and act on it.

Accountability is the job; fun is the delivery, not a substitute for it. One text. No preamble.

Call EXACTLY ONE tool:
- To SPEAK: call send_text with the exact SMS to send, in your voice — nothing else.
- To STAY SILENT: call stay_silent with a one-line reason.
If any part of your reasoning concludes a text is warranted, call send_text. NEVER call stay_silent and then say in the reason that you should have spoken — that is a contradiction; call send_text instead."""


# Wearable-aware decision guidance (build item 1). Appended to HEARTBEAT_PROMPT in `decide`
# only when HEARTBEAT_WEARABLE_AWARE_ENABLED, so the prompt is byte-for-byte unchanged for
# everyone with the flag off. Tells the model to ACT on the WEARABLE / RECOVERY blocks
# rather than recite them; degrades to a no-op when there is no wearable data.
_WEARABLE_GUIDANCE = """WEARABLE / RECOVERY — act on it, don't recite it (only when a WEARABLE or RECOVERY block is present below):
- A measured SHORT or POOR night → prefer a gentler check-in, and HOLD a demanding accountability nudge (a hard push, a "why'd you skip") for a better day. A real friend eases up after a rough night. This is a tone choice, NOT a license to invent a text out of nothing.
- A clearly GOOD-recovery day (well-rested / recovered strong vs baseline) → a warm win hook, or framing it as a good day to PUSH (go a little harder, chase a PR, take the harder session), lands well IF there's real, specific material for it (an open training thread, today's planned session). Never generic praise from the numbers alone, and it's an OPENING not a demand — a good-recovery day never becomes a hard "why'd you skip".
- LOW steps on a REST day → an optional, warm movement nudge (a short walk), never a scold.
- Never diagnose from HR/HRV, never call it a health issue; bring up one number only when it changes the plan. Missing or absent wearable data changes nothing — decide exactly as you would today."""


def _naive_utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _log_tick(user_id, spoke, reason, message=None, search=None):
    search = search or {}
    session = get_session()
    try:
        session.add(HeartbeatTick(user_id=user_id, spoke=spoke, reason=reason, message=message,
                                  search_available=search.get("available", False),
                                  search_used=search.get("used", False),
                                  search_query=search.get("query")))
        session.commit()
    finally:
        session.close()
    # stop= is the decide call's final stop_reason (None = no model call, e.g. a
    # guardrail tick) — it distinguishes a truncated tick from chosen silence.
    logger.info("HEARTBEAT_TICK user=%s spoke=%s reason=%s stop=%s search_available=%s search_used=%s",
                user_id, spoke, reason, search.get("stop"),
                search.get("available", False), search.get("used", False))


def _local_day_start_utc(user):
    try:
        tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
    except Exception:
        tz = ZoneInfo("America/Los_Angeles")
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


_HOUR_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)


def _parse_hour(s) -> int | None:
    """Best-effort local hour (0–23) from a free-text time field, else None."""
    if not s:
        return None
    m = _HOUR_RE.search(str(s).strip().lower())
    if not m:
        return None
    h, ap = int(m.group(1)), (m.group(3) or "").lower()
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return h if 0 <= h <= 23 else None


def _measured_sw_hours(user, session, *, now=None):
    """(bed_hm, wake_hm) local from the MEASURED sleep window (typical bed + wake from
    recent wearable sleep), or None → the caller uses the static profile times exactly as
    today. Flag-gated by MEASURED_SLEEP_WINDOW_ENABLED; fail-open on any error."""
    if not config.MEASURED_SLEEP_WINDOW_ENABLED or session is None:
        return None
    try:
        from wearable_read import measured_sleep_window
        w = measured_sleep_window(user, session, now=now)
        if not w:
            return None
        return w.bed_hm, w.wake_hm
    except Exception:
        return None


def _quiet_window(user, measured=None) -> tuple[int, int]:
    """The overnight quiet window (start_evening_hour, end_morning_hour), local. The
    default 9pm–8am is a FLOOR: a parseable sleep_time earlier than 9pm or a wake_time
    later than 8am (up to 2pm) only EXTENDS it (more protective), never shrinks it. When a
    MEASURED window is supplied (typical bed/wake from the watch), its hours REPLACE the
    static profile hours here — routed through the exact same FLOOR extension, never a
    weakening."""
    start, end = config.HEARTBEAT_QUIET_START_HOUR, config.HEARTBEAT_QUIET_END_HOUR
    if measured is not None:
        hs, hw = measured[0][0], measured[1][0]
    else:
        hs = _parse_hour(getattr(user, "sleep_time", None))
        hw = _parse_hour(getattr(user, "wake_time", None))
    if hs is not None and 12 <= hs <= 23 and hs < start:   # sleeps earlier than 9pm
        start = hs
    # Up to 2pm: a 1pm waker (live 2026-09-22, user 42: sleeps 4am, up 1pm) was
    # capped at the noon boundary and could be texted at 9am while asleep.
    if hw is not None and 0 <= hw <= 14 and hw > end:      # wakes later than 8am
        end = hw
    return start, end


def _now_aware() -> datetime:
    """The heartbeat's clock (aware UTC). Patched in tests to pin a local wall time."""
    return datetime.now(timezone.utc)


def _ref(now) -> datetime:
    """An aware-UTC reference instant: `now` (aware or naive-UTC) when given, else the clock."""
    if now is None:
        return _now_aware()
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _user_tz(user) -> ZoneInfo:
    try:
        return ZoneInfo(user.user_timezone or "America/Los_Angeles")
    except Exception:
        return ZoneInfo("America/Los_Angeles")


_HHMM_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def _parse_hhmm(s) -> tuple[int, int] | None:
    """STRICT 'HH:MM' → (h, m); a free phrase ('around 7', 'late') is None. The daily-rhythm
    pieces only trust a time the extractor stored as a clock value."""
    m = _HHMM_RE.match(str(s or ""))
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    return (h, mm) if 0 <= h <= 23 and 0 <= mm <= 59 else None


def _wake_hhmm_for(user, local_date) -> tuple[int, int] | None:
    """Their wake (h, m) on this weekday: wake_time_alt when the day is in wake_days_alt."""
    alt = _parse_hhmm(getattr(user, "wake_time_alt", None))
    days = (getattr(user, "wake_days_alt", None) or "").strip().lower()
    if alt and days:
        tokens = [t[:3] for t in re.split(r"[\s,/]+", days) if t]
        if _DAY_ABBR[local_date.weekday()] in tokens:
            return alt
    return _parse_hhmm(getattr(user, "wake_time", None))


def _sleep_hhmm(user) -> tuple[int, int] | None:
    return _parse_hhmm(getattr(user, "sleep_time", None))


QUIET_BEFORE_SLEEP_MIN = 30
QUIET_AFTER_WAKE_MIN = 15


def _profile_quiet_window(user, local, measured=None) -> tuple[int, int] | None:
    """(start, end) minutes-of-day: sleep−30min .. wake+15min (alt wake honoured for the
    local date's weekday). None unless BOTH profile times are strict 'HH:MM'. A supplied
    MEASURED window (typical bed/wake from the watch) REPLACES the profile times — same
    −30/+15 buffers, same gate."""
    if measured is not None:
        sleep, wake = measured[0], measured[1]
    else:
        wake = _wake_hhmm_for(user, local.date())
        sleep = _sleep_hhmm(user)
    if not wake or not sleep:
        return None
    start = (sleep[0] * 60 + sleep[1] - QUIET_BEFORE_SLEEP_MIN) % 1440
    end = (wake[0] * 60 + wake[1] + QUIET_AFTER_WAKE_MIN) % 1440
    return start, end


def _measured_wake_min(user, session, *, now=None) -> int | None:
    """Minutes-of-day (local) the morning quiet floor should extend to when the WATCH shows
    the user woke LATER than their stated wake this morning — so we don't ping someone the
    watch says is still asleep. Conservative + fail-open + flag-gated; None means 'no
    extension, use today's window exactly'. Reads wearable_days via the read-only helper;
    the sync pipeline is never touched."""
    if not config.HEARTBEAT_WEARABLE_AWARE_ENABLED or session is None:
        return None
    try:
        from wearable_read import recovery_read
        rec = recovery_read(user, session, now=now)
        if not rec or not rec.fresh or rec.sleep_end_local is None:
            return None
        local = _ref(now).astimezone(_user_tz(user))
        se = rec.sleep_end_local
        # Only trust a measured wake that lands on TODAY's local morning (a sleep that
        # ended this morning). A stale/older sleep_end tells us nothing about right now.
        if se.date() != local.date():
            return None
        # Same small after-wake buffer the profile window uses, capped at 2pm — matching
        # the existing _quiet_window late-wake cap so we never over-extend from a bad read.
        return min(se.hour * 60 + se.minute + QUIET_AFTER_WAKE_MIN, 14 * 60)
    except Exception:
        # Fail-open: a wearable read must never crash the quiet-hours gate.
        return None


def _in_standing_quiet_hours(user, *, now=None, session=None) -> bool:
    """True if it's currently the user's overnight quiet window (local). `now` is an
    optional aware/naive-UTC instant for tests. With QUIET_HOURS_FROM_PROFILE_ENABLED the
    window is THEIRS (sleep−30 .. wake+15) whenever both profile times parse; otherwise
    the global floor window (extended, never shrunk, by a parseable profile time). When a
    `session` is given and HEARTBEAT_WEARABLE_AWARE_ENABLED, a measured wake the watch
    shows LATER than their stated wake extends the morning floor too (never shrinks it).
    With MEASURED_SLEEP_WINDOW_ENABLED and enough fresh nights, the user's MEASURED typical
    bed/wake hours REPLACE the static profile hours fed into the window (same gate)."""
    if not config.HEARTBEAT_STANDING_QUIET_ENABLED:
        return False
    local = _ref(now).astimezone(_user_tz(user))
    m = local.hour * 60 + local.minute
    measured = _measured_sw_hours(user, session, now=now)   # None → static profile times
    wake_min = _measured_wake_min(user, session, now=now)   # None unless the watch says slept-in
    if config.QUIET_HOURS_FROM_PROFILE_ENABLED:
        win = _profile_quiet_window(user, local, measured=measured)
        if win:
            start, end = win
            if wake_min is not None:
                end = max(end, wake_min)        # measured slept-in only EXTENDS the morning floor
            if start == end:
                return False
            if start > end:                     # spans midnight (the normal case)
                return m >= start or m < end
            return start <= m < end             # e.g. sleeps 01:00, wakes 09:00
    start, end = _quiet_window(user, measured=measured)
    start_min, end_min = start * 60, end * 60
    if wake_min is not None:
        end_min = max(end_min, wake_min)        # measured slept-in only EXTENDS the morning floor
    # window always spans midnight (start is evening, end is morning)
    return m >= start_min or m < end_min


# ─── check-in level (daily rhythm §5) ────────────────────────────────────────
# users.checkin_level is the user's own ask ("text me more" / "chill with the texts"),
# written by the set_checkin_level tool. The column is the contract: its effect on the
# cap and on which rhythm conditions render does not depend on the tool flag.

CHECKIN_LEVELS = ("more", "normal", "less")
CHECKIN_CAP_MORE = 8
CHECKIN_CAP_LESS = 2


def _checkin_level(user) -> str:
    v = (getattr(user, "checkin_level", None) or "").strip().lower()
    return v if v in CHECKIN_LEVELS else "normal"


def _max_per_day(user) -> int:
    """The daily proactive cap for THIS user: 'more' → 8, 'less' → 2, else the global cap."""
    level = _checkin_level(user)
    if level == "more":
        return max(config.HEARTBEAT_MAX_PER_DAY, CHECKIN_CAP_MORE)
    if level == "less":
        return min(config.HEARTBEAT_MAX_PER_DAY, CHECKIN_CAP_LESS)
    return config.HEARTBEAT_MAX_PER_DAY


def guardrail_reason(user, session, *, now=None) -> str | None:
    """Return the first guardrail that blocks this tick, or None. Runs in code
    before any model call — the model can't talk past these."""
    # allowlist (burn-in: founder number only)
    if config.HEARTBEAT_ALLOWLIST and user.phone not in config.HEARTBEAT_ALLOWLIST:
        return "not_allowlisted"
    # Waitlisted users are NOT onboarded — never proactively coach them. (They only
    # ever got the "you're on the list" holding message.) Live 2026-09-21: clearing
    # the allowlist swept pending accounts (active=true) into the heartbeat.
    if (getattr(user, "waitlist_status", None) or "") == "pending":
        return "waitlisted"
    # Still onboarding — the profile isn't confirmed and every inbound routes to the
    # onboarding handler, so a proactive text here lands mid-intake and its reply gets
    # treated as an intake answer (live 2026-09-22, user 42: parked at step 2 for an
    # hour with the summary pending). Reminders they explicitly asked for still fire
    # (reminders.py) — those aren't the heartbeat's call.
    if (user.onboarding_step or 0) < 3:
        return "onboarding"
    # Opted out — no proactive contact until they resume with any inbound.
    if config.STOP_OPTOUT_ENABLED and getattr(user, "opted_out", False):
        return "opted_out"
    # Standing overnight quiet hours — no proactive send while they'd be asleep. This
    # is the always-on floor; quiet_until (a transient goodnight) is checked below too.
    if _in_standing_quiet_hours(user, now=now, session=session):
        return "quiet_hours_standing"
    # quiet hours (goodnight / quiet_until, naive UTC)
    if user.quiet_until and _naive_utcnow() < user.quiet_until:
        return "quiet_hours"
    day_start = _local_day_start_utc(user)
    # daily cap on proactive messages
    spoke_today = (session.query(HeartbeatTick)
                   .filter(HeartbeatTick.user_id == user.id, HeartbeatTick.spoke.is_(True),
                           HeartbeatTick.decided_at >= day_start).count())
    if spoke_today >= _max_per_day(user):   # per-user check-in level (more 8 / less 2)
        return "daily_budget"
    # obvious-silence pre-gate: a recent inbound means an active conversation
    last_in = (session.query(Message)
               .filter(Message.user_id == user.id, Message.direction == "in")
               .order_by(Message.created_at.desc()).first())
    if last_in and last_in.created_at:
        age_min = (datetime.now(timezone.utc) - last_in.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
        if age_min < config.HEARTBEAT_ACTIVE_CONVO_MINUTES:
            return "active_conversation"
    # anti-STACK: don't pile a second proactive nudge on an unanswered first, but
    # ONLY within the window and ONLY when the most-recent outbound was itself a
    # proactive nudge. Clears on time-elapse (no user reply needed) — the fix for
    # the unanswered_gap deadlock, where any reactive reply or legacy briefing used
    # to mute all initiation until the user spoke. See rewrite/heartbeat-calibration.
    from engagement_tracker import has_unanswered_proactive
    if has_unanswered_proactive(user.id, config.HEARTBEAT_STACK_WINDOW_MINUTES):
        return "proactive_stack"
    # FLOOR events hard-gate (deterministic): don't interrupt someone who is provably
    # mid-class. Only the high-precision regex floor gates here; a MODEL-logged event
    # ("summit 12-2:30") deliberately does NOT hard-gate — it informs the decision call
    # via context instead (deterministic guardrails, model decisions). See in_class_now.
    from events import in_class_now
    if in_class_now(user.id):
        return "in_class"
    # Connected-calendar block (class/exam/work) ongoing or starting within 90 min —
    # don't fire a proactive nudge into it, same as the sleep window (spec §1.3).
    from events import calendar_block_soon
    if calendar_block_soon(user.id):
        return "calendar_block"
    return None


def _search_available(user, session) -> bool:
    """Is web_search offered on this tick? Kill switch first; then the per-day
    budget — searched ticks (the model actually invoked search, i.e. spend) in the
    user's LOCAL day, derived from HeartbeatTick.search_used rather than a counter
    that could drift. Guardrail class: enforced here in code, before the tool ever
    reaches the model. At/over budget the tick still runs without the tool."""
    if not config.HEARTBEAT_WEB_SEARCH:
        return False
    from timefmt import local_day_bounds
    start, end = local_day_bounds(user)
    searched_today = (session.query(HeartbeatTick)
                      .filter(HeartbeatTick.user_id == user.id,
                              HeartbeatTick.search_used.is_(True),
                              HeartbeatTick.decided_at >= start,
                              HeartbeatTick.decided_at < end).count())
    return searched_today < config.HEARTBEAT_SEARCH_MAX_PER_DAY


def _recent_win_signal(user, session) -> str | None:
    """WARMTH material (Part 2), code-computed. Completed workouts in the last 7 days —
    the model can't be trusted to count this (date arithmetic; the playbook's 'the model
    quotes, code computes'), and RECENT WORKOUTS lists rows without a window total. This
    is the raw fact for a 'genuine win worth marking' text; the model judges whether the
    run is actually notable (a strong week) or thin (not a win — maybe an accountability
    signal instead). Neutral facts only — no 'great job', or code would be praising for
    the model. Returns None when there's nothing recent (clean-absent, no placeholder)."""
    cutoff = _naive_utcnow() - timedelta(days=7)
    done = (active(session, Workout, user_id=user.id)
            .filter(Workout.completed.is_(True), Workout.date >= cutoff)
            .order_by(Workout.date).all())
    if not done:
        return None
    types = ", ".join(w.workout_type or "workout" for w in done)
    return (f"## MOMENTUM (last 7 days — code-computed; a genuinely strong run may be worth "
            f"marking, a thin or falling-off one is NOT a win)\n"
            f"Completed {len(done)} workout(s) in the last 7 days: {types}.")


def _recovery_signal(user, session) -> str | None:
    """RECOVERY standing-condition hint (wearable), the PROACTIVE twin of the reactive
    WEARABLE block. Reads last night's sleep vs the user's 7-day baseline and resting-HR /
    HRV "worse than baseline" EXACTLY like the reactive flag, via the read-only wearable_read
    helper — the google_health sync module is never touched. Renders only on a real recovery
    read (a clearly POOR or clearly GOOD night); a middling night is None (no noise). The
    guidance is a SOFT tone gate (downgrade/hold a demanding nudge), never a hard suppress.
    Flag-gated; fail-open (any error or missing/stale data → None, the tick proceeds)."""
    if not config.HEARTBEAT_WEARABLE_AWARE_ENABLED:
        return None
    try:
        from wearable_read import recovery_read, _hm
        rec = recovery_read(user, session)
    except Exception:  # noqa: BLE001
        return None
    if not rec or not rec.fresh:
        return None
    # The GOOD-recovery positive framing is the new symmetric side — gate it on
    # BETTER_RECOVERY_ENABLED. With that flag off we behave exactly as before it shipped:
    # only the POOR side (soften/hold) speaks. The poor side is never gated here.
    show_good = rec.good_recovery and config.BETTER_RECOVERY_ENABLED
    if not (rec.poor_recovery or show_good):
        return None
    bits = []
    if rec.sleep_minutes is not None:
        s = f"last night {_hm(rec.sleep_minutes)}"
        if rec.sleep_baseline:
            s += f" (7-day avg {_hm(rec.sleep_baseline)})"
        bits.append(s)
    worse = []
    if rec.rhr_worse:
        worse.append("resting HR")
    if rec.hrv_worse:
        worse.append("HRV")
    if worse:
        bits.append(" and ".join(worse) + " worse than baseline")
    better = []
    if rec.rhr_better:
        better.append("resting HR")
    if rec.hrv_better:
        better.append("HRV")
    if rec.sleep_better:
        better.append("sleep")
    if better and config.BETTER_RECOVERY_ENABLED and not worse:
        bits.append(" and ".join(better) + " better than baseline")
    if rec.steps_today is not None:
        st = f"steps today {rec.steps_today:,}"
        if rec.steps_baseline:
            st += f" (7-day avg {int(rec.steps_baseline):,})"
        bits.append(st)
    facts = "; ".join(bits) if bits else "recent wearable data on file"
    if rec.poor_recovery:
        guidance = ("Recovery reads POOR. SOFT tone gate: if you'd otherwise send a demanding "
                    "accountability nudge (a hard push, a 'why'd you skip'), DOWNGRADE it to a gentle "
                    "check-in or HOLD it today — a real friend eases up after a rough night, doesn't pile "
                    "on. NOT a hard block: a genuine open thread, a safety item, or real warm material "
                    "still speaks. One number only if it changes the plan; never diagnose from HR/HRV.")
    else:
        guidance = ("Recovery reads GOOD (well-rested / recovered strong vs baseline). A warm win hook, or "
                    "framing today as a good day to PUSH — go a little harder, chase a PR, take the harder "
                    "session — lands well IF there's specific, real material for it (an open training thread, "
                    "today's planned session). Never generic praise from the numbers alone, and NOT a demand: "
                    "it's a positive framing/opening, not a hard obligation. One number only if it changes the "
                    "plan; never diagnose.")
    return ("## RECOVERY (wearable — code-computed; act on it, never a readout)\n"
            f"{facts}.\n{guidance}")


_DAY_ABBR = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _committed_per_week(user) -> int:
    """Their stated training frequency, as an int: named days ("mon/wed/fri" → 3), a
    number or range ("3-4" → the LOW end: the commitment they'd defend), default 3."""
    raw = (user.confirmed_training_days or user.workout_days or "").strip().lower()
    if not raw:
        return 3
    tokens = re.split(r"[\s,/&+]+", raw)
    named = {t[:3] for t in tokens if t[:3] in _DAY_ABBR}
    if named:
        return max(1, min(7, len(named)))
    nums = [int(n) for n in re.findall(r"\d+", raw)]
    if nums:
        return max(1, min(7, min(nums)))
    return 3


def _training_gap_signal(user, session) -> str | None:
    """ACCOUNTABILITY material, code-computed (the playbook: the model can't do date
    arithmetic from a transcript). Days since the last COMPLETED workout — or since
    they joined when there is none — against the frequency they committed to. Live
    2026-09-14→19 (user 32): five days, zero workouts, and not one training mention
    from the coach, because no block ever said so. Renders once the gap exceeds the
    spacing their frequency implies (ceil(7/n)+1 days); None below that."""
    per_week = _committed_per_week(user)
    threshold = math.ceil(7 / per_week) + 1
    now = _naive_utcnow()
    last_w = (active(session, Workout, user_id=user.id)
              .filter(Workout.completed.is_(True)).order_by(Workout.date.desc()).first())
    last_s = (session.query(WorkoutSession)
              .filter(WorkoutSession.user_id == user.id, WorkoutSession.status == "done")
              .order_by(WorkoutSession.finished_at.desc().nullslast(), WorkoutSession.date.desc()).first())
    stamps = [d for d in ((last_w.date if last_w else None),
                          ((last_s.finished_at or last_s.date) if last_s else None)) if d]
    if stamps:
        since = max(stamps)
        gap_days = (now - since).days
        basis = f"last completed workout: {gap_days} days ago"
    else:
        since = user.activated_at or user.created_at
        if not since:
            return None
        gap_days = (now - since).days
        basis = f"no completed workout on record — they joined {gap_days} days ago and have never trained with you"
    if gap_days < threshold:
        return None
    card = (session.query(WorkoutSession)
            .filter(WorkoutSession.user_id == user.id, WorkoutSession.status != "done",
                    WorkoutSession.date >= since)
            .order_by(WorkoutSession.date.desc()).first())
    card_line = ""
    if card and card.date:
        card_line = (f" A {card.template_key.replace('_', ' ')} session card was sent "
                     f"{(now - card.date).days} days ago and never finished.")
    setup = (user.equipment or "").replace("_", " ").strip()
    setup_line = f" Their setup: {setup}." if setup else ""
    return ("## TRAINING GAP (standing condition — code-computed)\n"
            f"They committed to {per_week} workouts/week. {basis}.{card_line}{setup_line} "
            "This is accountability material on its own — a warranted nudge here is the job: "
            "one text, specific to their setup, no lecture, and offer the next session rather "
            "than an inquest.")


_QUESTION_OPENERS = {"how", "what", "which", "when", "where", "who", "why", "did", "do",
                     "does", "can", "could", "would", "have", "has", "wanna", "want"}


def _looks_like_question(body: str) -> bool:
    b = (body or "").strip().lower()
    if not b:
        return False
    if "?" in b:
        return True
    first = re.split(r"[\s,]+", b, maxsplit=1)[0]
    return first in _QUESTION_OPENERS


def _open_thread_signal(user, session) -> str | None:
    """The coach's own unanswered question, code-dated. Same age → same standing (an
    hours-old question is an open thread, SPEAK material — the stale-thread anchor);
    but a question from a PREVIOUS local day is EXPIRED: live 2026-09-18→19 the tick
    history carried "open mcmuffin question, still her turn" for 20 ticks and the next
    morning's reply reopened it, ahead of that day's food and training."""
    from engagement_tracker import _not_reaction
    last_out = (session.query(Message)
                .filter(Message.user_id == user.id, Message.direction == "out", _not_reaction())
                .order_by(Message.created_at.desc()).first())
    if not last_out or not last_out.created_at or not _looks_like_question(last_out.body):
        return None
    answered = (session.query(Message.id)
                .filter(Message.user_id == user.id, Message.direction == "in",
                        Message.created_at > last_out.created_at).first())
    if answered:
        return None
    from timefmt import local_day_bounds
    day_start, _ = local_day_bounds(user)
    hrs = (_naive_utcnow() - last_out.created_at).total_seconds() / 3600
    quoted = (last_out.body or "").strip().replace("\n", " ")[:120]
    if last_out.created_at < day_start:
        return ("## OPEN THREAD (code-computed)\n"
                f"Your last message was a question sent ~{hrs:.0f} hours ago, on a PREVIOUS local "
                f"day, and they never answered: \"{quoted}\". It has EXPIRED with that day — do NOT "
                "reopen or re-ask it, and it is NOT a reason to stay silent. If you speak, lead with "
                "today (their day, food, training); if the detail still matters, work with what you have.")
    return ("## OPEN THREAD (code-computed)\n"
            f"Your last message was a question sent ~{hrs:.1f} hours ago today and they haven't "
            f"answered: \"{quoted}\". A question that has sat unanswered for hours is an open "
            "thread, not a live exchange.")


# ─── daily rhythm standing conditions (§1 meal gap, §4 morning open / evening close) ──
# All code-computed in the user's LOCAL time from the profile clock values; the model
# gets hours and counts, never a transcript to do date arithmetic on. Every function
# takes `now=` (aware/naive UTC) so a test can pin the wall clock.

MEAL_GAP_FIRST_MEAL_HOURS = 5        # nothing logged by wake+5h → "no breakfast/lunch yet"
MEAL_GAP_LONG_HOURS = 6              # ≥6h since the last logged meal during waking hours
MEAL_GAP_EVENING_HOURS = 2           # the evening-close window before sleep_time
MEAL_GAP_EVENING_MIN_SINCE_LAST = 3  # ...and the last meal is at least this old
RHYTHM_MORNING_MINUTES = 90          # MORNING OPEN: within 90 min after wake
RHYTHM_EVENING_HOURS = 2             # EVENING CLOSE: within 2h before sleep
RHYTHM_EVENING_REF_HOUR = 17         # ...and no outbound since 5pm local
_DEFAULT_WAKE = (8, 0)
_DEFAULT_SLEEP = (23, 0)


def _clock(local) -> str:
    return local.strftime("%-I:%M%p").replace("AM", "am").replace("PM", "pm")


def _clock_hm(hm) -> str:
    return _clock(datetime(2000, 1, 1, hm[0], hm[1]))


def _naive(local) -> datetime:
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _expected_meals(user) -> int:
    """meals_per_day ('1-2' → 2, '3' → 3, '4+' → 4); default 3."""
    nums = re.findall(r"\d+", str(getattr(user, "meals_per_day", "") or ""))
    return max(1, min(6, int(nums[-1]))) if nums else 3


def _waking_bounds(user, local) -> tuple[datetime, datetime]:
    """(wake, sleep) aware-local instants of the waking day that contains `local` — or the
    one about to start — from the profile clock values (alt wake by weekday), defaulting
    to 08:00 / 23:00 when they don't parse. A sleep at/before the wake hour (00:00,
    01:00) means bed is after midnight, so the waking day runs into the next date."""
    def _for(date):
        wake = _wake_hhmm_for(user, date) or _DEFAULT_WAKE
        sleep = _sleep_hhmm(user) or _DEFAULT_SLEEP
        w = local.replace(year=date.year, month=date.month, day=date.day, hour=wake[0], minute=wake[1],
                          second=0, microsecond=0)
        s = local.replace(year=date.year, month=date.month, day=date.day, hour=sleep[0], minute=sleep[1],
                          second=0, microsecond=0)
        if s <= w:
            s += timedelta(days=1)
        return w, s
    w, s = _for(local.date())
    if local < w:   # after midnight: still yesterday's waking day if it runs past midnight
        yw, ys = _for((local - timedelta(days=1)).date())
        if local < ys:
            return yw, ys
    return w, s


def _meal_gap_signal(user, session, *, now=None) -> str | None:
    """§1 MEAL GAP, code-computed. Renders when (a) nothing is logged by wake+5h, (b) ≥6h
    have passed since the last logged meal during waking hours, or (c) it's the last 2h
    before bed with fewer meals than they said they eat and ≥3h since the last one.
    Carries hours since the last meal, today's count vs expected, and whether a proactive
    text already went out during THIS gap (once per gap). Softer branch for a user who
    logs in their own app (food_logger_status == 'coexist'). None outside waking hours,
    with the flag off, or for a 'less' check-in level."""
    if not config.HEARTBEAT_MEAL_GAP_ENABLED or _checkin_level(user) == "less":
        return None
    local = _ref(now).astimezone(_user_tz(user))
    wake_dt, sleep_dt = _waking_bounds(user, local)
    if local < wake_dt or local >= sleep_dt:
        return None
    wake_utc, now_utc = _naive(wake_dt), _naive(local)
    today = (active(session, Meal, user_id=user.id)
             .filter(Meal.eaten_at >= wake_utc, Meal.eaten_at <= now_utc)
             .order_by(Meal.eaten_at).all())
    last = (active(session, Meal, user_id=user.id)
            .filter(Meal.eaten_at <= now_utc).order_by(Meal.eaten_at.desc()).first())
    hrs_last = (now_utc - last.eaten_at).total_seconds() / 3600 if last and last.eaten_at else None
    hrs_wake = (local - wake_dt).total_seconds() / 3600
    count, expected = len(today), _expected_meals(user)
    last_desc = (last.description or "").strip().replace("\n", " ")[:60] if last else ""

    kind = None
    if (local >= sleep_dt - timedelta(hours=MEAL_GAP_EVENING_HOURS) and count < expected
            and (hrs_last is None or hrs_last >= MEAL_GAP_EVENING_MIN_SINCE_LAST)):
        kind = "evening"
    elif count == 0 and hrs_wake >= MEAL_GAP_FIRST_MEAL_HOURS:
        kind = "first"
    elif count > 0 and hrs_last is not None and hrs_last >= MEAL_GAP_LONG_HOURS:
        kind = "gap"
    if not kind:
        return None

    if kind == "first":
        lead = (f"Nothing logged since they woke (~{_clock(wake_dt)}, {hrs_wake:.1f}h ago) — no breakfast "
                f"or lunch on record.")
    elif kind == "gap":
        lead = f"Last logged meal was {hrs_last:.1f}h ago ({last_desc}), during waking hours."
    else:
        since = f"{hrs_last:.1f}h since the last logged meal ({last_desc})" if hrs_last is not None \
            else "nothing logged at all"
        lead = (f"Evening close — bed at ~{_clock(sleep_dt)}, {since}, and the day is short of what they "
                f"usually eat: anything they ate and didn't log?")
    tail = ""
    if kind == "first" and last is not None:
        tail = f"; last logged meal {hrs_last:.1f}h ago"
    elif kind == "first":
        tail = "; no meal logged yet since they joined"
    counts = f"Logged since they woke: {count} of ~{expected} meals{tail}."

    # once per gap, code-dated: a proactive text already sent since the later of wake /
    # the last meal means this gap has been raised.
    since = max([wake_utc] + ([last.eaten_at] if last and last.eaten_at else []))
    spoke_since = (session.query(HeartbeatTick)
                   .filter(HeartbeatTick.user_id == user.id, HeartbeatTick.spoke.is_(True),
                           HeartbeatTick.decided_at >= since, HeartbeatTick.decided_at <= now_utc).count())
    repeat = ("No proactive text yet during this gap." if not spoke_since else
              "You ALREADY sent a proactive text during this gap — do not ask again; this block is "
              "not a reason to speak twice.")

    if (getattr(user, "food_logger_status", None) or "") == "coexist":
        guidance = ("They log food in their own app (coexist), so it's probably logged there, not here. "
                    "Once per gap: if you speak, ask for a screenshot of their day so far — never a re-type "
                    "of what they ate.")
    else:
        guidance = ("An unlogged stretch is a valid reason to speak on its own, once per gap: one short "
                    "line asking what they've eaten or whether they ate — not a lecture, not a macro rundown.")
    return ("## MEAL GAP (standing condition — code-computed)\n"
            f"It's {_clock(local)} their time. {lead} {counts} {repeat}\n{guidance}")


def _training_days(user) -> set:
    raw = (getattr(user, "confirmed_training_days", None) or getattr(user, "workout_days", None) or "").lower()
    return {t[:3] for t in re.split(r"[\s,/&+]+", raw) if t} & set(_DAY_ABBR)


def _morning_open_signal(user, session, *, now=None) -> str | None:
    """§4 MORNING OPEN: within RHYTHM_MORNING_MINUTES after a parseable wake (alt honoured)
    and no non-reaction message either way since they woke. One line of material: the
    weekday, workout/rest day per their split, today's logged events."""
    if not config.HEARTBEAT_RHYTHM_ENABLED or _checkin_level(user) == "less":
        return None
    local = _ref(now).astimezone(_user_tz(user))
    wake = _wake_hhmm_for(user, local.date())
    if not wake:
        return None
    wake_dt = local.replace(hour=wake[0], minute=wake[1], second=0, microsecond=0)
    if not (wake_dt <= local < wake_dt + timedelta(minutes=RHYTHM_MORNING_MINUTES)):
        return None
    from engagement_tracker import _not_reaction
    talked = (session.query(Message.id)
              .filter(Message.user_id == user.id, Message.created_at >= _naive(wake_dt), _not_reaction())
              .first())
    if talked:
        return None
    days = _training_days(user)
    today_abbr = _DAY_ABBR[local.weekday()]
    if days:
        plan = ("a workout day" if today_abbr in days else "a rest day") + \
               f" (they train {'/'.join(d for d in _DAY_ABBR if d in days)})"
    else:
        plan = "no fixed training days on file"
    from timefmt import local_day_bounds, to_local
    d0, d1 = local_day_bounds(user, now=_ref(now))
    evs = (active(session, Event, user_id=user.id)
           .filter(Event.occurred_at >= d0, Event.occurred_at < d1).order_by(Event.occurred_at).all())
    ev_txt = ", ".join(f"{(e.raw_text or e.event_type or 'event').strip()[:40]} at "
                       f"{_clock(to_local(e.occurred_at, user))}" for e in evs) or "none logged"
    mins = int((local - wake_dt).total_seconds() // 60)
    base = ("## MORNING OPEN (standing condition — code-computed)\n"
            f"It's {_clock(local)} {local.strftime('%A')}, ~{mins} min after their {_clock_hm(wake)} wake, and "
            f"nobody has texted since they woke. Today: {plan}; events today: {ev_txt}.\n")

    # Feature 4 — daily briefing: extend MORNING OPEN into a concise rundown (classes +
    # due dates + a suggested gym window + nutrition status) in ONE short message. Only
    # when the feature is on; otherwise the friend's-morning-line behaviour is unchanged.
    if config.CALENDAR_ASSISTANT_ENABLED and config.CALENDAR_DAILY_BRIEFING_ENABLED:
        brief = _daily_briefing_extras(user, session, now=now)
        return (base + brief +
                "A daily briefing: ONE short, warm rundown — the day's shape in a couple lines "
                "(what's on, what's due, a good gym window, where nutrition stands), not a dashboard "
                "and not a question stack. Once — if TICK HISTORY / RECENT PROACTIVE MESSAGES show a "
                "morning text already today, this is not a reason to speak.")

    return (base +
            "One short line — a friend's morning text, not a briefing: no plan dump, no totals, no "
            "question stack. Once — if TICK HISTORY / RECENT PROACTIVE MESSAGES show a morning text "
            "already today, this is not a reason to speak.")


def _daily_briefing_extras(user, session, *, now=None) -> str:
    """The extra briefing material MORNING OPEN adds under the daily-briefing feature:
    due dates, a suggested gym window, and nutrition status — all code-computed, read-only,
    fail-open (a failing piece is simply omitted)."""
    lines = []
    if config.WEATHER_ENABLED:
        try:
            from weather import weather_line
            wl = weather_line(user)
            if wl:
                lines.append(f"Weather: {wl}.")
        except Exception as e:  # noqa: BLE001
            logger.warning("BRIEFING_WEATHER_FAILED user=%s err=%s", user.id, e)
    try:
        from schedule import deadline_items
        items = deadline_items(user.id, session, days=7, now=now)
        if items:
            now_utc = _ref(now).astimezone(timezone.utc).replace(tzinfo=None)
            due = "; ".join(f"{d.title} ({'today' if d.days_until(now_utc) < 1 else f'{d.days_until(now_utc):.0f}d'})"
                            for d in items[:3])
            lines.append(f"Due soon: {due}.")
    except Exception as e:  # noqa: BLE001
        logger.warning("BRIEFING_DEADLINES_FAILED user=%s err=%s", user.id, e)
    try:
        from schedule import free_blocks, timed_schedule
        if timed_schedule(user.id, session, now=now):
            blocks = [b for b in free_blocks(user.id, session, now=now) if b.minutes >= 60]
            if blocks:
                b = blocks[0]
                lines.append(f"Suggested gym window: {_cal_local_clock(b.start, user)}–"
                             f"{_cal_local_clock(b.end, user)}.")
    except Exception as e:  # noqa: BLE001
        logger.warning("BRIEFING_GYM_FAILED user=%s err=%s", user.id, e)
    try:
        from timefmt import local_day_bounds
        d0, d1 = local_day_bounds(user, now=_ref(now))
        meals = (active(session, Meal, user_id=user.id)
                 .filter(Meal.eaten_at >= d0, Meal.eaten_at < d1).all())
        cal = sum(m.calories or 0 for m in meals)
        pro = sum(m.protein_g or 0 for m in meals)
        tgt = ""
        if user.calorie_target or user.protein_target:
            tgt = f" (target {user.calorie_target or '?'} cal / {user.protein_target or '?'}g)"
        lines.append(f"Nutrition so far: {cal} cal / {pro}g protein across {len(meals)} meal(s){tgt}.")
    except Exception as e:  # noqa: BLE001
        logger.warning("BRIEFING_NUTRITION_FAILED user=%s err=%s", user.id, e)
    return ("Briefing material: " + " ".join(lines) + "\n") if lines else ""


def _evening_close_signal(user, session, *, now=None) -> str | None:
    """§4 EVENING CLOSE: within RHYTHM_EVENING_HOURS before a parseable sleep_time and no
    non-reaction outbound since 5pm local (the 5pm of the evening that precedes bed, so an
    after-midnight sleeper's 12:15am still counts last evening). One line of material:
    today's totals vs targets, workouts completed, meals logged."""
    if not config.HEARTBEAT_RHYTHM_ENABLED or _checkin_level(user) == "less":
        return None
    local = _ref(now).astimezone(_user_tz(user))
    sleep = _sleep_hhmm(user)
    if not sleep:
        return None
    bed = local.replace(hour=sleep[0], minute=sleep[1], second=0, microsecond=0)
    if bed <= local:
        bed += timedelta(days=1)
    if local < bed - timedelta(hours=RHYTHM_EVENING_HOURS):
        return None
    five = bed.replace(hour=RHYTHM_EVENING_REF_HOUR, minute=0)
    if five >= bed:
        five -= timedelta(days=1)
    from engagement_tracker import _not_reaction
    out = (session.query(Message.id)
           .filter(Message.user_id == user.id, Message.direction == "out",
                   Message.created_at >= _naive(five), _not_reaction())
           .first())
    if out:
        return None
    from timefmt import local_day_bounds
    d0, d1 = local_day_bounds(user, now=_ref(now))
    meals = (active(session, Meal, user_id=user.id)
             .filter(Meal.eaten_at >= d0, Meal.eaten_at < d1).all())
    cal = sum(m.calories or 0 for m in meals)
    pro = sum(m.protein_g or 0 for m in meals)
    targets = ""
    if user.calorie_target or user.protein_target:
        targets = f" vs targets {user.calorie_target or '?'} cal / {user.protein_target or '?'}g"
    n_w = (active(session, Workout, user_id=user.id)
           .filter(Workout.completed.is_(True), Workout.date >= d0, Workout.date < d1).count())
    mins = int((bed - local).total_seconds() // 60)
    return ("## EVENING CLOSE (standing condition — code-computed)\n"
            f"It's {_clock(local)}, ~{mins} min before their {_clock_hm(sleep)} bedtime, and you haven't "
            f"texted since 5pm. Today: {cal} cal / {pro}g protein logged across {len(meals)} meal(s){targets}; "
            f"{n_w} workout{'s' if n_w != 1 else ''} completed.\n"
            "One short line — a friend's evening check, not a briefing: no totals recap unless it's the "
            "one thing worth saying, no tomorrow's plan, no question stack. Once — if TICK HISTORY / "
            "RECENT PROACTIVE MESSAGES show an evening text already, this is not a reason to speak.")


def _checkin_level_block(user) -> str | None:
    if not config.SET_CHECKIN_LEVEL_TOOL_ENABLED and not getattr(user, "checkin_level", None):
        return None
    level = _checkin_level(user)
    return ("## CHECK-IN LEVEL (their choice — code enforces the cap)\n"
            f"{level} — daily proactive cap {_max_per_day(user)}/day. 'more' = they asked to hear from you "
            "more (the rhythm check-ins are on); 'less' = they asked you to chill (only training gaps, open "
            "threads and real wins — no meal-gap / morning / evening check-ins, and keep it short); "
            "'normal' = as designed.")


# ─── calendar assistant standing conditions (read-only on Events) ─────────────
# Each is flag-gated (master CALENDAR_ASSISTANT_ENABLED + its own feature flag),
# code-computed, and FAIL-OPEN (any error / no calendar data → None, the tick
# proceeds exactly as today). They are context ADDITIONS the same decide() weighs —
# never a new send path — so every one still routes through guardrail_reason (daily
# cap, quiet hours, anti-stack, in_class, calendar_block). Read-only on the Event
# store: nothing here writes an event or touches a sync / write-back path.

# Prompt guidance appended to HEARTBEAT_PROMPT in decide() only when the high-load
# tone feature is on — so the prompt is byte-for-byte unchanged with it off. Degrades
# to a no-op when no ACADEMIC LOAD block is present.
_HIGH_LOAD_GUIDANCE = """ACADEMIC LOAD — act on it, don't recite it (only when an ACADEMIC LOAD block is present below):
- An exam or a dense cluster of deadlines within ~48h is a demanding stretch. PRIORITISE sleep and recovery, SOFTEN accountability, and HOLD a demanding training nudge (a hard push, a "why'd you skip") — like the poor-recovery gate, ease up. This is a tone choice, not a license to invent a text out of nothing.
- If you do bring up training, bias toward a LIGHTER / SHORTER session (a quick lift, a walk, a mobility break to reset), never a hard "you're behind".
- Offer real stress/recovery support the night before — a check-in, a nudge to eat and sleep — not a lecture. Combine with the RECOVERY block when both are present.
- Never nag about fitness during their crunch; the deadline is the priority and a good friend knows it."""


def _cal_local_clock(dt_naive_utc, user) -> str:
    """Naive-UTC instant → a short local 'Tue 2:15pm' for calendar rendering."""
    from timefmt import to_local
    loc = to_local(dt_naive_utc, user)
    return loc.strftime("%a %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def _cal_day_clock(dt_naive_utc, user, *, all_day=False) -> str:
    from timefmt import to_local
    loc = to_local(dt_naive_utc, user)
    if all_day:
        return loc.strftime("%a %b %-d")
    return loc.strftime("%a %b %-d, %-I:%M%p").replace("AM", "am").replace("PM", "pm")


def _deadline_radar_signal(user, session, *, now=None) -> str | None:
    """Feature 1 (+ 6). Surface upcoming deadlines + cluster density so the coach can
    PROACTIVELY raise them and offer to help plan — not just answer on demand. None
    when there are no deadlines on the calendar."""
    if not (config.CALENDAR_ASSISTANT_ENABLED and config.CALENDAR_DEADLINE_RADAR_ENABLED):
        return None
    from schedule import deadline_items, cluster_count, deadlines_within
    items = deadline_items(user.id, session, now=now)
    if not items:
        return None
    now_utc = _ref(now).astimezone(timezone.utc).replace(tzinfo=None)
    lines = []
    for d in items[:6]:
        hrs = d.hours_until(now_utc)
        when = (f"in {hrs:.0f}h" if hrs < 48 else f"in {d.days_until(now_utc):.0f}d")
        tag = " [EXAM]" if d.is_exam else ""
        lines.append(f"- {d.title}{tag} — due {_cal_day_clock(d.when, user, all_day=d.all_day)} ({when})")
    n_48 = len(deadlines_within(items, config.CALENDAR_HIGH_LOAD_HOURS, now=now))
    n_week = cluster_count(items, hours=7 * 24, now=now)
    density = f"{n_week} due in the next 7 days" + (f", {n_48} within 48h" if n_48 else "")
    offer = ""
    if config.CALENDAR_DEADLINE_REMINDERS_ENABLED:
        offer = (" You can OFFER to set a reminder to start one of these, or to block study time "
                 "(via the reminders you already set for them) — an offer, once, never an auto-spam.")
    return ("## DEADLINE RADAR (upcoming — code-computed from their calendar)\n"
            + "\n".join(lines) + f"\n{density}.\n"
            "A real upcoming deadline is proactive material NOW, not 'closer to the day': flag the "
            "nearest / a cluster and offer to help plan or break it down — one short line, grounded "
            "in the specific item, not a to-do dump." + offer)


def _free_window_signal(user, session, *, now=None) -> str | None:
    """Feature 2 (gym windows) + Feature 5 (meal timing). Surfaces today's/tomorrow's
    open windows as candidate gym/study time, and flags a long back-to-back run of
    blocks that leaves no eating gap. Inert (None) when the user has no timed calendar
    events — an empty calendar is not a schedule to plan around."""
    if not (config.CALENDAR_ASSISTANT_ENABLED and
            (config.CALENDAR_SCHEDULE_TRAINING_ENABLED or config.CALENDAR_MEAL_TIMING_ENABLED)):
        return None
    from schedule import timed_schedule, free_blocks, busy_runs
    if not timed_schedule(user.id, session, now=now):
        return None
    now_utc = _ref(now).astimezone(timezone.utc).replace(tzinfo=None)
    parts = []
    if config.CALENDAR_SCHEDULE_TRAINING_ENABLED:
        blocks = [b for b in free_blocks(user.id, session, now=now) if b.minutes >= 45][:3]
        if blocks:
            wins = "; ".join(f"{_cal_local_clock(b.start, user)}–{_cal_local_clock(b.end, user)} "
                             f"(~{b.minutes // 60}h{b.minutes % 60:02d}m)" for b in blocks)
            parts.append(f"Open windows (candidate gym/study time): {wins}.")
    if config.CALENDAR_MEAL_TIMING_ENABLED:
        thresh = timedelta(hours=config.CALENDAR_MEAL_TIMING_BLOCK_HOURS)
        long_runs = [(s, e) for (s, e) in busy_runs(user.id, session, now=now)
                     if e - s >= thresh and e > now_utc]
        if long_runs:
            s, e = long_runs[0]
            parts.append(f"Back-to-back blocks {_cal_local_clock(s, user)}–{_cal_local_clock(e, user)} "
                         "leave no real eating gap — worth a heads-up to eat before it.")
    if not parts:
        return None
    return ("## SCHEDULE (today/tomorrow — code-computed, read-only)\n"
            + " ".join(parts) + "\n"
            "Use these to make training/eating fit their real day — suggest a specific window a "
            "friend would ('good gap 2–4, wanna hit legs then?'), or a heads-up to eat before a long "
            "stretch. One short line, only if it's genuinely useful; never a schedule readout.")


def _high_load_signal(user, session, *, now=None) -> str | None:
    """Feature 2 (training soften) + Feature 3 (tone + recovery). On high_load_soon (an
    exam or a dense deadline cluster within ~48h) render a SOFT tone gate — ease training
    intensity, prioritise sleep, soften accountability, offer stress support. Never a hard
    suppress; a real open thread / win still speaks. None when load is not high."""
    if not (config.CALENDAR_ASSISTANT_ENABLED and
            (config.CALENDAR_SCHEDULE_TRAINING_ENABLED or config.CALENDAR_HIGH_LOAD_TONE_ENABLED)):
        return None
    from schedule import deadline_items, high_load_soon, deadlines_within
    items = deadline_items(user.id, session, now=now)
    if not items or not high_load_soon(user.id, session, now=now, items=items):
        return None
    now_utc = _ref(now).astimezone(timezone.utc).replace(tzinfo=None)
    soon = deadlines_within(items, config.CALENDAR_HIGH_LOAD_HOURS, now=now)
    exams = [d for d in soon if d.is_exam]
    if exams:
        d = exams[0]
        basis = f"{d.title} in {d.hours_until(now_utc):.0f}h"
    else:
        basis = f"{len(soon)} deadlines within {config.CALENDAR_HIGH_LOAD_HOURS}h"
    return ("## ACADEMIC LOAD (standing condition — code-computed from their calendar)\n"
            f"High load soon: {basis}. SOFT tone gate: prioritise sleep + recovery, SOFTEN "
            "accountability, and HOLD a demanding training nudge (downgrade a hard push to a light "
            "session or a gentle check-in) — a friend eases up during a crunch. NOT a hard block: a "
            "genuine open thread, a real win, or warm stress/recovery support (eat, sleep, you've got "
            "this) still speaks. Combine with the RECOVERY block if present.")


def _proactive_context(user, session) -> str:
    parts = [build_loop_context(user, session)]

    win = _recent_win_signal(user, session)
    if win:
        parts.append(win)

    gap = _training_gap_signal(user, session)
    if gap:
        parts.append(gap)

    # Wearable recovery (flag-gated + fail-open inside; inert without fresh wearable data).
    try:
        rec = _recovery_signal(user, session)
    except Exception as e:  # noqa: BLE001
        logger.warning("RECOVERY_SIGNAL_FAILED user=%s err=%s", user.id, e)
        rec = None
    if rec:
        parts.append(rec)

    # Daily rhythm standing conditions (each flag-gated inside; a failure never kills the tick).
    for fn in (_meal_gap_signal, _morning_open_signal, _evening_close_signal):
        try:
            blk = fn(user, session)
        except Exception as e:  # noqa: BLE001
            logger.warning("RHYTHM_SIGNAL_FAILED fn=%s user=%s err=%s", fn.__name__, user.id, e)
            blk = None
        if blk:
            parts.append(blk)

    # Calendar assistant standing conditions (each flag-gated + fail-open inside; inert
    # without calendar data). Read-only on Events, weighed by the same decide().
    for fn in (_deadline_radar_signal, _free_window_signal, _high_load_signal):
        try:
            blk = fn(user, session)
        except Exception as e:  # noqa: BLE001
            logger.warning("CALENDAR_SIGNAL_FAILED fn=%s user=%s err=%s", fn.__name__, user.id, e)
            blk = None
        if blk:
            parts.append(blk)

    lvl = _checkin_level_block(user)
    if lvl:
        parts.append(lvl)

    # Adaptive targets: a due weigh-in is a standing condition (once a week, mornings).
    if config.ADAPTIVE_TARGETS_ENABLED:
        try:
            from adaptive_targets import weigh_in_condition
            wc = weigh_in_condition(user, session)
            if wc:
                parts.append(wc)
        except Exception as e:  # noqa: BLE001
            logger.warning("WEIGH_IN_CONDITION_FAILED user=%s err=%s", user.id, e)

    from engagement_tracker import _not_reaction
    last_out = (session.query(Message)
                .filter(Message.user_id == user.id, Message.direction == "out", _not_reaction())
                .order_by(Message.created_at.desc()).first())
    if last_out and last_out.created_at:
        hrs = (datetime.now(timezone.utc) - last_out.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        parts.append(f"## TIME SINCE YOUR LAST MESSAGE\n~{hrs:.1f} hours")

    # The inbound age, code-computed — the model can't date the transcript's last
    # user message, so without this it reads a history that ends on the coach's own
    # question as a live exchange and re-cites "waiting on reply" from tick history
    # for hours (the unanswered-gap deadlock, reappeared at the model layer; the
    # HEARTBEAT_ACTIVE_CONVO_MINUTES code gate only blocks the genuinely-fresh case).
    last_in = (session.query(Message)
               .filter(Message.user_id == user.id, Message.direction == "in")
               .order_by(Message.created_at.desc()).first())
    if last_in and last_in.created_at:
        hrs_in = (datetime.now(timezone.utc) - last_in.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600
        parts.append(f"## TIME SINCE THEIR LAST MESSAGE\n~{hrs_in:.1f} hours — judge "
                     "'mid-conversation' by THIS number, not by where the transcript ends.")

    thread = _open_thread_signal(user, session)
    if thread:
        parts.append(thread)

    try:
        from reminders import context_block
        rb = context_block(user, session)
        if rb:
            parts.append(rb)
    except Exception as e:  # noqa: BLE001
        logger.warning("REMINDER_CONTEXT_FAILED user=%s err=%s", user.id, e)

    day_start = _local_day_start_utc(user)
    todays_out = (session.query(Message)
                  .filter(Message.user_id == user.id, Message.direction == "out",
                          Message.created_at >= day_start, _not_reaction())
                  .order_by(Message.created_at.desc()).limit(6).all())

    ticks = (session.query(HeartbeatTick)
             .filter(HeartbeatTick.user_id == user.id)
             .order_by(HeartbeatTick.decided_at.desc())
             .limit(config.HEARTBEAT_RECENT_TICKS).all())

    # Invert the empty-history signal (bias #1): a quiet slate used to render as
    # NOTHING, and the model read that void as "no proof this isn't a duplicate →
    # stay cautious" — it wouldn't speak because it hadn't spoken. Empty history is
    # PERMISSION, not caution. Render it explicitly rather than omitting the blocks.
    if not todays_out and not ticks:
        parts.append("## PROACTIVE STATUS\n"
                     "You have sent NO proactive messages today and have no recent tick "
                     "decisions on file — you have NOT nudged about anything yet. The "
                     "absence of history is PERMISSION to speak if there's a real reason, "
                     "never a reason for caution: there is nothing here you could be "
                     "repeating.")
    else:
        if todays_out:
            parts.append("## RECENT PROACTIVE MESSAGES (today — do NOT repeat these)\n"
                         + "\n".join(f"- {m.body}" for m in reversed(todays_out)))
        if ticks:
            tl = "\n".join(
                f"- {'SPOKE' if t.spoke else 'silent'}: {t.message if t.spoke else t.reason}"
                for t in reversed(ticks))
            parts.append("## TICK HISTORY (your recent proactive decisions — don't re-send a thought)\n" + tl)

    return "\n\n".join(parts)


def decide(user_id: int) -> tuple[bool, str, dict]:
    """One decision call. Returns (spoke, payload, search): payload is the message if
    spoke, else the silence reason; search is the search DECISION for the tick record
    — {"available": offered-under-budget, "used": model-invoked-it, "query": first
    query or None}. The model calls send_text to speak or stay_silent to stay quiet
    (both outcomes are explicit tools — see SEND_TEXT_TOOL). Loads the user in its own
    session so callers can pass just an id (no detached instance)."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        context = _proactive_context(user, session)
        search = {"available": _search_available(user, session), "used": False, "query": None}
    finally:
        session.close()

    prompt = HEARTBEAT_PROMPT
    if config.HEARTBEAT_WEARABLE_AWARE_ENABLED:
        prompt = prompt + "\n\n" + _WEARABLE_GUIDANCE
    # High-load academic tone guidance — appended only when the feature is on, so the
    # prompt is byte-for-byte unchanged with it off; a no-op with no ACADEMIC LOAD block.
    if config.CALENDAR_ASSISTANT_ENABLED and config.CALENDAR_HIGH_LOAD_TONE_ENABLED:
        prompt = prompt + "\n\n" + _HIGH_LOAD_GUIDANCE
    system = [
        {"type": "text", "text": _voice_prompt(), "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": prompt + "\n\n" + context},
    ]
    tools = [SEND_TEXT_TOOL, STAY_SILENT_TOOL]
    if search["available"]:
        from agent_tools import WEB_SEARCH_TOOL
        tools.append(WEB_SEARCH_TOOL)

    messages = [{"role": "user", "content": "[heartbeat tick — decide: speak or stay silent]"}]
    for _ in range(config.AGENT_LOOP_MAX_TOOL_ITERS):
        # Dedicated ceiling, NOT MAX_RESPONSE_TOKENS: thinking + tool JSON + the
        # composed message all bill against this one budget (Aug 6 truncation).
        resp = client.messages.create(
            model=config.AGENT_LOOP_MODEL, max_tokens=config.HEARTBEAT_DECIDE_MAX_TOKENS,
            thinking={"type": "adaptive"}, output_config={"effort": "low"},
            system=system, messages=messages, tools=tools,
        )
        try:
            track(user_id, "heartbeat.decide", config.AGENT_LOOP_MODEL, resp)
        except Exception as e:
            logger.warning("HEARTBEAT_COST_TRACK_FAILED user=%s err=%s", user_id, e)

        stop = getattr(resp, "stop_reason", None)
        search["stop"] = stop  # surfaces in the HEARTBEAT_TICK log line

        # Search invocations arrive as inline server_tool_use blocks (executed
        # server-side), on any stop_reason — scan every response before branching.
        for b in resp.content:
            if (getattr(b, "type", None) in ("server_tool_use", "tool_use")
                    and getattr(b, "name", None) == "web_search"):
                search["used"] = True
                if search["query"] is None:
                    search["query"] = (getattr(b, "input", None) or {}).get("query")

        if stop == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if stop == "tool_use":
            # The two decision tools terminate the loop. send_text = speak (payload is
            # the message); stay_silent = quiet (payload is the reason).
            spoke_tool = next((b for b in resp.content
                               if getattr(b, "type", None) == "tool_use" and b.name == "send_text"), None)
            if spoke_tool is not None:
                msg = (spoke_tool.input or {}).get("message", "").strip()
                # send_text is terminal either way — an empty message can't be sent, so
                # log it as silence rather than continuing (which would leave this
                # tool_use unanswered and malform the next request).
                return (True, msg, search) if msg else (False, "send_text empty message", search)
            silent = next((b for b in resp.content
                           if getattr(b, "type", None) == "tool_use" and b.name == "stay_silent"), None)
            if silent is not None:
                return (False, (silent.input or {}).get("reason", "chose silence"), search)
            # a server tool (web_search) — feed nothing back for client tools; continue
            _decision_names = ("send_text", "stay_silent")
            messages.append({"role": "assistant", "content": resp.content})
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "content": "n/a"}
                for b in resp.content
                if getattr(b, "type", None) == "tool_use" and b.name not in _decision_names
            ] or "continue"})
            continue

        # Truncation is its OWN outcome, checked BEFORE the bare-text fallback: a
        # max_tokens stop means the decision was cut off mid-generation — usually
        # zero text (thinking ate the budget), sometimes a partial compose. Neither
        # is chosen silence, and a partial must never reach the phone (a mid-sentence
        # SMS is the "glitchy connection" class). Log it by name so it can't hide as
        # a silent tick again; degrade to a labeled silent tick, don't raise.
        if stop == "max_tokens":
            block_types = [getattr(b, "type", None) for b in resp.content]
            logger.warning(
                "HEARTBEAT_TRUNCATED user=%s stop=max_tokens blocks=%s max_tokens=%d "
                "— decide cut off mid-generation; NOT chosen silence, nothing sent",
                user_id, block_types, config.HEARTBEAT_DECIDE_MAX_TOKENS)
            return (False, "truncated:max_tokens (decide hit the output cap "
                           "mid-generation — not chosen silence)", search)

        # Fallback: the model ended with bare text instead of calling send_text (rarer
        # now that speaking is an explicit tool, but kept for robustness). Concatenate
        # EVERY text block, not just the first — with adaptive thinking + inline
        # web_search the model splits the nudge across blocks (a lead-in before the
        # search result, the substance after). Same seam as agent_loop's _join_text.
        text = _join_text(resp.content)
        if text:
            return (True, text, search)
        # A clean terminal stop with neither a decision tool nor text is an anomaly
        # — log the response shape by name (the signal whose absence let the
        # truncation bug hide) before recording the silent tick.
        logger.warning("HEARTBEAT_NO_OUTPUT user=%s stop=%s blocks=%s",
                       user_id, stop, [getattr(b, "type", None) for b in resp.content])
        return (False, "no message composed", search)
    return (False, "decision loop exhausted", search)


def heartbeat_tick(user_id: int):
    """One tick for one user: guardrails -> decision -> log (+ send if speaking).
    Typing bubble before decide() is flag-gated OFF (TYPING_INDICATOR_HEARTBEAT):
    decide may choose silence, and dots followed by nothing reads as a glitch."""
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user or not user.active:
            return
        reason = guardrail_reason(user, session)
        phone = user.phone
    finally:
        session.close()

    if reason:
        _log_tick(user_id, False, f"guardrail:{reason}")
        return

    if config.TYPING_INDICATOR_HEARTBEAT:
        from typing_indicator import typing_start
        typing_start(user_id)  # a friend texting first: dots, then the message
    spoke, payload, search = decide(user_id)
    if spoke:
        send_sms(phone, payload, user_id=user_id, message_type="heartbeat")
        _log_tick(user_id, True, "spoke", payload, search=search)
    else:
        if config.TYPING_INDICATOR_HEARTBEAT:
            from typing_indicator import typing_stop
            typing_stop(user_id)  # chose silence — never leave dots with no message
        _log_tick(user_id, False, payload, search=search)


def heartbeat_all():
    """Global tick: run a heartbeat for each active allowlisted user. Called by the
    scheduler on the (jittered) interval."""
    if not config.HEARTBEAT_ENABLED:
        return
    session = get_session()
    try:
        # Exclude waitlisted (pending) accounts — they're active=true but not
        # onboarded, so they must never be proactively coached. guardrail_reason
        # also blocks them (defense in depth); filtering here saves the per-user work.
        q = (session.query(User)
             .filter(User.active.is_(True))
             .filter((User.waitlist_status.is_(None)) | (User.waitlist_status != "pending"))
             .filter(User.onboarding_step >= 3))
        if config.HEARTBEAT_ALLOWLIST:
            q = q.filter(User.phone.in_(config.HEARTBEAT_ALLOWLIST))
        user_ids = [u.id for u in q.all()]
    finally:
        session.close()
    for uid in user_ids:
        try:
            heartbeat_tick(uid)
        except Exception as e:
            logger.error("HEARTBEAT_TICK_FAILED user=%s err=%s", uid, e, exc_info=True)
