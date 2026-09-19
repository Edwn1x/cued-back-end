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
from events import upsert_external_event, delete_external_event

logger = logging.getLogger("cued.integrations.gcal_sync")

SOURCE = "gcal"
SYNC_HORIZON_DAYS = 60
MAX_EVENT_HOURS = 24          # skip semester-long blocks — noise


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


def sync_user(user_id: int) -> dict:
    """Sync one connected user's calendars into the event store. Returns a summary."""
    if not config.GCAL_ENABLED:
        return {"skipped": "flag off"}
    token = base.get_valid_access_token(user_id, SOURCE)
    if not token:
        return {"skipped": "not connected"}

    session = get_session()
    try:
        user = session.get(User, user_id)
        tz = ZoneInfo((user.user_timezone if user else None) or "America/Los_Angeles")
        integ = base.get_integration(session, user_id, SOURCE)
        sync_state = dict((integ.meta or {}).get("gcal_sync", {})) if integ else {}
    finally:
        session.close()

    try:
        cals = gcal.list_calendars(token)
    except Exception as e:
        logger.warning("GCAL_LIST_CALENDARS_FAILED user=%s err=%s", user_id, e)
        return {"error": str(e)}

    now = datetime.now(timezone.utc)
    time_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    time_max = (now + timedelta(days=SYNC_HORIZON_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    upserted = deleted = 0

    for cal in cals:
        cid = cal.get("id")
        if not cid or cal.get("hidden"):
            continue
        token_for_cal = sync_state.get(cid)
        try:
            if token_for_cal:
                gevents, next_sync = gcal.list_events(token, cid, sync_token=token_for_cal)
            else:
                gevents, next_sync = gcal.list_events(token, cid, time_min=time_min, time_max=time_max)
        except gcal.SyncTokenExpired:
            gevents, next_sync = gcal.list_events(token, cid, time_min=time_min, time_max=time_max)
        except Exception as e:
            logger.warning("GCAL_CAL_SYNC_FAILED user=%s cal=%s err=%s", user_id, cid, e)
            continue

        for ge in gevents:
            ext = _external_id(cid, ge)
            if ge.get("status") == "cancelled":
                if delete_external_event(user_id, source=SOURCE, external_id=ext):
                    deleted += 1
                continue
            if _should_skip(ge):
                delete_external_event(user_id, source=SOURCE, external_id=ext)  # e.g. now-declined
                continue
            start_utc, all_day = _parse_dt(ge.get("start"), tz)
            end_utc, _ = _parse_dt(ge.get("end"), tz)
            if start_utc is None:
                continue
            if end_utc and (end_utc - start_utc) > timedelta(hours=MAX_EVENT_HOURS):
                continue
            upsert_external_event(user_id, source=SOURCE, external_id=ext,
                                  title=ge.get("summary") or "(busy)",
                                  occurred_at=start_utc, ends_at=end_utc, all_day=all_day)
            upserted += 1
        if next_sync:
            sync_state[cid] = next_sync

    session = get_session()
    try:
        integ = base.get_integration(session, user_id, SOURCE)
        if integ:
            m = dict(integ.meta or {})
            m["gcal_sync"] = sync_state
            m["last_sync_at"] = now.replace(tzinfo=None).isoformat()
            integ.meta = m
            session.commit()
    finally:
        session.close()
    logger.info("GCAL_SYNC user=%s upserted=%s deleted=%s", user_id, upserted, deleted)
    return {"upserted": upserted, "deleted": deleted}


def sync_all() -> int:
    """Scheduler entry (every 30 min): sync every connected gcal user."""
    if not config.GCAL_ENABLED:
        return 0
    session = get_session()
    try:
        ids = [i.user_id for i in session.query(Integration)
               .filter(Integration.provider == SOURCE, Integration.status == "connected").all()]
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
