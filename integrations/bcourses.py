"""bCourses (Canvas) calendar feed — Part 1.4. No OAuth.

The user pastes their bCourses calendar-feed URL (bCourses → Calendar → Calendar
Feed). Code sniffs it out of the inbound text, stores it on an `integrations` row
(provider=bcourses, meta.feed_url), replies in code, and kicks a first sync in the
background. A 6-hour scheduler job re-pulls the ICS for every feed on file and
upserts assignments / course events into the shared event store with
source='bcourses' and external_id=<ICS UID>, so UPCOMING, the heartbeat's
calendar gate, and the coach's calendar rules see due dates exactly like gcal
events — no special-casing downstream.

Pure HTTP + DB, no model calls. The feed URL is effectively a bearer secret for
that calendar: it lives only in integrations.meta and is never put in context.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, date, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

import config
from models import get_session, User, Integration, Event
from integrations import base
from events import upsert_external_event

logger = logging.getLogger("cued.integrations.bcourses")

SOURCE = "bcourses"

# Canvas user feeds: https://bcourses.berkeley.edu/feeds/calendars/user_<token>.ics
FEED_URL_RE = re.compile(
    r"https?://(?:www\.)?bcourses\.berkeley\.edu/feeds/calendars/user_[A-Za-z0-9_-]+\.ics",
    re.IGNORECASE,
)

SYNC_HORIZON_DAYS = 60       # same forward window as gcal
LOOKBACK_DAYS = 1            # keep yesterday's due dates so "PASSED" reads still work
MAX_EVENT_HOURS = 24         # skip semester-long blocks — noise
FAILS_BEFORE_ERROR = 3       # transient fetch blips don't flip the row to error
GOT_IT = "got it. i'll pull ur due dates in and plan around them"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ─── inbound: the pasted feed link ────────────────────────────────────────────

def find_feed_url(text: str | None) -> str | None:
    m = FEED_URL_RE.search(text or "")
    return m.group(0) if m else None


def save_feed(user_id: int, feed_url: str) -> None:
    """Upsert the bcourses row as connected with the feed URL. A re-paste replaces
    the URL (Canvas lets you regenerate the feed) and clears any error state."""
    session = get_session()
    try:
        integ = base.get_integration(session, user_id, SOURCE)
        if integ is None:
            integ = Integration(user_id=user_id, provider=SOURCE, status="connected", meta={})
            session.add(integ)
        meta = dict(integ.meta or {})
        meta["feed_url"] = feed_url
        meta.pop("last_error", None)
        meta.pop("fail_count", None)
        integ.meta = meta
        integ.status = "connected"
        integ.updated_at = _utcnow()
        session.commit()
    finally:
        session.close()


def _spawn_first_sync(user_id: int) -> None:
    """First pull right after the paste, off the webhook thread (the ICS fetch is a
    network call; the webhook has a ~15s budget)."""
    def _run():
        try:
            sync_user(user_id)
        except Exception:
            logger.exception("BCOURSES_FIRST_SYNC_FAILED user=%s", user_id)
    threading.Thread(target=_run, daemon=True).start()


def handle_inbound_feed_url(session, user, body: str, *, channel: str = "sms") -> bool:
    """Inbound pre-pass. If the message carries a bCourses feed URL: store it, reply
    in code, start the first sync, and return True (terminal — the coach never
    sees this turn as a question to answer). Returns False otherwise. Flag-gated
    no-op when BCOURSES_ENABLED is off."""
    if not config.BCOURSES_ENABLED:
        return False
    url = find_feed_url(body)
    if not url:
        return False
    from sms import send_sms
    save_feed(user.id, url)
    send_sms(user.phone, GOT_IT, user_id=user.id, message_type="integration_connected")
    logger.info("BCOURSES_FEED_SAVED user=%s channel=%s", user.id, channel)
    _spawn_first_sync(user.id)
    return True


# ─── ICS fetch + parse ────────────────────────────────────────────────────────

def _fetch_ics(feed_url: str) -> str:
    r = requests.get(feed_url, timeout=config.INTEGRATIONS_HTTP_TIMEOUT_S,
                     headers={"User-Agent": "cued-coach/1.0 (+https://cued.fit)"})
    r.raise_for_status()
    text = r.text or ""
    if "BEGIN:VCALENDAR" not in text[:200]:
        raise ValueError("feed did not return an ICS calendar")
    return text


def _to_utc(value, tz: ZoneInfo) -> tuple[datetime | None, bool]:
    """An icalendar DTSTART/DTEND `.dt` → (naive UTC, all_day). A bare date is an
    all-day marker at local midnight; a floating datetime is taken as local time."""
    if value is None:
        return None, False
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=tz)
        return value.astimezone(timezone.utc).replace(tzinfo=None), False
    if isinstance(value, date):
        local = datetime(value.year, value.month, value.day, tzinfo=tz)
        return local.astimezone(timezone.utc).replace(tzinfo=None), True
    return None, False


def _is_assignment(uid: str) -> bool:
    # Canvas UIDs: event-assignment-<id>, event-assignment-override-<id>,
    # event-calendar-event-<id>, event-planner-note-<id>.
    return uid.startswith("event-assignment")


def parse_ics(text: str, tz: ZoneInfo) -> list[dict]:
    """ICS text → [{external_id, title, start_utc, end_utc, all_day}]. Assignments get
    a 'due: ' prefix so the coach reads them as deadlines, not blocks."""
    from icalendar import Calendar
    cal = Calendar.from_ical(text)
    out = []
    for comp in cal.walk("VEVENT"):
        uid = str(comp.get("UID") or "").strip()
        if not uid:
            continue
        summary = str(comp.get("SUMMARY") or "").strip() or "(untitled)"
        dtstart = comp.get("DTSTART")
        dtend = comp.get("DTEND")
        start_utc, all_day = _to_utc(getattr(dtstart, "dt", None), tz)
        end_utc, _ = _to_utc(getattr(dtend, "dt", None), tz)
        if start_utc is None:
            continue
        if end_utc is not None and end_utc < start_utc:
            end_utc = start_utc
        title = f"due: {summary}" if _is_assignment(uid) else summary
        out.append({"external_id": uid[:200], "title": title, "start_utc": start_utc,
                    "end_utc": end_utc, "all_day": all_day})
    return out


# ─── sync ─────────────────────────────────────────────────────────────────────

def _record_failure(user_id: int, err: Exception) -> None:
    session = get_session()
    try:
        integ = base.get_integration(session, user_id, SOURCE)
        if integ is None:
            return
        meta = dict(integ.meta or {})
        meta["fail_count"] = int(meta.get("fail_count") or 0) + 1
        meta["last_error"] = str(err)[:300]
        integ.meta = meta
        integ.updated_at = _utcnow()
        flip = meta["fail_count"] >= FAILS_BEFORE_ERROR and integ.status == "connected"
        if flip:
            integ.status = "error"
        session.commit()
    finally:
        session.close()
    logger.warning("BCOURSES_FETCH_FAILED user=%s err=%s", user_id, str(err)[:120])


def _record_success(user_id: int, now: datetime) -> None:
    session = get_session()
    try:
        integ = base.get_integration(session, user_id, SOURCE)
        if integ is None:
            return
        meta = dict(integ.meta or {})
        meta["fail_count"] = 0
        meta.pop("last_error", None)
        meta["last_sync_at"] = now.isoformat()
        integ.meta = meta
        integ.status = "connected"
        integ.updated_at = now
        session.commit()
    finally:
        session.close()


def _delete_vanished(user_id: int, seen: set, lo: datetime, hi: datetime, now: datetime) -> int:
    """An event that dropped out of the feed (assignment deleted / unpublished) is
    soft-deleted so it stops showing up. Only inside the sync window — rows outside
    it were never re-pulled this run, so their absence means nothing."""
    session = get_session()
    try:
        rows = (session.query(Event)
                .filter(Event.user_id == user_id, Event.source == SOURCE,
                        Event.deleted_at.is_(None),
                        Event.occurred_at >= lo, Event.occurred_at < hi).all())
        n = 0
        for ev in rows:
            if ev.external_id not in seen:
                ev.deleted_at = now
                n += 1
        if n:
            session.commit()
        return n
    finally:
        session.close()


def sync_user(user_id: int) -> dict:
    """Pull one user's feed into the event store. Returns a summary dict."""
    if not config.BCOURSES_ENABLED:
        return {"skipped": "flag off"}
    session = get_session()
    try:
        integ = base.get_integration(session, user_id, SOURCE)
        if integ is None or integ.status not in ("connected", "error"):
            return {"skipped": "not connected"}
        feed_url = (integ.meta or {}).get("feed_url")
        user = session.get(User, user_id)
        tz = ZoneInfo((user.user_timezone if user else None) or "America/Los_Angeles")
    finally:
        session.close()
    if not feed_url:
        return {"skipped": "no feed url"}

    try:
        text = _fetch_ics(feed_url)
        parsed = parse_ics(text, tz)
    except Exception as e:
        _record_failure(user_id, e)
        return {"error": str(e)}

    now = _utcnow()
    lo = now - timedelta(days=LOOKBACK_DAYS)
    hi = now + timedelta(days=SYNC_HORIZON_DAYS)
    seen: set = set()
    upserted = 0
    for ev in parsed:
        start, end = ev["start_utc"], ev["end_utc"]
        if not (lo <= start < hi):
            continue
        if end is not None and (end - start) > timedelta(hours=MAX_EVENT_HOURS):
            continue
        upsert_external_event(user_id, source=SOURCE, external_id=ev["external_id"],
                              title=ev["title"], occurred_at=start, ends_at=end,
                              all_day=ev["all_day"])
        seen.add(ev["external_id"])
        upserted += 1
    deleted = _delete_vanished(user_id, seen, lo, hi, now)
    _record_success(user_id, now)
    logger.info("BCOURSES_SYNC user=%s upserted=%s deleted=%s parsed=%s",
                user_id, upserted, deleted, len(parsed))
    return {"upserted": upserted, "deleted": deleted}


def sync_all() -> int:
    """Scheduler entry (every 6h): re-pull every feed on file, including rows in
    error (a fixed feed heals itself on the next good pull)."""
    if not config.BCOURSES_ENABLED:
        return 0
    session = get_session()
    try:
        ids = [i.user_id for i in session.query(Integration)
               .filter(Integration.provider == SOURCE,
                       Integration.status.in_(("connected", "error"))).all()]
    finally:
        session.close()
    n = 0
    for uid in ids:
        try:
            sync_user(uid)
            n += 1
        except Exception:
            logger.exception("BCOURSES_SYNC_USER_FAILED user=%s", uid)
    return n


__all__ = ["SOURCE", "FEED_URL_RE", "GOT_IT", "find_feed_url", "save_feed",
           "handle_inbound_feed_url", "parse_ics", "sync_user", "sync_all"]
