"""
Connecting integrations, proactively (founder, 2026-10-01/02).

Until now the coach only ever OFFERED a connection when the user happened to bring up
their calendar, their watch, or what's due (voice.md "Getting it connected"), and the
only person who has ever connected anything is the founder. Three gaps closed here:

1. THE GOOGLE ACCOUNT, FIRST. The OAuth consent screen is in Testing mode: a Google
   link only works for an account on the Cloud Console test-users list, so before any
   Google link goes out we need to know WHICH account — and the founder has to add it.
   `set_google_account` (coach tool) stores it; the admin console shows "needs
   allowlisting" and a one-click "mark allowlisted"; `send_connect_link` refuses to
   send a Google link until that's done (GOOGLE_OAUTH_TESTING_MODE=false lifts all of
   this once the app is verified).

2. THE RECONNECT NUDGE. In Testing mode every refresh token dies after 7 days (the
   founder's Google Health went `revoked` exactly 7d after connecting). When a row goes
   revoked, one line + a fresh link, once per revoke, inside the heartbeat's guardrails.

3. THE FIRST OFFER. Once onboarded for a day, one proactive offer per provider, a day
   apart, each at most once ever: google calendar (link, or the account question while
   in Testing), then bcourses (students; the paste instructions), then the wearable
   (only if they named one at signup). Declines are learned by the coach normally; the
   offer itself never repeats.

Everything here is code-sent and runs from a scheduler sweep like water_offer.sweep:
only when heartbeat.guardrail_reason would let a proactive text through, one action
per user per sweep. users.connect_offers (JSON {key: iso-ts}) is the once-only ledger;
integrations.meta.revoked_at / reconnect_nudged_at key the nudge.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone, timedelta

import config

logger = logging.getLogger("cued.connect_offers")

MESSAGE_TYPE = "connect_offer"
LINK_MESSAGE_TYPE = "connect_link"
GOOGLE_PROVIDERS = ("gcal", "google_health")
OFFER_GAP_HOURS = 24
MIN_DAYS_ONBOARDED = 1

OFFER_GCAL_LINK = ("want me on ur google calendar? i plan ur workouts around ur week with it. "
                   "tap to connect, or ignore this and i won't bring it up again")
OFFER_GCAL_ASK = ("want me on ur google calendar? i plan ur workouts around ur week with it. "
                  "if yes, which google account is it on")
# "u mentioned ur fitbit" asserted a chat mention that never happened: the device comes
# from the signup form's apps field (live 2026-10-07, user 48). Say where it came from.
OFFER_HEALTH_LINK = ("u put a {device} on ur signup. want me reading it? sleep, steps and heart rate, so i go "
                     "easier on a 5h night. tap to connect")
OFFER_HEALTH_ASK = ("u put a {device} on ur signup. want me reading it? sleep, steps and heart rate, so i go "
                    "easier on a 5h night. if yes, which google account is it on")
OFFER_BCOURSES = ("if u want ur bcourses due dates on my radar: in bcourses go to calendar, then 'calendar feed' "
                  "(bottom right), and paste me that link. i'll track them from there")
RECONNECT_LINE = {"gcal": "ur google calendar disconnected on google's end. tap to reconnect",
                  "google_health": "ur {device} disconnected on google's end. tap to reconnect"}
ALLOWLISTED_LINE = {"gcal": "ur set on my end. tap to connect ur calendar",
                    "google_health": "ur set on my end. tap to connect ur {device}"}

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def device_label(user) -> str:
    tools = (getattr(user, "existing_tools", None) or "").lower()
    if "pixel" in tools:
        return "pixel watch"
    if "fitbit" in tools:
        return "fitbit"
    return "watch"


def has_wearable(user) -> bool:
    tools = (getattr(user, "existing_tools", None) or "").lower()
    return any(k in tools for k in ("fitbit", "pixel", "google_health", "google health"))


def is_student(user) -> bool:
    return "student" in (getattr(user, "occupation", None) or "").lower() or bool(getattr(user, "year", None))


# ─── allowlist state (Testing mode) ──────────────────────────────────────────

def _has_google_row(session, user_id: int) -> bool:
    """Ever connected a Google provider → that account is on the test list already."""
    from models import Integration
    return (session.query(Integration.id)
            .filter(Integration.user_id == user_id, Integration.provider.in_(GOOGLE_PROVIDERS),
                    Integration.status.in_(("connected", "revoked", "pending"))).first()) is not None


def allowlist_state(user, session=None) -> str:
    """'ok' | 'needs_account' | 'needs_allowlist'. Always 'ok' once the consent screen is
    published (GOOGLE_OAUTH_TESTING_MODE=false) or if they've connected Google before."""
    if not config.GOOGLE_OAUTH_TESTING_MODE:
        return "ok"
    if getattr(user, "google_allowlisted_at", None):
        return "ok"
    if getattr(user, "google_email", None):
        # a saved-but-unlisted account outranks "they connected before": a SECOND Google
        # login (school calendar) needs its own test-users entry
        return "needs_allowlist"
    own = session is None
    if own:
        from models import get_session
        session = get_session()
    try:
        if _has_google_row(session, user.id):
            return "ok"
    finally:
        if own:
            session.close()
    return "needs_account"


def context_line(user) -> str | None:
    """One line for the coach's INTEGRATIONS block while in Testing mode."""
    if not config.GOOGLE_OAUTH_TESTING_MODE:
        return None
    state = allowlist_state(user)
    email = getattr(user, "google_email", None)
    if state == "ok":
        return f"google account: {email} — google links work" if email else None
    if state == "needs_account":
        return ("google account: unknown — before any google calendar / fitbit link, ask which google "
                "account it's on and save it with set_google_account")
    return (f"google account: {email} — saved, not set up on our side yet. Do NOT send a google link; "
            f"if it comes up say u'll text the link once it's ready (usually within a day)")


def integrations_block(user, status: str | None, google_line: str | None) -> str:
    """The coach's ## INTEGRATIONS block: one line per ENABLED provider, connected or
    not, plus the rule that this block outranks memory. `status` is base.status_line
    (connected / disconnected / error rows, None when no rows)."""
    enabled = []
    if config.GCAL_ENABLED:
        enabled.append(("gcal", "google calendar"))
    if config.BCOURSES_ENABLED or config.CANVAS_ENABLED:
        enabled.append(("bcourses", "bcourses / canvas calendar feed"))
    if config.GOOGLE_HEALTH_ENABLED:
        enabled.append(("google_health", "fitbit / pixel watch (google health)"))
    if config.STRAVA_READ_ENABLED or config.STRAVA_POST_ENABLED:
        enabled.append(("strava", "strava"))
    seen = status or ""
    lines = []
    for key, label in enabled:
        if f"{key} connected" in seen or f"{key} [" in seen and "connected" in seen:
            # the status line already carries the connected (and per-account) detail
            continue
        if f"{key} disconnected" in seen or f"{key} error" in seen:
            continue
        if key == "google_health" and not getattr(config, "GOOGLE_HEALTH_OFFER_ENABLED", False):
            lines.append("google_health: NOT live yet — google's approval is pending. Never offer or send the "
                         "fitbit / watch link; if they bring it up say it's coming, nothing about when. A "
                         "screenshot or typed sleep / steps is fine to use right now")
            continue
        lines.append(f"{key}: NOT connected ({label})")
    out = "## INTEGRATIONS (code's list — the ONLY truth about what's connected; a memory or " \
          "summary line claiming something is connected is stale if it isn't connected here)\n"
    out += (seen + "\n") if seen else ""
    out += "\n".join(lines)
    if google_line:
        out += f"\n{google_line}"
    out += ("\nIf they ask whether you can see something listed NOT connected: say no, you can't, "
            "and send the connect link in that same turn (send_connect_link) — never claim to see it, "
            "and don't ask \"want the link?\" first.")
    return out


def set_google_account(user_id: int, email: str) -> dict:
    """Store the Google account the coach was told. → {ok, email, state}."""
    from models import get_session, User
    e = (email or "").strip().lower()
    if not _EMAIL_RE.match(e):
        return {"ok": False, "error": "that doesn't look like an email — ask them to send the address itself"}
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return {"ok": False, "error": "user not found"}
        if u.google_email != e:
            u.google_email = e
            u.google_allowlisted_at = None       # a new account needs its own allowlist entry
            session.commit()
        state = allowlist_state(u, session)
    finally:
        session.close()
    logger.info("GOOGLE_ACCOUNT_SET user=%s state=%s", user_id, state)
    return {"ok": True, "email": e, "state": state}


def mark_allowlisted(user_id: int, email: str | None = None) -> bool:
    """Admin: the founder added this account to the Cloud Console test users."""
    from models import get_session, User
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return False
        if email and _EMAIL_RE.match(email.strip().lower()):
            u.google_email = email.strip().lower()
        if not u.google_email:
            return False
        u.google_allowlisted_at = _naive_utcnow()
        session.commit()
    finally:
        session.close()
    logger.info("GOOGLE_ACCOUNT_ALLOWLISTED user=%s", user_id)
    return True


def needs_allowlisting(session) -> list:
    """Users whose Google account is saved but not yet on the test list (admin list)."""
    from models import User
    if not config.GOOGLE_OAUTH_TESTING_MODE:
        return []
    return (session.query(User)
            .filter(User.google_email.isnot(None), User.google_allowlisted_at.is_(None))
            .order_by(User.id).all())


# ─── links ───────────────────────────────────────────────────────────────────

def mint_link(user_id: int, provider: str) -> str:
    """A fresh single-use connect link (same plumbing as the coach's send_connect_link)."""
    from integrations import base
    return base.mint_connect_link(user_id, provider)


def send_link(user_id: int, provider: str, line: str | None, *, source: str) -> None:
    from models import get_session, User
    from sms import send_sms
    session = get_session()
    try:
        u = session.get(User, user_id)
        phone = u.phone if u else None
    finally:
        session.close()
    if not phone:
        return
    link = mint_link(user_id, provider)
    if line:
        send_sms(phone, line, user_id=user_id, message_type=MESSAGE_TYPE)
    send_sms(phone, link, user_id=user_id, message_type=LINK_MESSAGE_TYPE)
    logger.info("CONNECT_LINK_SENT user=%s provider=%s source=%s", user_id, provider, source)


# ─── the sweep ───────────────────────────────────────────────────────────────

def _offers(user) -> dict:
    return dict(getattr(user, "connect_offers", None) or {})


def _mark(session, user, key: str, now: datetime) -> None:
    from sqlalchemy.orm.attributes import flag_modified
    offers = _offers(user)
    offers[key] = now.isoformat()
    user.connect_offers = offers
    flag_modified(user, "connect_offers")


def _last_offer_at(user) -> datetime | None:
    stamps = [_parse_iso(v) for k, v in _offers(user).items() if not k.endswith("_link")]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


def _row(session, user_id: int, provider: str):
    from models import Integration
    return (session.query(Integration)
            .filter(Integration.user_id == user_id, Integration.provider == provider).first())


def _eligible(user) -> bool:
    if not user.active or (user.onboarding_step or 0) < 3:
        return False
    if (getattr(user, "waitlist_status", None) or "") == "pending":
        return False
    if config.STOP_OPTOUT_ENABLED and getattr(user, "opted_out", False):
        return False
    return True


def _action_for(session, user, now: datetime, *, min_gap: timedelta | None = None) -> tuple[str, str, str | None] | None:
    """→ (kind, provider, text) for the ONE thing to send this user now, or None.
    kind: 'reconnect' | 'allowlisted' | 'offer_link' | 'offer_ask' | 'offer_text'."""
    # 1. a Google connection died → one nudge per revoke (every account's row)
    if config.RECONNECT_NUDGE_ENABLED:
        from integrations.base import rows_for
        for provider in GOOGLE_PROVIDERS:
            for r in rows_for(session, user.id, provider):
                if r.status != "revoked":
                    continue
                meta = r.meta or {}
                revoked_at = _parse_iso(meta.get("revoked_at")) or r.updated_at
                nudged_at = _parse_iso(meta.get("reconnect_nudged_at"))
                if revoked_at and (nudged_at is None or nudged_at < revoked_at):
                    line = RECONNECT_LINE[provider].format(device=device_label(user))
                    if r.account and r.external_id:
                        line = line.replace("ur google calendar", f"ur google calendar ({r.external_id})")
                    return ("reconnect", provider, line)

    offers = _offers(user)
    state = allowlist_state(user, session)

    # 2. they gave an account, the founder allowlisted it → the link they were promised
    if state == "ok" and getattr(user, "google_allowlisted_at", None):
        for provider in GOOGLE_PROVIDERS:
            if provider in offers and f"{provider}_link" not in offers:
                r = _row(session, user.id, provider)
                if r is None or r.status not in ("connected", "pending"):
                    return ("allowlisted", provider, ALLOWLISTED_LINE[provider].format(device=device_label(user)))

    # 3. the first offers — one per provider, once ever. A day in and a day apart, EXCEPT
    # inside the setup window right after onboarding (setup_sequence.py, founder
    # 2026-10-09): there they are the next steps of setup, paced by the sequence.
    if not config.CONNECT_OFFER_ENABLED:
        return None
    if not _first_offer_gates_ok(user, now, min_gap=min_gap):
        return None
    cands = first_offer_candidates(session, user, offers=offers, state=state)
    return cands[0] if cands else None


def in_setup_window(user, now: datetime | None = None) -> bool:
    """Inside SETUP_WINDOW_HOURS of onboarding completion (and the sequence is on)."""
    if not getattr(config, "SETUP_SEQUENCE_ENABLED", True):
        return False
    done = getattr(user, "onboarding_completed_at", None)
    if not done:
        return False
    now = now or _naive_utcnow()
    return now - done < timedelta(hours=getattr(config, "SETUP_WINDOW_HOURS", 48))


def _first_offer_gates_ok(user, now: datetime, *, min_gap: timedelta | None = None) -> bool:
    window = in_setup_window(user, now)
    since = getattr(user, "activated_at", None) or user.created_at
    if not window and since and now - since < timedelta(days=MIN_DAYS_ONBOARDED):
        return False
    last = _last_offer_at(user)
    if last:
        gap = (min_gap if min_gap is not None else
               (timedelta(minutes=getattr(config, "SETUP_STEP_QUIET_MINUTES", 20)) if window
                else timedelta(hours=OFFER_GAP_HOURS)))
        if now - last < gap:
            return False
    return True


def first_offer_candidates(session, user, *, offers: dict | None = None, state: str | None = None) -> list:
    """Every first offer still owed to this user, in order (gcal → bcourses → wearable),
    ignoring the time gates. [] when nothing is left — the setup sequence's cue that
    the card can go."""
    if not config.CONNECT_OFFER_ENABLED:
        return []
    offers = _offers(user) if offers is None else offers
    state = allowlist_state(user, session) if state is None else state
    out = []

    def google_offer(provider, link_text, ask_text):
        if provider in offers or _row(session, user.id, provider) is not None:
            return None
        if state == "ok":
            return ("offer_link", provider, link_text)
        if state == "needs_account":
            return ("offer_ask", provider, ask_text)
        return None   # needs_allowlist: the founder's move; the follow-through sends the link

    if config.GCAL_ENABLED:
        a = google_offer("gcal", OFFER_GCAL_LINK, OFFER_GCAL_ASK)
        if a:
            out.append(a)
    if config.BCOURSES_ENABLED and is_student(user) and "bcourses" not in offers \
            and _row(session, user.id, "bcourses") is None:
        out.append(("offer_text", "bcourses", OFFER_BCOURSES))
    if config.GOOGLE_HEALTH_ENABLED and getattr(config, "GOOGLE_HEALTH_OFFER_ENABLED", False) and has_wearable(user):
        d = device_label(user)
        a = google_offer("google_health", OFFER_HEALTH_LINK.format(device=d), OFFER_HEALTH_ASK.format(device=d))
        if a:
            out.append(a)
    return out


def _commit_action(session, u, action, now: datetime) -> None:
    """Mark the once-only ledger BEFORE sending so a send-side retry can't double-send."""
    kind, provider, _text = action
    if kind == "reconnect":
        from integrations.base import rows_for
        for r in rows_for(session, u.id, provider):
            if r.status != "revoked":
                continue
            meta = dict(r.meta or {})
            if _parse_iso(meta.get("reconnect_nudged_at")) and \
                    _parse_iso(meta.get("reconnect_nudged_at")) >= (_parse_iso(meta.get("revoked_at")) or r.updated_at):
                continue
            meta["reconnect_nudged_at"] = now.isoformat()
            r.meta = meta
            break
        _mark(session, u, f"{provider}_reconnect", now)   # counts toward the one-a-day gap
    elif kind == "allowlisted":
        _mark(session, u, f"{provider}_link", now)
    else:
        _mark(session, u, provider, now)
    session.commit()


def _send_action(uid: int, kind: str, provider: str, text: str | None, phone: str) -> None:
    from sms import send_sms
    if kind in ("reconnect", "allowlisted", "offer_link"):
        send_link(uid, provider, text, source=kind)
    else:
        send_sms(phone, text, user_id=uid, message_type=MESSAGE_TYPE)
        logger.info("CONNECT_OFFER_SENT user=%s provider=%s kind=%s", uid, provider, kind)


def offer_now(user_id: int, now: datetime | None = None, *, min_gap: timedelta | None = None) -> str | None:
    """The setup sequence's call: send this user's ONE next action right now (no
    heartbeat guardrails — they just texted). Returns the provider sent, else None."""
    now = now or _naive_utcnow()
    from models import get_session, User
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u or not _eligible(u):
            return None
        action = _action_for(session, u, now, min_gap=min_gap)
        if not action:
            return None
        _commit_action(session, u, action, now)
        kind, provider, text = action
        phone = u.phone
    finally:
        session.close()
    try:
        _send_action(user_id, kind, provider, text, phone)
    except Exception as e:  # noqa: BLE001
        logger.warning("CONNECT_OFFER_FAILED user=%s provider=%s kind=%s err=%s", user_id, provider, kind, e)
        return None
    return provider


def sweep(now: datetime | None = None) -> int:
    """One action per eligible user per run, only when the heartbeat's guardrails allow a
    proactive text (quiet hours, active conversation, budget…). Scheduler: every 10 min."""
    if not (config.CONNECT_OFFER_ENABLED or config.RECONNECT_NUDGE_ENABLED):
        return 0
    if not (config.GCAL_ENABLED or config.GOOGLE_HEALTH_ENABLED or config.BCOURSES_ENABLED):
        return 0
    now = (now or _naive_utcnow())
    if now.tzinfo is not None:
        now = now.astimezone(timezone.utc).replace(tzinfo=None)
    from models import get_session, User, Integration
    from heartbeat import guardrail_reason
    from sms import send_sms
    session = get_session()
    todo: list[tuple[int, str, str, str | None, str | None]] = []
    try:
        users = session.query(User).filter(User.active.is_(True), User.onboarding_step >= 3).all()
        for u in users:
            if not _eligible(u) or guardrail_reason(u, session, now=now):
                continue
            action = _action_for(session, u, now)
            if not action:
                continue
            kind, provider, text = action
            if kind.startswith("offer"):
                # First offers inside the setup window are setup_sequence's to send, one
                # step per tick in order (live 2026-10-10: this sweep and water's fired in
                # the same tick). Reconnect / allowlisted lines still go from here.
                from setup_sequence import owns as _setup_owns
                if _setup_owns(u, now):
                    continue
            _commit_action(session, u, action, now)
            todo.append((u.id, kind, provider, text, u.phone))
    finally:
        session.close()
    sent = 0
    for uid, kind, provider, text, phone in todo:
        try:
            _send_action(uid, kind, provider, text, phone)
            sent += 1
        except Exception as e:  # noqa: BLE001 — one bad user must not stop the sweep
            logger.warning("CONNECT_OFFER_FAILED user=%s provider=%s kind=%s err=%s", uid, provider, kind, e)
    return sent
