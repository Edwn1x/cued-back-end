"""Fitbit sync (Part 2a) — daily summaries → wearable_days, scale readings → weight_logs,
and the `## WEARABLE (fitbit)` context block the coach + heartbeat read.

Pure API + DB, no model calls. Window = the last FITBIT_SYNC_DAYS local days (today +
yesterday) on the 30-min poll; FITBIT_BACKFILL_DAYS on the first pull after connect.
Per sync: 4 range calls (steps, resting HR, sleep, HRV) + today's activity summary +
one weight-log call per day in the window — ≤7 calls steady-state against Fitbit's
150/hour/user limit. Push notifications (routes: /oauth/fitbit/subscriber) call
sync_user for the owner so data is fresh minutes after the watch syncs, not 30 later.

Sleep is keyed by dateOfSleep (the MORNING it ends) and only the main sleep counts —
a nap doesn't overwrite last night. Fitbit's start/endTime are wall-clock local with
no zone; we attach the user's timezone and store naive UTC like everything else.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import config
from models import get_session, User, Integration, WearableDay, WeightLog
from integrations import base, fitbit

logger = logging.getLogger("cued.integrations.fitbit_sync")

SOURCE = "fitbit"
CONTEXT_MAX_AGE_DAYS = 3        # no row newer than this → block says "no recent data"
WEIGHT_NOTE_PREFIX = "fitbit:"  # weight_logs.notes = 'fitbit:<logId>' — the idempotency key


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tz(user) -> ZoneInfo:
    return ZoneInfo((getattr(user, "user_timezone", None)) or "America/Los_Angeles")


def _local_today(tz) -> date:
    return datetime.now(tz).date()


def _window(tz, days: int) -> list[str]:
    """Oldest → newest local day strings, `days` long, ending today."""
    today = _local_today(tz)
    days = max(1, int(days or 1))
    return [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]


def _local_to_utc(s: str | None, tz) -> datetime | None:
    """Fitbit '2026-09-23T23:48:00.000' (wall clock, no zone) → naive UTC."""
    if not s:
        return None
    try:
        local = datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=tz)
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


# ─── the pull ────────────────────────────────────────────────────────────────

def _upsert_day(session, user_id: int, day: str, **fields) -> None:
    row = (session.query(WearableDay)
           .filter(WearableDay.user_id == user_id, WearableDay.provider == SOURCE,
                   WearableDay.day == day).one_or_none())
    if row is None:
        row = WearableDay(user_id=user_id, provider=SOURCE, day=day)
        session.add(row)
    for k, v in fields.items():
        if v is not None:
            setattr(row, k, v)
    row.synced_at = _utcnow()


def _record_weight(session, user, entry: dict, tz) -> bool:
    """One Fitbit weight log → weight_logs row (idempotent on logId) + users.weight_lbs
    latest-wins with the same protein follow rule as log_weight. Returns True if new."""
    log_id = entry.get("logId")
    try:
        lbs = round(float(entry.get("weight")), 1)
    except (TypeError, ValueError):
        return False
    if log_id is None or not (60 <= lbs <= 600):
        return False
    note = f"{WEIGHT_NOTE_PREFIX}{log_id}"
    exists = (session.query(WeightLog.id)
              .filter(WeightLog.user_id == user.id, WeightLog.notes == note).first())
    if exists:
        return False
    when = _local_to_utc(f"{entry.get('date')}T{entry.get('time') or '09:00:00'}", tz) or _utcnow()
    row = WeightLog(user_id=user.id, weighed_at=when, weight_lbs=lbs, notes=note)
    session.add(row)
    session.flush()
    latest = (session.query(WeightLog.weighed_at).filter(WeightLog.user_id == user.id, WeightLog.id != row.id)
              .order_by(WeightLog.weighed_at.desc()).first())
    if latest is None or when >= latest[0]:
        user.weight_lbs = lbs
        if getattr(user, "targets_source", None) != "user" and user.protein_target:
            try:
                from macro_calculator import calculate_targets
                new_p = calculate_targets(user)["protein"]
                if new_p != user.protein_target:
                    user.protein_target = new_p
                if getattr(user, "protein_target_computed", None) != new_p:
                    user.protein_target_computed = new_p
            except Exception as e:  # noqa: BLE001 — a target hiccup never blocks the sync
                logger.warning("FITBIT_WEIGHT_PROTEIN_FOLLOW_FAILED user=%s err=%s", user.id, e)
    return True


def sync_user(user_id: int, *, days: int | None = None) -> dict:
    """Pull one connected user's window into wearable_days (+ weight_logs). Returns a
    summary dict; never raises for API trouble (recorded via note_sync_failure)."""
    if not config.FITBIT_ENABLED:
        return {"skipped": "flag off"}
    token = base.get_valid_access_token(user_id, SOURCE)
    if not token:
        return {"skipped": "not connected"}

    session = get_session()
    try:
        user = session.get(User, user_id)
        if user is None:
            return {"skipped": "no user"}
        tz = _tz(user)
        integ = base.get_integration(session, user_id, SOURCE)
        first = not bool((integ.meta or {}).get("first_sync_done")) if integ else True
    finally:
        session.close()

    if days is None:
        days = config.FITBIT_BACKFILL_DAYS if first else config.FITBIT_SYNC_DAYS
    window = _window(tz, days)
    start, end = window[0], window[-1]

    try:
        steps = fitbit.get_steps_series(token, start, end)
        rhr = fitbit.get_resting_hr_series(token, start, end)
        sleep_logs = fitbit.get_sleep_range(token, start, end)
        hrv = fitbit.get_hrv_series(token, start, end)
        today_summary = fitbit.get_activity_summary(token, end)
        weights = {d: fitbit.get_weight_logs(token, d) for d in window}
    except fitbit.FitbitAPIError as e:
        if e.status == 401:
            # the grant is gone (user revoked in the Fitbit app, or a lost rotated refresh
            # token) — say so once via the status line rather than retrying forever
            base.mark_revoked(user_id, SOURCE)
            return {"error": "revoked"}
        base.note_sync_failure(user_id, SOURCE, e)
        return {"error": str(e)[:120]}
    except Exception as e:  # noqa: BLE001 — network etc.
        base.note_sync_failure(user_id, SOURCE, e)
        return {"error": str(e)[:120]}

    # main sleep per dateOfSleep (a nap must not overwrite the night)
    main_sleep: dict[str, dict] = {}
    for log in sleep_logs:
        d = log.get("dateOfSleep")
        if not d:
            continue
        cur = main_sleep.get(d)
        if cur is None or (log.get("isMainSleep") and not cur.get("isMainSleep")):
            main_sleep[d] = log

    days_written = new_weights = 0
    session = get_session()
    try:
        user = session.get(User, user_id)
        for d in window:
            fields: dict = {}
            if d in steps:
                fields["steps"] = steps[d]
            if d in rhr:
                fields["resting_hr"] = rhr[d]
            if d in hrv:
                fields["hrv_rmssd"] = hrv[d]
            sl = main_sleep.get(d)
            if sl:
                fields["sleep_minutes"] = int(sl.get("minutesAsleep") or 0) or None
                fields["sleep_start"] = _local_to_utc(sl.get("startTime"), tz)
                fields["sleep_end"] = _local_to_utc(sl.get("endTime"), tz)
                eff = sl.get("efficiency")
                fields["sleep_efficiency"] = int(eff) if isinstance(eff, (int, float)) and eff > 0 else None
            if d == end and today_summary:
                try:
                    fields["calories_out"] = int(today_summary.get("caloriesOut") or 0) or None
                    fields["active_minutes"] = (int(today_summary.get("veryActiveMinutes") or 0)
                                                + int(today_summary.get("fairlyActiveMinutes") or 0))
                    if "steps" not in fields and today_summary.get("steps") is not None:
                        fields["steps"] = int(today_summary.get("steps") or 0)
                    if "resting_hr" not in fields and today_summary.get("restingHeartRate"):
                        fields["resting_hr"] = int(today_summary["restingHeartRate"])
                except (TypeError, ValueError):
                    pass
            if fields:
                _upsert_day(session, user_id, d, **fields)
                days_written += 1
            for entry in weights.get(d) or []:
                if _record_weight(session, user, entry, tz):
                    new_weights += 1
        session.commit()
    finally:
        session.close()

    base.note_sync_success(user_id, SOURCE, first_sync_done=True, last_window=[start, end])
    logger.info("FITBIT_SYNC user=%s window=%s..%s days=%s new_weights=%s", user_id, start, end,
                days_written, new_weights)
    return {"days": days_written, "new_weights": new_weights, "window": [start, end]}


def sync_all() -> int:
    """Scheduler entry (every 30 min): every connected fitbit user. `error` rows keep
    being polled — they heal on the next good pull (base.note_sync_success)."""
    if not config.FITBIT_ENABLED:
        return 0
    session = get_session()
    try:
        ids = [i.user_id for i in session.query(Integration)
               .filter(Integration.provider == SOURCE, Integration.status.in_(("connected", "error"))).all()]
    finally:
        session.close()
    n = 0
    for uid in ids:
        try:
            sync_user(uid)
            n += 1
        except Exception:
            logger.exception("FITBIT_SYNC_USER_FAILED user=%s", uid)
    return n


# ─── push notifications (routes → here) ──────────────────────────────────────

def users_for_owner_ids(owner_ids) -> list[int]:
    ids = [str(o) for o in owner_ids if o]
    if not ids:
        return []
    session = get_session()
    try:
        rows = (session.query(Integration.user_id)
                .filter(Integration.provider == SOURCE, Integration.external_id.in_(ids),
                        Integration.status.in_(("connected", "error"))).all())
        return sorted({r[0] for r in rows})
    finally:
        session.close()


def handle_notifications(payload, *, run_async: bool = True) -> list[int]:
    """Fitbit POSTed a list of {collectionType, date, ownerId, ...}. Map owners → users
    and sync each once, off-thread (the endpoint must 204 within 5s). Returns the
    user ids kicked; unknown owners are ignored."""
    if not isinstance(payload, list):
        return []
    owners = {str(n.get("ownerId")) for n in payload if isinstance(n, dict) and n.get("ownerId")}
    user_ids = users_for_owner_ids(owners)
    if not user_ids:
        return []
    logger.info("FITBIT_NOTIFY owners=%s users=%s", len(owners), user_ids)

    def _run():
        for uid in user_ids:
            try:
                sync_user(uid)
            except Exception:
                logger.exception("FITBIT_NOTIFY_SYNC_FAILED user=%s", uid)

    if run_async:
        threading.Thread(target=_run, name="fitbit-notify-sync", daemon=True).start()
    else:
        _run()
    return user_ids


# ─── the context block ───────────────────────────────────────────────────────

def _hm(minutes: int | None) -> str:
    if not minutes:
        return "?"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _clock(dt_utc: datetime | None, tz) -> str:
    if not dt_utc:
        return "?"
    local = dt_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    return local.strftime("%-I:%M%p").lower()


def _avg(vals) -> float | None:
    vals = [v for v in vals if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def wearable_context(user, session) -> str:
    """`## WEARABLE (fitbit)` — last night, today's steps, HR/HRV vs a 7-day baseline.
    Empty string when the user isn't connected or has no row in the last 3 days
    (a connected-but-stale account gets a one-line 'no recent data' instead)."""
    if not config.FITBIT_ENABLED:
        return ""
    integ = base.get_integration(session, user.id, SOURCE)
    if integ is None or integ.status not in ("connected", "error"):
        return ""
    tz = _tz(user)
    today = _local_today(tz)
    since = (today - timedelta(days=7)).isoformat()
    rows = (session.query(WearableDay)
            .filter(WearableDay.user_id == user.id, WearableDay.provider == SOURCE,
                    WearableDay.day >= since)
            .order_by(WearableDay.day.asc()).all())
    by_day = {r.day: r for r in rows}
    fresh_cutoff = (today - timedelta(days=CONTEXT_MAX_AGE_DAYS)).isoformat()
    if not any(r.day >= fresh_cutoff for r in rows):
        return ("## WEARABLE (fitbit)\nconnected but nothing synced in the last few days — their "
                "watch may not have synced to the Fitbit app. Don't mention it unless they ask.")

    lines = []
    today_s = today.isoformat()
    t = by_day.get(today_s)
    # last night = the sleep that ENDED today (dateOfSleep = today); fall back to yesterday's
    night = t if (t and t.sleep_minutes) else by_day.get((today - timedelta(days=1)).isoformat())
    night_label = "last night" if (t and t.sleep_minutes) else "most recent night"
    sleep_avg = _avg([r.sleep_minutes for r in rows if r.sleep_minutes])
    if night and night.sleep_minutes:
        s = f"{night_label}: {_hm(night.sleep_minutes)} ({_clock(night.sleep_start, tz)}–{_clock(night.sleep_end, tz)})"
        if sleep_avg:
            s += f", 7-day avg {_hm(int(sleep_avg))}"
        lines.append(s)
    steps_avg = _avg([r.steps for r in rows if r.steps and r.day != today_s])
    if t and t.steps is not None:
        s = f"steps today: {t.steps:,} so far"
        if steps_avg:
            s += f" · 7-day avg {int(steps_avg):,}"
        lines.append(s)
    elif steps_avg:
        lines.append(f"steps: 7-day avg {int(steps_avg):,} (nothing synced yet today)")
    rhr_today = (t.resting_hr if t else None) or (night.resting_hr if night else None)
    rhr_avg = _avg([r.resting_hr for r in rows if r.resting_hr])
    hrv_today = (t.hrv_rmssd if t else None) or (night.hrv_rmssd if night else None)
    hrv_avg = _avg([r.hrv_rmssd for r in rows if r.hrv_rmssd])
    hr_bits, worse = [], []
    if rhr_today:
        hr_bits.append(f"resting HR: {rhr_today}" + (f" (7-day avg {int(round(rhr_avg))})" if rhr_avg else ""))
        if rhr_avg and rhr_today >= rhr_avg + 4:
            worse.append("HR")
    if hrv_today:
        hr_bits.append(f"HRV {hrv_today:.0f}ms" + (f" (7-day avg {hrv_avg:.0f}ms)" if hrv_avg else ""))
        if hrv_avg and hrv_today <= hrv_avg * 0.85:
            worse.append("HRV")
    if hr_bits:
        s = " · ".join(hr_bits)
        if worse:
            s += " — " + (" and ".join(worse)) + " worse than their baseline"
        lines.append(s)
    if not lines:
        return ""
    synced = integ.updated_at
    if synced:
        mins = int((_utcnow() - synced).total_seconds() // 60)
        lines.append(f"synced {mins} min ago" if mins < 120 else f"synced {mins // 60}h ago")
    lines.append("Context to act on, never a readout: one number only when it changes the plan "
                 "(a short night → lighter session or an earlier bed; low steps on a rest day → a "
                 "walk). Never diagnose from HR/HRV, never call it a health issue. Scale weight is "
                 "in WEIGHT.")
    return "## WEARABLE (fitbit)\n" + "\n".join(lines)


__all__ = ["sync_user", "sync_all", "handle_notifications", "users_for_owner_ids",
           "wearable_context", "SOURCE", "WEIGHT_NOTE_PREFIX"]
