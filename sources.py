"""
sources — WHERE their stuff is posted, on file (the save_source tool + the SOURCES block).
=========================================================================================

The durable fact behind a menu photo is not this week's dishes; it's that the house's
food comes from Grizzly Chefs and the menu lives at grizzlychefs.com/weekly-menu. Before
this the coach read the copy and lost the source (live 2026-11-02, user 77: nothing in
his record named the provider; the founder had to supply the link by hand). A syllabus,
a course site, a club calendar, a team schedule are the same shape.

Shape (users.sources, JSON list, newest last, capped):
  {"id": int, "name": "Grizzly Chefs weekly menu", "url": "https://…", "purpose": "house menu",
   "recheck": "weekly"|"daily"|"none", "added_at": iso, "last_checked_at": iso|None,
   "task_id": int|None}
One row per url (netloc+path); re-saving the same url updates name/purpose/recheck in
place. `task_id` points at the routine (agent_tasks kind "routine") that re-checks it.

Division of labor: code owns dedupe, caps, the url policy (webfetch.check_url — a source
the coach can't open isn't a source), and the routine's clock; the model owns naming
what a thing is and deciding it will recur.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import config

logger = logging.getLogger("cued.sources")

RECHECKS = ("none", "daily", "weekly")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _url_key(url: str) -> tuple:
    try:
        p = urlsplit(url)
        return (p.netloc.lower(), p.path.rstrip("/") or "/")
    except ValueError:
        return ("", url)


def _clean(text: str, n: int) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()[:n]


def list_sources(saved) -> list[dict]:
    items = saved if isinstance(saved, list) else []
    return [dict(s) for s in items if isinstance(s, dict) and s.get("url")]


def apply_source(saved, *, name: str, url: str, purpose: str = "", recheck: str = "none") -> tuple[list, dict]:
    """Pure: add or update the row for `url`. Returns (new_list, the_row)."""
    items = list_sources(saved)
    recheck = (recheck or "none").strip().lower()
    if recheck not in RECHECKS:
        recheck = "none"
    key = _url_key(url)
    now = _utcnow().isoformat()
    for row in items:
        if _url_key(row["url"]) == key:
            row["name"] = _clean(name, 80) or row.get("name") or "source"
            row["url"] = url.strip()
            if purpose:
                row["purpose"] = _clean(purpose, 80)
            row["recheck"] = recheck
            row["updated_at"] = now
            return items, row
    next_id = max([int(r.get("id") or 0) for r in items] + [0]) + 1
    row = {"id": next_id, "name": _clean(name, 80) or "source", "url": url.strip(),
           "purpose": _clean(purpose, 80), "recheck": recheck, "added_at": now,
           "last_checked_at": None, "task_id": None}
    items.append(row)
    cap = int(config.SOURCES_MAX_PER_USER)
    if len(items) > cap:
        items = items[-cap:]
    return items, row


def save_source_for_user(user_id: int, *, name: str, url: str, purpose: str = "",
                         recheck: str = "none") -> dict:
    """Validate the url through webfetch's policy (public http(s) only — the same door the
    coach reads with), persist the row, and create / cancel its routine to match `recheck`.
    Returns {"ok": True, "row": …, "routine": {...}|None} or {"error": str}."""
    import webfetch
    from sqlalchemy.orm.attributes import flag_modified
    from models import get_session, User

    name = _clean(name, 80)
    if not name:
        return {"error": "name is required (what this is, e.g. 'Grizzly Chefs weekly menu')"}
    ok, norm, why = webfetch.check_url(url or "")
    if not ok:
        return {"error": f"that url can't be a source ({why})"}

    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return {"error": "user not found"}
        items, row = apply_source(user.sources, name=name, url=norm, purpose=purpose, recheck=recheck)
        user.sources = items
        flag_modified(user, "sources")
        session.commit()
    finally:
        session.close()

    routine = None
    if config.ROUTINES_ENABLED and config.TASKS_ENABLED:
        from agent_tasks import create_routine, cancel_task
        if row["recheck"] in ("daily", "weekly"):
            r = create_routine(user_id, source=row)
            if "error" in r:
                logger.warning("SOURCE_ROUTINE_FAILED user=%s url=%s err=%s", user_id, norm, r["error"])
            else:
                routine = r
                _set_task_id(user_id, row["id"], r["id"])
                row["task_id"] = r["id"]
        elif row.get("task_id"):
            cancel_task(user_id, row["task_id"])
            _set_task_id(user_id, row["id"], None)
            row["task_id"] = None
    logger.info("SOURCE_SAVED user=%s id=%s name=%r url=%s recheck=%s routine=%s",
                user_id, row["id"], row["name"], norm, row["recheck"], (routine or {}).get("id"))
    return {"ok": True, "row": row, "routine": routine}


def _set_task_id(user_id: int, source_id: int, task_id) -> None:
    from sqlalchemy.orm.attributes import flag_modified
    from models import get_session, User
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return
        items = list_sources(user.sources)
        for row in items:
            if row.get("id") == source_id:
                row["task_id"] = task_id
        user.sources = items
        flag_modified(user, "sources")
        session.commit()
    finally:
        session.close()


def mark_checked(user_id: int, url: str) -> None:
    from sqlalchemy.orm.attributes import flag_modified
    from models import get_session, User
    key = _url_key(url)
    session = get_session()
    try:
        user = session.query(User).filter(User.id == user_id).with_for_update().first()
        if not user:
            return
        items = list_sources(user.sources)
        hit = False
        for row in items:
            if _url_key(row["url"]) == key:
                row["last_checked_at"] = _utcnow().isoformat()
                hit = True
        if hit:
            user.sources = items
            flag_modified(user, "sources")
            session.commit()
    finally:
        session.close()


def source_for_url(saved, url: str) -> dict | None:
    key = _url_key(url)
    for row in list_sources(saved):
        if _url_key(row["url"]) == key:
            return row
    return None


def render_sources_block(saved) -> str:
    items = list_sources(saved)
    if not items:
        return ""
    lines = ["## SOURCES ON FILE (where their stuff is posted — open with fetch_page when they ask "
             "about it; a routine re-checks the ones marked)"]
    for r in items:
        what = f" — {r['purpose']}" if r.get("purpose") else ""
        chk = ""
        if r.get("recheck") in ("daily", "weekly"):
            chk = f" (code checks it {r['recheck']}"
            if r.get("task_id"):
                chk += f", task {r['task_id']}"
            chk += ")"
        lines.append(f"- {r['name']}{what}: {r['url']}{chk}")
    return "\n".join(lines)
