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


__all__ = ["recovery_read", "recent_step_avg", "Recovery", "SOURCE"]
