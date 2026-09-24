"""Google Health sync (Part 2a) — daily summaries → wearable_days, scale readings →
weight_logs, and the `## WEARABLE` context block the coach + heartbeat read.

Pure API + DB, no model calls. Window = the last GOOGLE_HEALTH_SYNC_DAYS local days
(today + yesterday) on the 30-min poll; GOOGLE_HEALTH_BACKFILL_DAYS on the first pull
after connect. Per sync: 3 daily rollups (steps, total-calories, active-zone-minutes) +
2 daily-type lists (resting HR, HRV) + sleep reconcile + weight list = 7 calls.
Webhook notifications (routes: /oauth/google_health/webhook) call sync_user for the
healthUserId so data is fresh minutes after the watch syncs, not 30 later.

Sleep is keyed by the LOCAL date the session ENDS (the morning) and only MAIN_SLEEP
counts — a NAP never overwrites last night. Interval timestamps are RFC3339 UTC with a
separate UTC offset; we store naive UTC like everything else.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone, timedelta, date
from zoneinfo import ZoneInfo

import config
from models import get_session, User, Integration, WearableDay, WeightLog
from integrations import base, google_health as gh

logger = logging.getLogger("cued.integrations.google_health_sync")

SOURCE = "google_health"
CONTEXT_MAX_AGE_DAYS = 3         # no row newer than this → block says "no recent data"
WEIGHT_NOTE_PREFIX = "ghealth:"  # weight_logs.notes = 'ghealth:<dataPoint name>' — idempotency key


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _tz(user) -> ZoneInfo:
    return ZoneInfo((getattr(user, "user_timezone", None)) or "America/Los_Angeles")


def _local_today(tz) -> date:
    return datetime.now(tz).date()


def _parse_ts(s: str | None) -> datetime | None:
    """RFC3339 ('2026-09-24T06:31:00Z' / '…+00:00' / fractional) → naive UTC."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _offset_seconds(s) -> int | None:
    """'-25200s' → -25200."""
    try:
        return int(str(s).rstrip("s"))
    except (TypeError, ValueError):
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


def _record_weight(session, user, sample: dict) -> bool:
    """One weight sample → weight_logs row (idempotent on the data point name) +
    users.weight_lbs latest-wins with the same protein follow rule as log_weight."""
    name = sample.get("name")
    lbs = sample.get("lbs")
    if not name or lbs is None or not (60 <= lbs <= 600):
        return False
    note = f"{WEIGHT_NOTE_PREFIX}{name}"
    if session.query(WeightLog.id).filter(WeightLog.user_id == user.id, WeightLog.notes == note).first():
        return False
    when = _parse_ts(sample.get("sample_time")) or _utcnow()
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
                logger.warning("GOOGLE_HEALTH_WEIGHT_PROTEIN_FOLLOW_FAILED user=%s err=%s", user.id, e)
    return True


def _main_sleep_by_day(sessions: list[dict], tz) -> dict[str, dict]:
    """{local end-date: session} keeping MAIN_SLEEP over NAP, longest if several."""
    out: dict[str, dict] = {}
    for s in sessions:
        iv = s.get("interval") or {}
        end_utc = _parse_ts(iv.get("endTime"))
        if not end_utc:
            continue
        off = _offset_seconds(iv.get("endUtcOffset"))
        local_end = (end_utc + timedelta(seconds=off)) if off is not None else \
            end_utc.replace(tzinfo=timezone.utc).astimezone(tz).replace(tzinfo=None)
        day = local_end.date().isoformat()
        is_main = (s.get("type") == "MAIN_SLEEP")
        mins = int(((s.get("summary") or {}).get("minutesAsleep")) or 0)
        cur = out.get(day)
        if cur is None:
            out[day] = s | {"_main": is_main, "_mins": mins}
            continue
        if (is_main and not cur["_main"]) or (is_main == cur["_main"] and mins > cur["_mins"]):
            out[day] = s | {"_main": is_main, "_mins": mins}
    return out


def sync_user(user_id: int, *, days: int | None = None) -> dict:
    """Pull one connected user's window into wearable_days (+ weight_logs). Returns a
    summary dict; never raises for API trouble (recorded via note_sync_failure)."""
    if not config.GOOGLE_HEALTH_ENABLED:
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
        days = config.GOOGLE_HEALTH_BACKFILL_DAYS if first else config.GOOGLE_HEALTH_SYNC_DAYS
    days = max(1, int(days or 1))
    end = _local_today(tz)
    start = end - timedelta(days=days - 1)
    window = [(start + timedelta(days=i)).isoformat() for i in range(days)]

    try:
        steps = gh.get_steps_by_day(token, start, end)
        calories = gh.get_calories_by_day(token, start, end)
        azm = gh.get_active_zone_minutes_by_day(token, start, end)
        rhr = gh.get_resting_hr_by_day(token, start, end)
        hrv = gh.get_hrv_by_day(token, start, end)
        sleep_sessions = gh.get_sleep_sessions(token, start)
        weights = gh.get_weight_samples(token, start)
    except gh.HealthAPIError as e:
        if e.status == 401:
            # the grant is gone (revoked in Google account settings) — say so once via the
            # status line rather than retrying forever
            base.mark_revoked(user_id, SOURCE)
            return {"error": "revoked"}
        base.note_sync_failure(user_id, SOURCE, e)
        return {"error": str(e)[:120]}
    except Exception as e:  # noqa: BLE001 — network etc.
        base.note_sync_failure(user_id, SOURCE, e)
        return {"error": str(e)[:120]}

    main_sleep = _main_sleep_by_day(sleep_sessions, tz)

    days_written = new_weights = 0
    session = get_session()
    try:
        user = session.get(User, user_id)
        for d in window:
            fields: dict = {}
            if d in steps:
                fields["steps"] = steps[d]
            if d in calories:
                fields["calories_out"] = calories[d]
            if d in azm:
                fields["active_minutes"] = azm[d]
            if d in rhr:
                fields["resting_hr"] = rhr[d]
            if d in hrv:
                fields["hrv_rmssd"] = hrv[d]
            sl = main_sleep.get(d)
            if sl and sl["_mins"]:
                iv = sl.get("interval") or {}
                fields["sleep_minutes"] = sl["_mins"]
                fields["sleep_start"] = _parse_ts(iv.get("startTime"))
                fields["sleep_end"] = _parse_ts(iv.get("endTime"))
                summ = sl.get("summary") or {}
                asleep, awake = sl["_mins"], int(summ.get("minutesAwake") or 0)
                if asleep + awake > 0:
                    fields["sleep_efficiency"] = int(round(100 * asleep / (asleep + awake)))
            if fields:
                _upsert_day(session, user_id, d, **fields)
                days_written += 1
        for sample in weights:
            if _record_weight(session, user, sample):
                new_weights += 1
        session.commit()
    finally:
        session.close()

    base.note_sync_success(user_id, SOURCE, first_sync_done=True,
                           last_window=[start.isoformat(), end.isoformat()])
    logger.info("GOOGLE_HEALTH_SYNC user=%s window=%s..%s days=%s new_weights=%s", user_id, start, end,
                days_written, new_weights)
    return {"days": days_written, "new_weights": new_weights, "window": [start.isoformat(), end.isoformat()]}


def sync_all() -> int:
    """Scheduler entry (every 30 min): every connected user. `error` rows keep being
    polled — they heal on the next good pull (base.note_sync_success)."""
    if not config.GOOGLE_HEALTH_ENABLED:
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
            logger.exception("GOOGLE_HEALTH_SYNC_USER_FAILED user=%s", uid)
    return n


# ─── webhook notifications (routes → here) ───────────────────────────────────

def users_for_health_ids(health_ids) -> list[int]:
    ids = [str(h) for h in health_ids if h]
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


def _health_ids(payload) -> set[str]:
    """Accept one notification {data:{healthUserId…}}, a bare {healthUserId…}, or a JSON
    array of either (batched delivery)."""
    items = payload if isinstance(payload, list) else [payload]
    out = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        d = it.get("data") if isinstance(it.get("data"), dict) else it
        hid = d.get("healthUserId")
        if hid:
            out.add(str(hid))
    return out


def handle_notifications(payload, *, run_async: bool = True) -> list[int]:
    """Map the notification's healthUserId(s) → users and sync each once, off-thread
    (the endpoint must 204 immediately). Returns the user ids kicked; unknown ids are
    ignored. A notification only says 'new data' — never what it is — so we re-pull."""
    user_ids = users_for_health_ids(_health_ids(payload))
    if not user_ids:
        return []
    logger.info("GOOGLE_HEALTH_NOTIFY users=%s", user_ids)

    def _run():
        for uid in user_ids:
            try:
                sync_user(uid)
            except Exception:
                logger.exception("GOOGLE_HEALTH_NOTIFY_SYNC_FAILED user=%s", uid)

    if run_async:
        threading.Thread(target=_run, name="google-health-notify-sync", daemon=True).start()
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
    if not config.GOOGLE_HEALTH_ENABLED:
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
                "watch may not have synced to the Fitbit / Google Health app. Don't mention it unless they ask.")

    lines = []
    today_s = today.isoformat()
    t = by_day.get(today_s)
    # last night = the sleep that ENDED today; fall back to the most recent night
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


__all__ = ["sync_user", "sync_all", "handle_notifications", "users_for_health_ids",
           "wearable_context", "SOURCE", "WEIGHT_NOTE_PREFIX"]
