"""Google Calendar sync (Part 1.2) — pull calendars → upsert into the event store.

Pure API + DB, no model calls. Per-calendar incremental sync via a syncToken kept in
integration.meta['gcal_sync']; a 410 drops the token and does a full re-pull. Events
land in the shared events table with source='gcal' and external_id='<calendar>:<event>',
so the existing UPCOMING reader/context surfaces them with no special-casing. The
30-min scheduler job (sync_all) fans this out over every connected user.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import config
from models import get_session, User, Integration
from integrations import base, gcal
import time

from events import upsert_external_events, delete_external_event, prune_external_events

logger = logging.getLogger("cued.integrations.gcal_sync")

SOURCE = "gcal"
SYNC_HORIZON_DAYS = 60
MAX_EVENT_HOURS = 24          # skip semester-long blocks — noise

# How this sync actually runs in prod (checked 2026-10-27, user 77 after a dozen ticks:
# integration.meta['gcal_sync'] == {}): the full pull asks Google for orderBy=startTime, and
# an ORDERED events.list never carries a nextSyncToken — so the incremental branch below has
# never run and every tick is a FULL 60-day pull per calendar. That is the behaviour we KEEP
# on purpose: a sync token from a windowed request pins that window (timeMin/timeMax can't be
# re-sent with it), so after SYNC_HORIZON_DAYS nothing new would ever arrive. What a full pull
# was missing is the other half of correctness — rows that silently VANISH from the window
# (an event the user MOVED to another calendar has no status=cancelled tombstone on the old
# one; neither does one whose calendar was un-shared) stayed in the mirror forever. Live
# 2026-10-27: user 77 moved three coach-created office-hour blocks onto his "Class schedule"
# calendar; the main-calendar copies lingered as ghosts through the next sync. So: after
# every full pull of a calendar, prune that calendar's rows in the pulled window that the
# pull didn't return (bcourses has done this since day one).


def _parse_dt(node, tz) -> tuple[datetime | None, bool]:
    """A Google start/end node → (naive UTC, all_day). {dateTime}=timed, {date}=all-day."""
    if not node:
        return None, False
    if node.get("dateTime"):
        dt = datetime.fromisoformat(node["dateTime"].replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None), False
    if node.get("date"):
        local = datetime.fromisoformat(node["date"]).replace(tzinfo=tz)   # local midnight
        return local.astimezone(timezone.utc).replace(tzinfo=None), True
    return None, False


def _should_skip(gevent) -> bool:
    """Declined / free-transparent / birthday events are noise (spec §1.2)."""
    if gevent.get("transparency") == "transparent":
        return True
    if gevent.get("eventType") == "birthday":
        return True
    for a in gevent.get("attendees") or []:
        if a.get("self") and a.get("responseStatus") == "declined":
            return True
    return False


def _external_id(calendar_id: str, gevent: dict) -> str:
    return f"{calendar_id}:{gevent.get('id')}"


def event_row(calendar_id: str, gevent: dict, tz) -> dict | None:
    """One Google event (an expanded instance, singleEvents=true) → the event-store row
    shape upsert_external_events takes. None = skip (no start, or a semester-long block).
    Shared with the coach's calendar write tools so a coach-created series mirrors
    exactly the way the 30-min sync would (same ids, same fields)."""
    start_utc, all_day = _parse_dt(gevent.get("start"), tz)
    end_utc, _ = _parse_dt(gevent.get("end"), tz)
    if start_utc is None:
        return None
    if end_utc and (end_utc - start_utc) > timedelta(hours=MAX_EVENT_HOURS):
        return None
    return {"external_id": _external_id(calendar_id, gevent), "title": gevent.get("summary") or "(busy)",
            "occurred_at": start_utc, "ends_at": end_utc, "all_day": all_day,
            "series_id": gevent.get("recurringEventId") or None}


def sync_user(user_id: int) -> dict:
    """Sync one user's calendars into the event store — every connected Google account
    (primary + any second login), each with its own token and sync state. Returns a
    summary; {"skipped": "not connected"} when no row is connected."""
    if not config.GCAL_ENABLED:
        return {"skipped": "flag off"}
    session = get_session()
    try:
        row_ids = [r.id for r in base.rows_for(session, user_id, SOURCE) if r.status == "connected"]
    finally:
        session.close()
    if not row_ids:
        return {"skipped": "not connected"}
    total = {"upserted": 0, "deleted": 0, "pruned": 0, "accounts": 0}
    for rid in row_ids:
        r = _sync_row(user_id, rid)
        if "upserted" in r:
            total["upserted"] += r["upserted"]
            total["deleted"] += r["deleted"]
            total["pruned"] += r.get("pruned", 0)
            total["accounts"] += 1
    if not total["accounts"]:
        return {"skipped": "not connected"}
    return total


def _sync_row(user_id: int, integration_id: int) -> dict:
    """One connected row's calendars → the event store."""
    token = base.get_valid_access_token(user_id, SOURCE, integration_id=integration_id)
    if not token:
        return {"skipped": "not connected"}

    session = get_session()
    try:
        user = session.get(User, user_id)
        tz = ZoneInfo((user.user_timezone if user else None) or "America/Los_Angeles")
        integ = session.get(Integration, integration_id)
        sync_state = dict((integ.meta or {}).get("gcal_sync", {})) if integ else {}
    finally:
        session.close()

    try:
        cals = gcal.list_calendars(token)
    except Exception as e:
        # Grants made with events.readonly alone can't list calendars (403). The
        # primary calendar is still readable, so sync that rather than nothing; a
        # re-connect picks up the calendarlist scope and the secondary calendars.
        logger.warning("GCAL_LIST_CALENDARS_FAILED user=%s err=%s — falling back to primary", user_id, e)
        cals = [{"id": "primary", "summary": "primary"}]

    now = datetime.now(timezone.utc)
    time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=SYNC_HORIZON_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    win_lo = now.replace(tzinfo=None)
    win_hi = (now + timedelta(days=SYNC_HORIZON_DAYS)).replace(tzinfo=None)
    upserted = deleted = unchanged = pruned = 0
    t0 = time.monotonic()

    for cal in cals:
        cid = cal.get("id")
        if not cid or cal.get("hidden"):
            continue
        token_for_cal = sync_state.get(cid)
        full_pull = not token_for_cal
        try:
            if token_for_cal:
                gevents, next_sync = gcal.list_events(token, cid, sync_token=token_for_cal)
            else:
                gevents, next_sync = gcal.list_events(token, cid, time_min=time_min, time_max=time_max)
        except gcal.SyncTokenExpired:
            full_pull = True
            gevents, next_sync = gcal.list_events(token, cid, time_min=time_min, time_max=time_max)
        except Exception as e:
            logger.warning("GCAL_CAL_SYNC_FAILED user=%s cal=%s err=%s", user_id, cid, e)
            continue

        batch: list[dict] = []
        for ge in gevents:
            ext = _external_id(cid, ge)
            if ge.get("status") == "cancelled":
                if delete_external_event(user_id, source=SOURCE, external_id=ext):
                    deleted += 1
                continue
            if _should_skip(ge):
                delete_external_event(user_id, source=SOURCE, external_id=ext)  # e.g. now-declined
                continue
            row = event_row(cid, ge, tz)
            if row is not None:
                batch.append(row)
        # One session + one commit per calendar; rows that didn't change aren't rewritten.
        c = upsert_external_events(user_id, source=SOURCE, rows=batch)
        upserted += c["inserted"] + c["updated"]
        unchanged += c["unchanged"]
        if full_pull:
            # A full pull is the whole truth for this calendar's window: anything we hold
            # that Google didn't return is gone (moved / un-shared / deleted without a
            # tombstone we saw). An EMPTY pull is not trusted as "they cleared everything"
            # — a transient empty 200 would wipe the mirror — so that case is logged, not pruned.
            if gevents:
                keep = {r["external_id"] for r in batch}
                n_pruned = prune_external_events(user_id, source=SOURCE, keep=keep, lo=win_lo, hi=win_hi,
                                                 external_prefix=f"{cid}:")
                if n_pruned:
                    logger.info("GCAL_PRUNED user=%s cal=%s rows=%s", user_id, cid, n_pruned)
                pruned += n_pruned
            else:
                logger.info("GCAL_PRUNE_SKIPPED_EMPTY user=%s cal=%s", user_id, cid)
        if next_sync:
            sync_state[cid] = next_sync

    session = get_session()
    try:
        integ = session.get(Integration, integration_id)
        if integ:
            m = dict(integ.meta or {})
            m["gcal_sync"] = sync_state
            m["last_sync_at"] = now.replace(tzinfo=None).isoformat()
            integ.meta = m
            session.commit()
    finally:
        session.close()
    logger.info("GCAL_SYNC user=%s row=%s upserted=%s unchanged=%s deleted=%s pruned=%s took=%.1fs",
                user_id, integration_id, upserted, unchanged, deleted, pruned, time.monotonic() - t0)
    return {"upserted": upserted, "deleted": deleted + pruned, "unchanged": unchanged, "pruned": pruned}


def sync_all() -> int:
    """Scheduler entry (every 30 min): sync every connected gcal user."""
    if not config.GCAL_ENABLED:
        return 0
    session = get_session()
    try:
        ids = sorted({i.user_id for i in session.query(Integration)
                      .filter(Integration.provider == SOURCE, Integration.status == "connected").all()})
    finally:
        session.close()
    n = 0
    for uid in ids:
        try:
            sync_user(uid)
            n += 1
        except Exception:
            logger.exception("GCAL_SYNC_USER_FAILED user=%s", uid)
    return n
