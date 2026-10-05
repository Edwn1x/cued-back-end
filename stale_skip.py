"""
Stale-tick skip — a code rule that runs AFTER the guardrails and BEFORE the Opus
decision call, answering one question: "has anything the coach weighs changed since
its last real decision?" If nothing has, the last decision (silence) still stands and
the tick is skipped without a model call.

Why (2026-10-05, prod 14-day read): 90% of the ticks that reached the model had no
new data since the previous tick, and the dominant silence reason was "already sent X
today, nothing new" — a dedup judgment re-derived ~25×/day/user at full-context Opus
cost (~$3.76 per spoken text). But a NAIVE "no new rows → skip" would have silenced
28 of 31 proactive texts: most speaks happen on the first eligible tick of the day
with nothing new in the DB (the morning open, a standing training gap), and six were
purely TIME-driven (wake time passed, a planned 6pm session arrived, dinner window
opened, a conversation cooldown expired). So the rule is narrower than "nothing new":

  skip only when ALL of
    1. nothing landed since the anchor (message, meal, workout, event, wearable sync,
       weigh-in, target recompute, receipt/location signal, phone activity);
    2. the anchor — the last tick that actually ran the model — chose silence, and
       every tick since was itself a stale skip (a code gate or a spoken tick in
       between resets the chain: the first tick after quiet hours ALWAYS runs);
    3. the state fingerprint is byte-identical to the anchor's: the proactive context
       the model would see, minus the blocks that change every tick by construction
       (the clock, the tick ledger), with hour/minute-granular durations and clock
       times collapsed, PLUS coarse time bands (local date, daypart, hours-since-
       their-last-message band, hours-since-my-last-message band) so that time
       passing still re-opens the question at the resolution the model acts on;
    4. the anchor is younger than HEARTBEAT_STALE_MAX_HOURS (the floor: a trigger the
       fingerprint misses costs at most a delay, never a lost text);
    5. the anchor didn't set a recheck time that has now arrived (stay_silent's
       optional recheck_in_minutes — the model declaring when its silence expires,
       e.g. "session at 6, recheck then").

Two flags: HEARTBEAT_STALE_SKIP_SHADOW computes + records the verdict on every tick
but still calls the model (the would-skip/spoke pairs are the false-negative audit:
HEARTBEAT_STALE_SHADOW_MISS); HEARTBEAT_STALE_SKIP_ENABLED actually skips. Ships
shadow-on / skip-off; flip after a week of clean shadow data.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config
from models import (HeartbeatTick, Message, Meal, Workout, WorkoutSession, Event, WearableDay,
                    WeightLog, TargetAdjustment, Signal, AgentTask)

logger = logging.getLogger("cued.heartbeat.stale")

SKIP_REASON_PREFIX = "skipped:stale"

# Context blocks that change on EVERY tick by construction. Their meaning is carried
# by the coarse bands appended in fingerprint() instead.
_VOLATILE_HEADER_RE = re.compile(
    r"^(NOW|TIME SINCE YOUR LAST MESSAGE|TIME SINCE THEIR LAST MESSAGE|TICK HISTORY|PROACTIVE STATUS)\b",
    re.I)
# Hour/minute-granular durations ("~2.5h ago", "in 37h", "~45 min after") and clock
# times ("It's 11:28", "12:45–15:00"). Day-granular counts ("5 days since", "in 3d")
# are kept on purpose — those change once a day and SHOULD re-open the question.
_DURATION_RE = re.compile(
    r"~?\d+(?:\.\d+)?\s*(?:h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\b", re.I)
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:am|pm)?\b", re.I)

# decide() outcomes that are neither chosen silence nor a send — never an anchor.
_ANOMALY_REASONS = {"send_text empty message", "no message composed", "decision loop exhausted"}

_SINCE_BANDS = (1, 3, 6, 12, 24, 72)


@dataclass
class Verdict:
    fingerprint: str
    would_skip: bool
    detail: str


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _now(now=None) -> datetime:
    return _naive(now) if now is not None else datetime.now(timezone.utc).replace(tzinfo=None)


# ---- fingerprint ------------------------------------------------------------

def normalize(context: str) -> str:
    """The proactive context with the per-tick-volatile blocks removed and fine-grained
    time tokens collapsed. Everything that stays is state the model weighs: which
    standing-condition blocks are present and what they say, today's outbound, memory,
    totals, events, reminders."""
    chunks = re.split(r"(?m)^(?=## )", context)
    kept = []
    for c in chunks:
        if c.startswith("## "):
            header = c[3:].split("\n", 1)[0].strip()
            if _VOLATILE_HEADER_RE.match(header):
                continue
        kept.append(c)
    text = "".join(kept)
    text = _DURATION_RE.sub("<dur>", text)
    text = _CLOCK_RE.sub("<clock>", text)
    return text


def _band_hours(hours: float | None) -> str:
    if hours is None:
        return "none"
    for b in _SINCE_BANDS:
        if hours < b:
            return f"<{b}h"
    return f">={_SINCE_BANDS[-1]}h"


def _daypart(local_hour: int) -> str:
    if 5 <= local_hour < 10:
        return "morning"
    if 10 <= local_hour < 14:
        return "midday"
    if 14 <= local_hour < 17:
        return "afternoon"
    if 17 <= local_hour < 21:
        return "evening"
    return "night"


def bands(user, session, *, now=None) -> str:
    """The coarse time bands folded into the fingerprint: local date + daypart (so a
    new daypart re-opens the question at most ~5×/day), and the hours-since-their-
    last-message / hours-since-my-last-message bands (a conversation cooldown or an
    anti-stack window expiring is a real change the model acts on)."""
    from engagement_tracker import _not_reaction
    now_n = _now(now)
    try:
        tz = ZoneInfo(user.user_timezone or "America/Los_Angeles")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("America/Los_Angeles")
    local = now_n.replace(tzinfo=timezone.utc).astimezone(tz)

    def _hours_since(q):
        row = q.order_by(Message.created_at.desc()).first()
        if not row or not row.created_at:
            return None
        return (now_n - _naive(row.created_at)).total_seconds() / 3600

    since_in = _hours_since(session.query(Message).filter(
        Message.user_id == user.id, Message.direction == "in"))
    since_out = _hours_since(session.query(Message).filter(
        Message.user_id == user.id, Message.direction == "out", _not_reaction()))
    return (f"@date={local:%Y-%m-%d} @daypart={_daypart(local.hour)} "
            f"@since_in={_band_hours(since_in)} @since_out={_band_hours(since_out)}")


def fingerprint(user, session, context: str, *, now=None) -> str:
    material = normalize(context) + "\n" + bands(user, session, now=now)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


# ---- what landed since the anchor -------------------------------------------

def new_rows_since(user, session, since: datetime, now: datetime) -> list[str]:
    """Labels of every data source with a row in (since, now] for this user. One
    EXISTS query per source; extend the list when a new proactive input lands."""
    checks = (
        ("message", Message, Message.created_at),
        ("meal", Meal, Meal.logged_at),
        ("workout", Workout, Workout.date),
        ("session_started", WorkoutSession, WorkoutSession.started_at),
        ("session_finished", WorkoutSession, WorkoutSession.finished_at),
        ("event_added", Event, Event.created_at),
        ("event_started", Event, Event.occurred_at),
        ("event_ended", Event, Event.ends_at),
        ("wearable", WearableDay, WearableDay.synced_at),
        ("weigh_in", WeightLog, WeightLog.weighed_at),
        ("target_adjustment", TargetAdjustment, TargetAdjustment.at),
        ("signal", Signal, Signal.ts),
        # deferred tasks (agent_tasks.py): a new promise or a finished one (its outbound
        # is a Message too; this catches a failed/cancelled finish that sent nothing)
        ("task_created", AgentTask, AgentTask.created_at),
        ("task_finished", AgentTask, AgentTask.finished_at),
    )
    found = []
    for label, model, col in checks:
        hit = (session.query(model.id)
               .filter(model.user_id == user.id, col > since, col <= now)
               .first())
        if hit:
            found.append(label)
    la = getattr(user, "last_active_at", None)
    if la and since < _naive(la) <= now:
        found.append("phone_activity")
    return found


# ---- anchor + verdict -------------------------------------------------------

def is_chained_skip(t: HeartbeatTick) -> bool:
    """A tick that was (or, in shadow mode, would have been) skipped as stale — it is
    NOT a decision, so the anchor is whatever came before it."""
    return (not t.spoke) and ((t.reason or "").startswith(SKIP_REASON_PREFIX) or bool(t.stale_would_skip))


def is_model_silent(t: HeartbeatTick) -> bool:
    r = t.reason or ""
    return ((not t.spoke) and bool(r)
            and not r.startswith(("guardrail:", "truncated:", SKIP_REASON_PREFIX))
            and r not in _ANOMALY_REASONS)


def find_anchor(user, session) -> tuple[HeartbeatTick | None, int]:
    """The most recent tick that was a real decision (walking back over stale skips),
    and how many skips were chained onto it."""
    ticks = (session.query(HeartbeatTick)
             .filter(HeartbeatTick.user_id == user.id)
             .order_by(HeartbeatTick.decided_at.desc(), HeartbeatTick.id.desc())
             .limit(60).all())
    chained = 0
    for t in ticks:
        if is_chained_skip(t):
            chained += 1
            continue
        return t, chained
    return None, chained


def evaluate(user, session, context: str, *, now=None) -> Verdict:
    """The verdict for this tick. Pure read; the caller stores the fingerprint on the
    tick row either way so the NEXT tick can compare against it."""
    now_n = _now(now)
    fp = fingerprint(user, session, context, now=now_n)
    anchor, chained = find_anchor(user, session)
    if anchor is None:
        return Verdict(fp, False, "no prior tick")
    if not is_model_silent(anchor):
        kind = "spoke" if anchor.spoke else (anchor.reason or "")[:40]
        return Verdict(fp, False, f"anchor #{anchor.id} not model-silent ({kind})")
    if anchor.fingerprint != fp:
        return Verdict(fp, False, f"state changed since anchor #{anchor.id}")
    age = now_n - _naive(anchor.decided_at)
    if age > timedelta(hours=config.HEARTBEAT_STALE_MAX_HOURS):
        return Verdict(fp, False, f"floor: {age.total_seconds() / 3600:.1f}h since anchor #{anchor.id}")
    if anchor.recheck_at and now_n >= _naive(anchor.recheck_at):
        return Verdict(fp, False, f"recheck due (anchor #{anchor.id} asked for {anchor.recheck_at:%H:%M}Z)")
    new = new_rows_since(user, session, _naive(anchor.decided_at), now_n)
    if new:
        return Verdict(fp, False, f"new since anchor #{anchor.id}: {','.join(new)}")
    return Verdict(fp, True, f"anchor #{anchor.id} silent {age.total_seconds() / 60:.0f}m ago, "
                             f"{chained} chained, nothing new, same state")
