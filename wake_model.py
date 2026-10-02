"""
Layered wake model — "when did this user actually wake up TODAY?"

Founder (2026-10-02): wearable-first wake/sleep working in BOTH directions (earlier too),
and for people without a wearable, detect that they're awake from them being on their
phone — like Apple Fitness sends the recap once you're up. We have NO app and cannot see
screen-on/unlock or read receipts (we only send those). We CAN see: inbound texts,
tapbacks, and workout-card opens. So: a layered model with a precedence.

    resolve_wake(user, session, now=) -> WakeInfo | None

  1. activity         the EARLIEST thing they did today that plausibly marks waking: an
                      inbound text, a tapback, or a card open (messages table +
                      users.last_active_at). Plausibility guard so "still up at 3am" is
                      NOT a wake: local hour >= WAKE_DETECT_EARLIEST_LOCAL_HOUR AND at
                      least WAKE_DETECT_MIN_HOURS_AFTER_SLEEP after their sleep start
                      (measured typical bed if the watch has one, else profile sleep_time).
  2. measured_today   the watch's wake this morning (wearable_read.today_sleep) when the
                      row is FRESH (synced within WEARABLE_WAKE_FRESH_HOURS of sleep_end)
                      and PLAUSIBLE (sleep_minutes >= WEARABLE_WAKE_MIN_SLEEP_MINUTES — a
                      nap is not a wake; same earliest-hour floor). Both directions.
  3. measured_typical the median typical wake over recent nights (#146) — unchanged.
  4. profile          wake_time (alt-day honoured). FALLBACK ONLY, never nulled.

Consumers (heartbeat.py): the standing quiet-hours morning END (an activity /
measured_today wake REPLACES the end — wake + QUIET_AFTER_WAKE_MIN — instead of only
extending it) and the MORNING OPEN anchor (#156). For typical / profile / None the
callers keep today's code path exactly, so nothing regresses when there is no fresh data.

Read-only except `touch_last_active` (the per-event stamp, called from the inbound path
and the card page). Flag-gated by WAKE_MODEL_ENABLED; fail-open: flag off or ANY
exception → None and callers behave byte-for-byte as today.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import config
from models import get_session, Message, User

logger = logging.getLogger("cued.wake_model")

SOURCES = ("activity", "measured_today", "measured_typical", "profile")
# The two "we know it's TODAY's wake" sources — the ones consumers act on directly.
TODAY_SOURCES = ("activity", "measured_today")


@dataclass
class WakeInfo:
    source: str                    # one of SOURCES
    local_hm: tuple[int, int]      # wake (hour, minute) in the user's local zone
    at_utc: datetime | None        # the wake instant, naive UTC (today at local_hm)
    detail: str = ""               # short diagnostic ("text at 11:00", "card open", ...)


# ─── small helpers (local to this module; the heartbeat's own stay untouched) ──

def _ref(now) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


def _naive(aware) -> datetime:
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _tz(user):
    from heartbeat import _user_tz
    return _user_tz(user)


def _sleep_start_local(user, session, local, *, now=None):
    """Aware-local instant they went to bed before today's wake: the measured typical
    bedtime (watch) when available, else the profile sleep_time; None when neither
    parses. A bed hour before noon (01:00) is today's small hours; later is yesterday."""
    from heartbeat import _measured_sw_hours, _sleep_hhmm
    hm = None
    sw = _measured_sw_hours(user, session, now=now)
    if sw:
        hm = sw[0]
    if hm is None:
        hm = _sleep_hhmm(user)
    if hm is None:
        return None
    bed = local.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if hm[0] >= 12:
        bed -= timedelta(days=1)
    return bed


def _plausible_wake(local_dt, sleep_start_local) -> bool:
    """The still-up guard: a plausible wake is at/after the earliest local hour AND far
    enough after they went to bed. Without a parseable bedtime only the hour floor applies."""
    if local_dt.hour < config.WAKE_DETECT_EARLIEST_LOCAL_HOUR:
        return False
    if sleep_start_local is not None:
        if local_dt < sleep_start_local + timedelta(hours=config.WAKE_DETECT_MIN_HOURS_AFTER_SLEEP):
            return False
    return True


# ─── layers ───────────────────────────────────────────────────────────────────

def _activity_wake(user, session, local, *, now=None) -> WakeInfo | None:
    """Layer 1. Candidates (today's local day, up to `now`): every inbound Message
    (text — reactions are not stored as rows) and users.last_active_at (the per-event
    stamp: tapbacks + card opens + texts). The EARLIEST candidate that passes the
    still-up guard is the wake; candidates failing it are ignored, not disqualifying."""
    tz = local.tzinfo
    day_start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_start = _naive(day_start_local)
    now_utc = _naive(local)
    cands: list[tuple[datetime, str]] = []
    rows = (session.query(Message.created_at, Message.message_type)
            .filter(Message.user_id == user.id, Message.direction == "in",
                    Message.created_at >= day_start, Message.created_at <= now_utc)
            .order_by(Message.created_at.asc()).all())
    for created_at, mtype in rows:
        if created_at is None:
            continue
        cands.append((created_at, "tapback" if mtype == "reaction" else "text"))
    last = getattr(user, "last_active_at", None)
    if last is not None and day_start <= last <= now_utc:
        cands.append((last, "activity"))   # a card open / tapback / the latest text
    if not cands:
        return None
    sleep_start = _sleep_start_local(user, session, local, now=now)
    for at, kind in sorted(cands, key=lambda c: c[0]):
        at_local = at.replace(tzinfo=timezone.utc).astimezone(tz)
        if not _plausible_wake(at_local, sleep_start):
            continue
        return WakeInfo(source="activity", local_hm=(at_local.hour, at_local.minute), at_utc=at,
                        detail=f"{kind} at {at_local.strftime('%H:%M')}")
    return None


def _measured_today_wake(user, session, local, *, now=None) -> WakeInfo | None:
    """Layer 2. Today's watch wake, FRESH + PLAUSIBLE; used in BOTH directions."""
    if not config.HEARTBEAT_WEARABLE_AWARE_ENABLED:
        return None
    from wearable_read import today_sleep
    ts = today_sleep(user, session, now=now)
    if ts is None:
        return None
    if ts.sleep_minutes is None or ts.sleep_minutes < config.WEARABLE_WAKE_MIN_SLEEP_MINUTES:
        return None                                   # a nap (or unknown length) is not a wake
    if ts.synced_at is None:
        return None
    age_h = (ts.synced_at - ts.sleep_end_utc).total_seconds() / 3600.0
    if age_h > config.WEARABLE_WAKE_FRESH_HOURS:
        return None                                   # learned about it too late to be "now"
    se = ts.sleep_end_local
    if se > local:
        return None                                   # a wake "in the future" (clock skew) — ignore
    if se.hour < config.WAKE_DETECT_EARLIEST_LOCAL_HOUR:
        return None
    return WakeInfo(source="measured_today", local_hm=(se.hour, se.minute), at_utc=ts.sleep_end_utc,
                    detail=f"watch sleep_end {se.strftime('%H:%M')}, {ts.sleep_minutes} min asleep")


def _typical_wake(user, session, local, *, now=None) -> WakeInfo | None:
    """Layer 3. The measured TYPICAL wake (#146) — same helper quiet hours already use."""
    from heartbeat import _measured_sw_hours
    sw = _measured_sw_hours(user, session, now=now)
    if not sw:
        return None
    hm = sw[1]
    at = local.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    return WakeInfo(source="measured_typical", local_hm=hm, at_utc=_naive(at), detail="median typical wake")


def _profile_wake(user, local) -> WakeInfo | None:
    """Layer 4. wake_time (alt-day honoured). Fallback only."""
    from heartbeat import _wake_hhmm_for
    hm = _wake_hhmm_for(user, local.date())
    if not hm:
        return None
    at = local.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    return WakeInfo(source="profile", local_hm=hm, at_utc=_naive(at), detail="profile wake_time")


def resolve_wake(user, session, *, now=None) -> WakeInfo | None:
    """The layered wake for TODAY (user-local). First layer that applies wins:
    activity → measured_today → measured_typical → profile. None when the flag is off,
    there is no session, nothing resolves, or anything raises (fail-open: callers then
    behave exactly as today). NO DB writes."""
    if not config.WAKE_MODEL_ENABLED or session is None or user is None:
        return None
    try:
        local = _ref(now).astimezone(_tz(user))
        for layer in (lambda: _activity_wake(user, session, local, now=now),
                      lambda: _measured_today_wake(user, session, local, now=now),
                      lambda: _typical_wake(user, session, local, now=now),
                      lambda: _profile_wake(user, local)):
            info = layer()
            if info is not None:
                return info
        return None
    except Exception as e:  # noqa: BLE001
        logger.warning("WAKE_MODEL_FAILED user=%s err=%s", getattr(user, "id", "?"), e)
        return None


# ─── the per-event activity stamp (the ONE write in this module) ──────────────

def touch_last_active(user_id: int, *, at: datetime | None = None) -> None:
    """Stamp users.last_active_at = now (naive UTC) for an inbound text / tapback / a
    workout-card open. Own short session, one UPDATE, fail-open: this must never block
    or fail the request that triggered it."""
    if not user_id:
        return
    when = at or datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        s = get_session()
        try:
            s.query(User).filter(User.id == user_id).update({User.last_active_at: when},
                                                             synchronize_session=False)
            s.commit()
        finally:
            s.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("LAST_ACTIVE_STAMP_FAILED user=%s err=%s", user_id, e)


__all__ = ["resolve_wake", "touch_last_active", "WakeInfo", "SOURCES", "TODAY_SOURCES"]
