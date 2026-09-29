"""Read-only wearable summary for the PROACTIVE engine (the heartbeat).

The google_health SYNC pipeline (integrations/google_health.py + the write side of
integrations/google_health_sync.py) owns WRITING wearable_days. This module only READS
that table, so the proactive heartbeat can become recovery-aware without touching the
sync module (and without importing its private context-builder). The reactive coach reply
keeps using integrations.google_health_sync.wearable_context; this is its proactive twin
— a compact, code-computed recovery read, not a user-facing readout.

Everything here is fail-open: with GOOGLE_HEALTH_ENABLED off, the user not connected, or
no fresh row, `recovery_read` returns None and every caller falls back to today's exact
behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

import config
from models import WearableDay

# Kept LOCAL (not imported from google_health_sync) so this reader stays decoupled from
# the sync module — it must never be edited by, or edit, that pipeline.
SOURCE = "google_health"
FRESH_MAX_AGE_DAYS = 3        # no row newer than this → treat as stale, fall back
# "worse / better than baseline" thresholds are the SHARED source of truth in config.RECOVERY_*
# so this proactive read and the reactive `## WEARABLE` block always agree. Do NOT fork the
# numbers here — read them from config at compute time (below).
# poor-night thresholds (conservative; local to the poor-night definition)
SHORT_NIGHT_MIN = 360        # < 6h absolute
POOR_VS_BASELINE = 0.80      # or < 80% of their 7-day sleep baseline


def _tz(user) -> ZoneInfo:
    try:
        return ZoneInfo(user.user_timezone or "America/Los_Angeles")
    except Exception:
        return ZoneInfo("America/Los_Angeles")


def _local_today(user, now=None) -> date:
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    return ref.astimezone(_tz(user)).date()


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def _to_local(dt_utc, tz):
    """A naive-UTC wearable timestamp → aware-local datetime (None-safe)."""
    if dt_utc is None:
        return None
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(tz)


@dataclass
class Recovery:
    sleep_minutes: int | None          # last night, minutes asleep
    sleep_baseline: float | None       # 7-day avg sleep minutes
    sleep_end_local: datetime | None   # measured wake this morning (aware local)
    sleep_start_local: datetime | None # measured bedtime (aware local)
    steps_today: int | None
    steps_baseline: float | None
    rhr_today: int | None
    rhr_baseline: float | None
    hrv_today: float | None
    hrv_baseline: float | None
    rhr_worse: bool
    hrv_worse: bool
    rhr_better: bool                   # resting HR clearly below baseline (symmetric to rhr_worse)
    hrv_better: bool                   # HRV clearly above baseline (symmetric to hrv_worse)
    sleep_better: bool                 # last night meaningfully above the 7-day sleep baseline
    fresh: bool                        # a row within FRESH_MAX_AGE_DAYS exists

    @property
    def poor_night(self) -> bool:
        if self.sleep_minutes is None:
            return False
        if self.sleep_minutes < SHORT_NIGHT_MIN:
            return True
        return bool(self.sleep_baseline) and self.sleep_minutes < self.sleep_baseline * POOR_VS_BASELINE

    @property
    def poor_recovery(self) -> bool:
        # A short/poor night, OR both cardiovascular markers worse than baseline
        # (mirrors the reactive "worse than baseline" pair — never a single number).
        return self.poor_night or (self.rhr_worse and self.hrv_worse)

    @property
    def good_recovery(self) -> bool:
        # Symmetric to poor_recovery: never "good" if any marker is WORSE than baseline,
        # and require a real POSITIVE signal (not merely "not worse") — a night meaningfully
        # above the sleep baseline, OR resting HR / HRV clearly better than baseline. Uses
        # the SAME shared thresholds as the reactive block (computed in recovery_read).
        if self.rhr_worse or self.hrv_worse:
            return False
        return bool(self.sleep_better or self.rhr_better or self.hrv_better)


def recovery_read(user, session, *, now=None) -> Recovery | None:
    """Compact recovery read from wearable_days for the heartbeat, or None when there is
    nothing trustworthy to act on (flag/connection/stale all fail-open to None).

    Read-only: a single SELECT over WearableDay for this user's last 7 local days. Never
    writes, never calls the sync module."""
    if not config.GOOGLE_HEALTH_ENABLED:
        return None
    try:
        # connection status: only a connected/error account has meaningful history
        # (mirrors the reactive gate without importing the sync module).
        from integrations.base import get_integration
        integ = get_integration(session, user.id, SOURCE)
        if integ is None or integ.status not in ("connected", "error"):
            return None

        tz = _tz(user)
        today = _local_today(user, now=now)
        since = (today - timedelta(days=7)).isoformat()
        rows = (session.query(WearableDay)
                .filter(WearableDay.user_id == user.id, WearableDay.provider == SOURCE,
                        WearableDay.day >= since)
                .order_by(WearableDay.day.asc()).all())
        if not rows:
            return None
        fresh_cutoff = (today - timedelta(days=FRESH_MAX_AGE_DAYS)).isoformat()
        fresh = any(r.day >= fresh_cutoff for r in rows)
        if not fresh:
            return Recovery(None, None, None, None, None, None, None, None, None, None,
                            False, False, False, False, False, False)

        by_day = {r.day: r for r in rows}
        today_s = today.isoformat()
        t = by_day.get(today_s)
        # last night = the sleep that ENDED today; else the most recent night
        night = t if (t and t.sleep_minutes) else by_day.get((today - timedelta(days=1)).isoformat())

        sleep_minutes = night.sleep_minutes if night else None
        sleep_baseline = _avg([r.sleep_minutes for r in rows if r.sleep_minutes])
        sleep_end_local = _to_local(night.sleep_end, tz) if night else None
        sleep_start_local = _to_local(night.sleep_start, tz) if night else None

        steps_today = t.steps if t else None
        steps_baseline = _avg([r.steps for r in rows if r.steps and r.day != today_s])

        rhr_today = (t.resting_hr if t else None) or (night.resting_hr if night else None)
        rhr_baseline = _avg([r.resting_hr for r in rows if r.resting_hr])
        hrv_today = (t.hrv_rmssd if t else None) or (night.hrv_rmssd if night else None)
        hrv_baseline = _avg([r.hrv_rmssd for r in rows if r.hrv_rmssd])

        # Shared thresholds (config.RECOVERY_*) — same numbers the reactive block uses.
        rhr_worse = bool(rhr_today and rhr_baseline and
                         rhr_today >= rhr_baseline + config.RECOVERY_RHR_WORSE_DELTA)
        hrv_worse = bool(hrv_today and hrv_baseline and
                         hrv_today <= hrv_baseline * config.RECOVERY_HRV_WORSE_RATIO)
        # Symmetric "better than baseline" side.
        rhr_better = bool(rhr_today and rhr_baseline and
                          rhr_today <= rhr_baseline - config.RECOVERY_RHR_BETTER_DELTA)
        hrv_better = bool(hrv_today and hrv_baseline and
                          hrv_today >= hrv_baseline * config.RECOVERY_HRV_BETTER_RATIO)
        sleep_better = bool(sleep_minutes and sleep_baseline and
                            sleep_minutes >= config.RECOVERY_GOOD_NIGHT_MIN and
                            sleep_minutes >= sleep_baseline * config.RECOVERY_SLEEP_BETTER_RATIO)

        return Recovery(
            sleep_minutes=sleep_minutes, sleep_baseline=sleep_baseline,
            sleep_end_local=sleep_end_local, sleep_start_local=sleep_start_local,
            steps_today=steps_today, steps_baseline=steps_baseline,
            rhr_today=rhr_today, rhr_baseline=rhr_baseline,
            hrv_today=hrv_today, hrv_baseline=hrv_baseline,
            rhr_worse=rhr_worse, hrv_worse=hrv_worse,
            rhr_better=rhr_better, hrv_better=hrv_better, sleep_better=sleep_better,
            fresh=True)
    except Exception:
        # Fail-open: a reader error must never crash a heartbeat tick.
        return None


def _hm(minutes) -> str:
    if not minutes:
        return "?"
    return f"{int(minutes) // 60}h{int(minutes) % 60:02d}m"


# Trailing-window + minimum-days for the TDEE step average (kept LOCAL, like the
# recovery thresholds above — this reader stays decoupled from the sync module).
STEP_AVG_WINDOW_DAYS = 14   # trailing local days considered
STEP_AVG_MIN_DAYS = 3       # need at least this many days WITH steps to be trustworthy


def recent_step_avg(user, session, *, now=None) -> float | None:
    """Trailing average of REAL daily steps from wearable_days, for the TDEE
    activity multiplier — or None when there is nothing trustworthy to use (so
    the caller falls back to the static onboarding avg_steps).

    Read-only: a single SELECT over WearableDay for this user's last
    STEP_AVG_WINDOW_DAYS local days. Never writes, never calls the sync module.
    Fail-open: flag off / not connected / too few days → None."""
    if not config.GOOGLE_HEALTH_ENABLED:
        return None
    try:
        from integrations.base import get_integration
        integ = get_integration(session, user.id, SOURCE)
        if integ is None or integ.status not in ("connected", "error"):
            return None

        today = _local_today(user, now=now)
        since = (today - timedelta(days=STEP_AVG_WINDOW_DAYS)).isoformat()
        rows = (session.query(WearableDay)
                .filter(WearableDay.user_id == user.id, WearableDay.provider == SOURCE,
                        WearableDay.day >= since)
                .all())
        # Only count days that actually recorded steps (a 0/None day is usually a
        # non-wear gap, not a genuinely sedentary day — averaging it in would drag
        # the multiplier down artificially).
        steps = [r.steps for r in rows if r.steps]
        if len(steps) < STEP_AVG_MIN_DAYS:
            return None
        return sum(steps) / len(steps)
    except Exception:
        # Fail-open: a reader error must never change (or crash) target computation.
        return None


# ─── Measured sleep/wake window (typical bed + wake hour) ─────────────────────
# Derived from RECENT wearable sleep_start/sleep_end so the coach can prefer the user's
# ACTUAL rhythm over the static onboarding sleep_time/wake_time (irregular sleepers drift
# a lot from what they typed months ago). Kept LOCAL to this reader, like the constants
# above — never touches the sync pipeline.
MEASURED_WINDOW_DAYS = 10   # trailing local days considered for the window
MEASURED_MIN_NIGHTS = 3     # need at least this many nights WITH real sleep to trust it


def _median(vals):
    """Plain median of a non-empty numeric list (float for an even count)."""
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


@dataclass
class SleepWindow:
    bed_hm: tuple[int, int]            # typical bedtime (local hour, minute) — MEDIAN
    wake_hm: tuple[int, int]           # typical wake (local hour, minute) — MEDIAN
    last_wake_local: datetime | None   # most recent measured wake (aware local)
    nights: int                        # nights with real sleep that informed the median


def measured_sleep_window(user, session, *, now=None) -> SleepWindow | None:
    """The user's TYPICAL bed hour + wake hour from RECENT wearable sleep, or None when
    there aren't enough fresh nights to trust a measured rhythm (caller then falls back to
    the static profile times — EXACTLY today's behaviour).

    Robust central value = MEDIAN over the last MEASURED_WINDOW_DAYS local days, ignoring
    nights with no sleep_start/sleep_end. Bedtimes are placed on a continuous evening→dawn
    axis (an after-midnight bedtime like 01:00 is shifted past the evening cluster) so the
    median of a 23:30/00:30 pair doesn't collapse to noon. Requires >= MEASURED_MIN_NIGHTS
    real nights.

    Read-only: one SELECT over WearableDay. Never writes, never calls the sync module.
    Fail-open: flag off / not connected / too few nights / any error → None."""
    if not config.GOOGLE_HEALTH_ENABLED:
        return None
    try:
        from integrations.base import get_integration
        integ = get_integration(session, user.id, SOURCE)
        if integ is None or integ.status not in ("connected", "error"):
            return None

        tz = _tz(user)
        today = _local_today(user, now=now)
        since = (today - timedelta(days=MEASURED_WINDOW_DAYS)).isoformat()
        rows = (session.query(WearableDay)
                .filter(WearableDay.user_id == user.id, WearableDay.provider == SOURCE,
                        WearableDay.day >= since)
                .order_by(WearableDay.day.asc()).all())

        beds: list[int] = []   # minutes-of-day, after-midnight shifted by +1440
        wakes: list[int] = []  # minutes-of-day (wake is morning-ish; no wrap needed)
        last_wake: datetime | None = None
        for r in rows:
            bs = _to_local(r.sleep_start, tz)
            we = _to_local(r.sleep_end, tz)
            if bs is None or we is None:
                continue
            bm = bs.hour * 60 + bs.minute
            if bs.hour < 12:          # an after-midnight bedtime sits AFTER the evening cluster
                bm += 1440
            beds.append(bm)
            wakes.append(we.hour * 60 + we.minute)
            if last_wake is None or we > last_wake:
                last_wake = we

        if len(beds) < MEASURED_MIN_NIGHTS:
            return None

        bed_med = int(round(_median(beds))) % 1440
        wake_med = int(round(_median(wakes))) % 1440
        return SleepWindow(
            bed_hm=(bed_med // 60, bed_med % 60),
            wake_hm=(wake_med // 60, wake_med % 60),
            last_wake_local=last_wake,
            nights=len(beds),
        )
    except Exception:
        # Fail-open: a reader error must never crash a caller's sleep/quiet logic.
        return None


# ─── Recent-activity awareness (today's movement) ─────────────────────────────
@dataclass
class Activity:
    steps: int | None
    active_minutes: int | None
    notably_active: bool     # steps or active minutes past a "clearly moving" threshold


def recent_activity(user, session, *, now=None) -> Activity | None:
    """Today's measured movement (steps + active minutes) with a coarse "notably active"
    flag, or None when there is nothing trustworthy to use.

    Read-only: one SELECT for today's WearableDay row. Never writes, never calls the sync
    module. Fail-open: flag off / not connected / no today row / no data / error → None."""
    if not config.GOOGLE_HEALTH_ENABLED:
        return None
    try:
        from integrations.base import get_integration
        integ = get_integration(session, user.id, SOURCE)
        if integ is None or integ.status not in ("connected", "error"):
            return None

        today = _local_today(user, now=now).isoformat()
        row = (session.query(WearableDay)
               .filter(WearableDay.user_id == user.id, WearableDay.provider == SOURCE,
                       WearableDay.day == today)
               .one_or_none())
        if row is None:
            return None
        steps = row.steps
        active = row.active_minutes
        if not steps and not active:
            return None
        notably = bool((steps and steps >= config.WEARABLE_ACTIVE_STEPS_THRESHOLD) or
                       (active and active >= config.WEARABLE_ACTIVE_MINUTES_THRESHOLD))
        return Activity(steps=steps, active_minutes=active, notably_active=notably)
    except Exception:
        return None


def activity_context(user, session, *, now=None) -> str | None:
    """A compact advisory ACTIVITY TODAY block for build_loop_context (and, since the
    heartbeat wraps that builder, its ticks too) — or None when inert. Flag-gated by
    WEARABLE_ACTIVITY_CONTEXT_ENABLED; fail-open to None otherwise."""
    if not config.WEARABLE_ACTIVITY_CONTEXT_ENABLED:
        return None
    try:
        act = recent_activity(user, session, now=now)
    except Exception:
        return None
    if act is None:
        return None
    bits: list[str] = []
    if act.steps:
        bits.append(f"~{act.steps:,} steps")
    if act.active_minutes:
        bits.append(f"{act.active_minutes} active min")
    if not bits:
        return None
    if act.notably_active:
        tail = (" — they've been moving today (e.g. walked to class). Acknowledge the "
                "movement; do NOT imply they've been sedentary or hard-push more exercise.")
    else:
        tail = (" so far today. Advisory context only — factor it in, don't lecture and "
                "don't assume they've been sedentary.")
    return "## ACTIVITY TODAY\n" + ", ".join(bits) + tail


__all__ = ["recovery_read", "recent_step_avg", "measured_sleep_window", "recent_activity",
           "activity_context", "Recovery", "SleepWindow", "Activity", "SOURCE"]
