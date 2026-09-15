"""
Waitwell client (series §2.6). See NOTES.md: the site sits behind a Cloudflare
challenge, so `join` raises QueueUnavailable by construction today. The contract
and the ticket ledger are real so a transport swap makes D2 work unchanged.

Polite by construction: one join per user per day, status polled no faster than
the page (60s), a real User-Agent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import requests

import config
from models import get_session, QueueTicket

logger = logging.getLogger("cued.waitwell")

BASE = "https://417804.waitwell.us"
JOIN_URL = f"{BASE}/join/48"
PUBLIC_URL = f"{BASE}/"


class QueueUnavailable(RuntimeError):
    """The queue can't be joined server-side (challenge, non-2xx, changed shape)."""


@dataclass
class Ticket:
    ticket_id: str
    position: int | None
    est_wait_min: int | None
    joined_at: datetime


_alerted_on = {"date": None}


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _alert(reason: str):
    """Log at ERROR once per day so a form change is noticed."""
    today = _utcnow().date()
    if _alerted_on["date"] != today:
        _alerted_on["date"] = today
        logger.error("QUEUE_UNAVAILABLE_ALERT reason=%s — D1 fallback in effect", reason)
    else:
        logger.info("QUEUE_UNAVAILABLE reason=%s", reason)


def _headers():
    return {"User-Agent": f"Cued/1.0 (contact: {config.RSF_CONTACT_EMAIL})", "Accept": "application/json, text/html"}


def transport_post(url: str, data: dict) -> requests.Response:
    """The one network seam (monkeypatched in tests; swapped when a real transport exists)."""
    return requests.post(url, data=data, headers=_headers(), timeout=config.RSF_TIMEOUT_S, allow_redirects=False)


def transport_get(url: str) -> requests.Response:
    return requests.get(url, headers=_headers(), timeout=config.RSF_TIMEOUT_S, allow_redirects=False)


def _is_challenge(resp) -> bool:
    return resp.status_code == 403 and ("challenge" in (resp.headers.get("cf-mitigated") or "").lower()
                                        or "challenges.cloudflare.com" in (getattr(resp, "text", "") or ""))


def parse_join_response(resp) -> Ticket:
    """The shape we accept: JSON {ticket_id|id, position, est_wait_min|estimated_wait}. Anything
    else → QueueUnavailable (a changed form is a signal, not a guess)."""
    if _is_challenge(resp):
        raise QueueUnavailable("cloudflare_challenge")
    if resp.status_code >= 300:
        raise QueueUnavailable(f"http_{resp.status_code}")
    try:
        d = resp.json()
    except Exception:
        raise QueueUnavailable("non_json")
    tid = d.get("ticket_id") or d.get("id")
    if not tid:
        raise QueueUnavailable("changed_shape")
    pos = d.get("position")
    wait = d.get("est_wait_min", d.get("estimated_wait"))
    try:
        pos = int(pos) if pos is not None else None
        wait = int(wait) if wait is not None else None
    except (TypeError, ValueError):
        raise QueueUnavailable("changed_shape")
    return Ticket(str(tid), pos, wait, _utcnow())


def open_ticket(user_id: int) -> QueueTicket | None:
    session = get_session()
    try:
        return (session.query(QueueTicket).filter(QueueTicket.user_id == user_id, QueueTicket.status == "open")
                .order_by(QueueTicket.id.desc()).first())
    finally:
        session.close()


def joined_today(user_id: int) -> bool:
    since = _utcnow() - timedelta(hours=20)
    session = get_session()
    try:
        return session.query(QueueTicket.id).filter(QueueTicket.user_id == user_id, QueueTicket.joined_at >= since).first() is not None
    finally:
        session.close()


def join(user_id: int, name: str, phone: str) -> Ticket:
    """Idempotent: an open ticket is returned as-is; one join per user per day."""
    existing = open_ticket(user_id)
    if existing:
        return Ticket(existing.ticket_id, None, existing.est_wait_min, existing.joined_at)
    if joined_today(user_id):
        raise QueueUnavailable("already_joined_today")
    if not config.RSF_QUEUE_ENABLED:
        raise QueueUnavailable("queue_disabled")
    try:
        resp = transport_post(JOIN_URL, {"name": name, "phone": phone})
    except Exception as e:  # noqa: BLE001
        _alert(f"transport:{e.__class__.__name__}")
        raise QueueUnavailable("transport_error")
    try:
        t = parse_join_response(resp)
    except QueueUnavailable as e:
        _alert(str(e))
        raise
    session = get_session()
    try:
        session.add(QueueTicket(user_id=user_id, ticket_id=t.ticket_id, joined_at=t.joined_at,
                                est_wait_min=t.est_wait_min, status="open"))
        session.commit()
    finally:
        session.close()
    logger.info("QUEUE_JOINED user=%s ticket=%s wait=%s", user_id, t.ticket_id, t.est_wait_min)
    return t


def status(ticket_id: str) -> dict:
    """{position, est_wait_min, summoned}. Raises QueueUnavailable on anything unexpected."""
    try:
        resp = transport_get(f"{BASE}/api/tickets/{ticket_id}")
    except Exception:  # noqa: BLE001
        raise QueueUnavailable("transport_error")
    if _is_challenge(resp) or resp.status_code >= 300:
        raise QueueUnavailable("status_unavailable")
    try:
        d = resp.json()
        return {"position": d.get("position"), "est_wait_min": d.get("est_wait_min", d.get("estimated_wait")),
                "summoned": bool(d.get("summoned") or d.get("status") == "summoned")}
    except Exception:
        raise QueueUnavailable("changed_shape")


def leave(ticket_id: str) -> bool:
    """No leave endpoint is documented; mark ours left and stop polling (noted in NOTES.md)."""
    session = get_session()
    try:
        t = session.query(QueueTicket).filter(QueueTicket.ticket_id == ticket_id, QueueTicket.status == "open").first()
        if not t:
            return False
        t.status, t.left_at = "left", _utcnow()
        session.commit()
        return True
    finally:
        session.close()


def mark_summoned(ticket_id: str) -> None:
    session = get_session()
    try:
        t = session.query(QueueTicket).filter(QueueTicket.ticket_id == ticket_id, QueueTicket.status == "open").first()
        if t:
            t.status, t.summoned_at = "summoned", _utcnow()
            session.commit()
    finally:
        session.close()
