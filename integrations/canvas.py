"""Canvas personal access token — Part 1.4b. The richer bCourses connection.

The feed (bcourses.py) is a calendar: due dates only. A pasted personal access
token (bCourses → Account → Settings → New Access Token) unlocks the planner API,
which also knows what's already been SUBMITTED, the course roster, and the user's
own to-do notes. Both connections coexist:

  - the feed sync keeps running for feed-only users and resumes automatically if
    the token is ever revoked;
  - while a token is valid it supersedes the feed's events for that user (the same
    assignment would otherwise appear twice), so the coach sees one board.

Security posture: a student token is full-account access with no scope limits, so
it is treated like an OAuth secret — Fernet at rest (crypto.py), decrypted at point
of use, scrubbed from the logged inbound row, never in context. The coach sees
`canvas connected (cs70 · cs61c)`, never the token.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone, timedelta

import requests

import config
from models import get_session, Integration
from integrations import base, crypto
from events import upsert_external_event, delete_external_event, prune_external_events

logger = logging.getLogger("cued.integrations.canvas")

SOURCE = "canvas"
FEED_SOURCE = "bcourses"          # the calendar-feed twin this provider supersedes

# Canvas user tokens: "<shard>~<64 alnum>" (e.g. "7~aB3…" or "1234~aB3…").
TOKEN_RE = re.compile(r"(?<![A-Za-z0-9~])(\d{1,8}~[A-Za-z0-9]{32,128})(?![A-Za-z0-9~])")

SYNC_HORIZON_DAYS = 60
LOOKBACK_DAYS = 1
MAX_EVENT_HOURS = 24
INLINE_TIMEOUT_S = 8              # the paste reply waits this long, then goes async
PER_PAGE = 100
MAX_PAGES = 10

REPLY_OK = "got it. i can see ur bcourses now, {n} things due on the board"
REPLY_EMPTY = "got it. i can see ur bcourses now, nothing due on it yet tho"
REPLY_ASYNC = "got it. i can see ur bcourses now, pulling ur due dates in"
REPLY_BAD_TOKEN = ("that token didn't work, make sure u copied the whole thing "
                   "(it starts with a number and a ~)")
REPLY_TRY_LATER = "couldn't reach bcourses just now, try pasting it again in a bit"
REDACTED = "[bcourses token]"

_DONE_KEYS = ("submitted", "graded", "excused")


class InvalidToken(Exception):
    """Canvas said 401 — the token is wrong, expired, or was deleted."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def find_token(text: str | None) -> str | None:
    m = TOKEN_RE.search(text or "")
    return m.group(1) if m else None


# ─── API client ───────────────────────────────────────────────────────────────

def _base_url() -> str:
    return (config.CANVAS_BASE_URL or "").rstrip("/")


def _get(token: str, path_or_url: str, params: dict | None = None, *, timeout: float | None = None):
    url = path_or_url if path_or_url.startswith("http") else f"{_base_url()}{path_or_url}"
    r = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"},
                     timeout=timeout or config.INTEGRATIONS_HTTP_TIMEOUT_S)
    if r.status_code == 401:
        raise InvalidToken("401 from canvas")
    r.raise_for_status()
    return r.json(), r.links.get("next", {}).get("url")


def _get_all(token: str, path: str, params: dict, *, timeout: float | None = None) -> list:
    out, nxt, pages = [], None, 0
    body, nxt = _get(token, path, params, timeout=timeout)
    out.extend(body or [])
    while nxt and pages < MAX_PAGES:
        body, nxt = _get(token, nxt, timeout=timeout)
        out.extend(body or [])
        pages += 1
    return out


def whoami(token: str, *, timeout: float | None = None) -> dict:
    body, _ = _get(token, "/api/v1/users/self", timeout=timeout)
    return body or {}


def list_courses(token: str, *, timeout: float | None = None) -> list[dict]:
    return _get_all(token, "/api/v1/courses",
                    {"enrollment_state": "active", "per_page": PER_PAGE}, timeout=timeout)


def planner_items(token: str, start: datetime, end: datetime, *, timeout: float | None = None) -> list[dict]:
    return _get_all(token, "/api/v1/planner/items",
                    {"start_date": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "end_date": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "per_page": PER_PAGE}, timeout=timeout)


# ─── inbound: the pasted token ────────────────────────────────────────────────

def save_token(user_id: int, token: str, *, external_id: str | None) -> None:
    session = get_session()
    try:
        integ = base.get_integration(session, user_id, SOURCE)
        if integ is None:
            integ = Integration(user_id=user_id, provider=SOURCE, status="connected", meta={})
            session.add(integ)
        integ.access_token = crypto.encrypt(token)
        integ.refresh_token = None
        integ.expires_at = None
        integ.external_id = (external_id or "")[:64] or None
        meta = dict(integ.meta or {})
        meta.pop("last_error", None)
        meta.pop("fail_count", None)
        meta["base_url"] = _base_url()
        integ.meta = meta
        integ.status = "connected"
        integ.updated_at = _utcnow()
        session.commit()
    finally:
        session.close()


def _spawn_sync(user_id: int) -> None:
    def _run():
        try:
            sync_user(user_id)
        except Exception:
            logger.exception("CANVAS_BG_SYNC_FAILED user=%s", user_id)
    threading.Thread(target=_run, daemon=True).start()


def handle_inbound_token(session, user, body: str, *, channel: str = "sms") -> bool:
    """Inbound pre-pass. A pasted Canvas token is validated (users/self), stored
    encrypted, scrubbed from the logged inbound row, and answered in code with what
    the first sync found. Terminal whenever a token-shaped string was present —
    valid or not — so the model never gets a bearer token as a message. Flag-gated."""
    if not config.CANVAS_ENABLED:
        return False
    token = find_token(body)
    if not token:
        return False
    from sms import send_sms
    # Scrub first: whatever happens next, the plaintext must not sit in messages.
    base.redact_inbound(user.id, token, REDACTED)

    try:
        me = whoami(token, timeout=INLINE_TIMEOUT_S)
    except InvalidToken:
        logger.info("CANVAS_TOKEN_REJECTED user=%s", user.id)
        send_sms(user.phone, REPLY_BAD_TOKEN, user_id=user.id, message_type="integration_connected")
        return True
    except Exception as e:
        logger.warning("CANVAS_TOKEN_CHECK_FAILED user=%s err=%s", user.id, str(e)[:120])
        send_sms(user.phone, REPLY_TRY_LATER, user_id=user.id, message_type="integration_connected")
        return True

    save_token(user.id, token, external_id=str(me.get("id") or "") or None)
    logger.info("CANVAS_TOKEN_SAVED user=%s channel=%s canvas_user=%s", user.id, channel, me.get("id"))

    # First pull inline (bounded) so the reply can say what's on the board; if it
    # doesn't finish in time the sync just happens in the background instead.
    try:
        res = sync_user(user.id, timeout=INLINE_TIMEOUT_S)
        n = int(res.get("upserted") or 0) if "upserted" in res else None
    except Exception:
        n = None
    if n is None:
        _spawn_sync(user.id)
        line = REPLY_ASYNC
    elif n == 0:
        line = REPLY_EMPTY
    else:
        line = REPLY_OK.format(n=n)
    send_sms(user.phone, line, user_id=user.id, message_type="integration_connected")
    return True


# ─── sync ─────────────────────────────────────────────────────────────────────

def _short_course(course: dict | None, fallback: str | None) -> str:
    if course:
        code = (course.get("course_code") or "").strip()
        if 0 < len(code) <= 20:
            return code.lower()
        name = (course.get("name") or "").strip()
        if name:
            return name[:28].lower()
    return (fallback or "")[:28].lower()


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def _is_done(item: dict) -> bool:
    sub = item.get("submissions")
    if isinstance(sub, dict) and any(sub.get(k) for k in _DONE_KEYS):
        return True
    ov = item.get("planner_override") or {}
    return bool(isinstance(ov, dict) and ov.get("marked_complete"))


def map_item(item: dict, courses: dict[int, dict]) -> dict | None:
    """One planner item → the event-store shape, or None to skip. Done work returns
    {'done': True, 'external_id': …} so the caller can drop a row it had before."""
    ptype = item.get("plannable_type")
    p = item.get("plannable") or {}
    pid = item.get("plannable_id") or p.get("id")
    if not ptype or pid is None:
        return None
    ext = f"{ptype}:{pid}"
    course = courses.get(item.get("course_id"))
    tag = _short_course(course, item.get("context_name"))
    title = (p.get("title") or p.get("name") or "").strip() or "(untitled)"

    if ptype in ("assignment", "quiz", "discussion_topic"):
        if _is_done(item):
            return {"done": True, "external_id": ext}
        due = _parse_ts(p.get("due_at") or p.get("todo_date"))
        if due is None:
            return None                      # undated — nothing to plan around
        label = f"due: {title}" + (f" [{tag}]" if tag else "")
        return {"external_id": ext, "title": label, "start": due, "end": due, "all_day": False}

    if ptype == "calendar_event":
        start = _parse_ts(p.get("start_at"))
        if start is None:
            return None
        end = _parse_ts(p.get("end_at"))
        label = title + (f" [{tag}]" if tag else "")
        return {"external_id": ext, "title": label, "start": start, "end": end,
                "all_day": bool(p.get("all_day"))}

    if ptype == "planner_note":
        if _is_done(item):
            return {"done": True, "external_id": ext}
        when = _parse_ts(p.get("todo_date"))
        if when is None:
            return None
        return {"external_id": ext, "title": f"todo: {title}", "start": when, "end": when,
                "all_day": False}

    return None                              # announcement / wiki_page / assessment_request


def sync_user(user_id: int, *, timeout: float | None = None) -> dict:
    """Pull one user's planner into the event store; supersede their feed events."""
    if not config.CANVAS_ENABLED:
        return {"skipped": "flag off"}
    session = get_session()
    try:
        integ = base.get_integration(session, user_id, SOURCE)
        if integ is None or integ.status not in ("connected", "error"):
            return {"skipped": "not connected"}
        token = crypto.decrypt(integ.access_token)
    finally:
        session.close()
    if not token:
        return {"skipped": "no token"}

    now = _utcnow()
    lo = now - timedelta(days=LOOKBACK_DAYS)
    hi = now + timedelta(days=SYNC_HORIZON_DAYS)
    try:
        courses = {c.get("id"): c for c in list_courses(token, timeout=timeout) if c.get("id") is not None}
        items = planner_items(token, lo, hi, timeout=timeout)
    except InvalidToken:
        base.mark_revoked(user_id, SOURCE)   # feed sync resumes on its own
        return {"error": "revoked"}
    except Exception as e:
        base.note_sync_failure(user_id, SOURCE, e)
        return {"error": str(e)}

    seen: set = set()
    upserted = done = 0
    for item in items:
        m = map_item(item, courses)
        if m is None:
            continue
        if m.get("done"):
            if delete_external_event(user_id, source=SOURCE, external_id=m["external_id"]):
                done += 1
            continue
        start, end = m["start"], m["end"]
        if not (lo <= start < hi):
            continue
        if end is not None and (end - start) > timedelta(hours=MAX_EVENT_HOURS):
            continue
        upsert_external_event(user_id, source=SOURCE, external_id=m["external_id"],
                              title=m["title"], occurred_at=start, ends_at=end,
                              all_day=m["all_day"])
        seen.add(m["external_id"])
        upserted += 1
    pruned = prune_external_events(user_id, source=SOURCE, keep=seen, lo=lo, hi=hi, now=now)
    # One board: the feed's copies of the same assignments go away while the token is live.
    superseded = prune_external_events(user_id, source=FEED_SOURCE, keep=set(), lo=lo, hi=hi, now=now)

    codes = sorted({_short_course(c, None) for c in courses.values()} - {""})
    base.note_sync_success(user_id, SOURCE, now, course_codes=codes)
    logger.info("CANVAS_SYNC user=%s upserted=%s done=%s pruned=%s superseded_feed=%s courses=%s",
                user_id, upserted, done, pruned, superseded, len(codes))
    return {"upserted": upserted, "done": done, "pruned": pruned, "superseded": superseded}


def is_active(user_id: int) -> bool:
    """True while this user has a live token — the feed sync defers to it."""
    if not config.CANVAS_ENABLED:
        return False
    session = get_session()
    try:
        integ = base.get_integration(session, user_id, SOURCE)
        return bool(integ and integ.status in ("connected", "error") and integ.access_token)
    finally:
        session.close()


def sync_all() -> int:
    if not config.CANVAS_ENABLED:
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
            logger.exception("CANVAS_SYNC_USER_FAILED user=%s", uid)
    return n


__all__ = ["SOURCE", "TOKEN_RE", "find_token", "save_token", "handle_inbound_token",
           "map_item", "sync_user", "sync_all", "is_active", "InvalidToken",
           "REPLY_OK", "REPLY_EMPTY", "REPLY_ASYNC", "REPLY_BAD_TOKEN", "REPLY_TRY_LATER", "REDACTED"]
