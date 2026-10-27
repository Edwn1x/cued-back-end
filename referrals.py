"""
Referral codes (founder, 2026-10-27): a frat challenge — who can get the most sign-ups.

- Every signed-up user has a code (users.referral_code): their first name + two digits
  ("NAU42"), made the first time anything needs it (ensure_code) and for everyone at once
  from the admin page (backfill). Only an account has one — there is nothing to hand out
  before sign-up.
- They get it by ASKING the coach ("what's my referral code") → the get_referral_code
  tool (agent_tools), which also returns how many sign-ups they've been credited with.
  During onboarding the code sits in the system prompt for the same ask.
- The LAST onboarding question is code-authored (ASK_TEXT, onboarding_agent): did a friend
  send them, and if so whose code. The next inbound is parsed here (resolve_reply): a token
  that matches another user's code credits that user (users.referred_by_user_id); anything
  else means no credit. Either way it is asked once (users.referral_asked_at /
  referral_resolved_at) and never again.
- /admin/referrals (admin_system) is the leaderboard.
"""

from __future__ import annotations

import logging
import random
import re
import unicodedata
from datetime import datetime, timezone

import config

logger = logging.getLogger("cued.referrals")

ASK_TEXT = "last thing: did a friend send u here? if so drop their referral code, if not just say no"
EXPLAIN_TEXT = ("it's a code a friend who's already on cued gets from me, so they get credit for u "
                "joining. got one? if not just say no")
CREDITED_TEXT = "bet, {name} gets the credit"
OWN_CODE_TEXT = "that's ur own code lol, no credit for that one"
UNKNOWN_CODE_TEXT = "hm, that code doesn't match anyone, no stress"

# A code is 2–10 letters then 2–3 digits. Typed as one token ("NAU42", "Nau-42") it is
# unmistakably a code; with a space ("nau 42") it only counts when it matches a real one
# ("i bench 135 for 5" must never read as a code). Letters are ASCII only: the name part
# is normalised the same way at generation time.
_CODE_RE = re.compile(r"\b([A-Za-z]{2,10})-?(\d{2,3})\b")
_SPACED_CODE_RE = re.compile(r"\b([A-Za-z]{2,10})\s(\d{2,3})\b")
_FALLBACK_PREFIX = "CUED"
_MAX_PREFIX = 8


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enabled() -> bool:
    return bool(getattr(config, "REFERRALS_ENABLED", True))


def ask_enabled() -> bool:
    """The onboarding ask specifically (the code + tool stay on when this is off)."""
    return enabled() and bool(getattr(config, "REFERRAL_ASK_ENABLED", True))


def _prefix_from_name(name: str | None) -> str:
    first = (name or "").strip().split()[0] if (name or "").strip() else ""
    ascii_name = unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode()
    letters = re.sub(r"[^A-Za-z]", "", ascii_name).upper()[:_MAX_PREFIX]
    return letters if len(letters) >= 2 else _FALLBACK_PREFIX


def _candidate(prefix: str, attempt: int, rng: random.Random) -> str:
    digits = rng.randint(10, 99) if attempt < 10 else rng.randint(100, 999)
    return f"{prefix}{digits}"


def ensure_code(user_id: int, *, rng: random.Random | None = None) -> str | None:
    """The user's referral code, generating (and committing) it if they don't have one.
    None for an unknown user."""
    from models import get_session, User
    from sqlalchemy.exc import IntegrityError
    rng = rng or random
    session = get_session()
    try:
        user = session.get(User, user_id)
        if not user:
            return None
        if user.referral_code:
            return user.referral_code
        prefix = _prefix_from_name(user.name)
        for attempt in range(40):
            code = _candidate(prefix, attempt, rng)
            taken = session.query(User.id).filter(User.referral_code == code).first()
            if taken:
                continue
            user.referral_code = code
            try:
                session.commit()
            except IntegrityError:          # lost a race on the unique index — pick again
                session.rollback()
                user = session.get(User, user_id)
                if user.referral_code:
                    return user.referral_code
                continue
            logger.info("REFERRAL_CODE_SET user=%s code=%s attempt=%s", user_id, code, attempt)
            return code
        logger.error("REFERRAL_CODE_EXHAUSTED user=%s prefix=%s", user_id, prefix)
        return None
    finally:
        session.close()


def backfill(limit: int = 500) -> int:
    """Give every code-less user a code (idempotent; the admin page calls it on load)."""
    from models import get_session, User
    session = get_session()
    try:
        ids = [r[0] for r in session.query(User.id).filter(User.referral_code.is_(None))
               .order_by(User.id).limit(limit).all()]
    finally:
        session.close()
    done = 0
    for uid in ids:
        if ensure_code(uid):
            done += 1
    if done:
        logger.info("REFERRAL_CODES_BACKFILLED n=%s", done)
    return done


def find_code_token(text: str) -> str | None:
    """The first one-token thing in the text shaped like a code, upper-cased — or None."""
    m = _CODE_RE.search(text or "")
    if not m:
        return None
    return f"{m.group(1).upper()}{m.group(2)}"


def candidate_tokens(text: str) -> list[str]:
    """Every code-shaped thing in the text, one-token forms first, then spaced ones."""
    out = []
    for rx in (_CODE_RE, _SPACED_CODE_RE):
        for m in rx.finditer(text or ""):
            tok = f"{m.group(1).upper()}{m.group(2)}"
            if tok not in out:
                out.append(tok)
    return out


def lookup(code: str):
    """(user_id, name) for a code, or None."""
    from models import get_session, User
    if not code:
        return None
    session = get_session()
    try:
        row = (session.query(User.id, User.name)
               .filter(User.referral_code == code.upper()).first())
        return (row[0], row[1]) if row else None
    finally:
        session.close()


def pending(user) -> bool:
    """The ask went out and nothing has answered it yet."""
    return bool(getattr(user, "referral_asked_at", None)) and not getattr(user, "referral_resolved_at", None)


def mark_asked(user_id: int) -> None:
    from models import get_session, User
    session = get_session()
    try:
        u = session.get(User, user_id)
        if u and not u.referral_asked_at:
            u.referral_asked_at = _naive_utcnow()
            session.commit()
            logger.info("REFERRAL_ASKED user=%s", user_id)
    finally:
        session.close()


def resolve_reply(user_id: int, text: str) -> str | None:
    """Settle the pending ask from this inbound. Credits the referrer when the text
    carries another user's code; stamps referral_resolved_at either way (asked once).
    Returns the one-line ack to send, or None when there's nothing to say (a 'no')."""
    from models import get_session, User
    strict = find_code_token(text)
    candidates = candidate_tokens(text)
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return None
        u.referral_resolved_at = _naive_utcnow()
        if not candidates:
            session.commit()
            logger.info("REFERRAL_RESOLVED user=%s credited=none body=%r", user_id, (text or "")[:40])
            return None
        row = token = None
        for token in candidates:
            row = session.query(User.id, User.name).filter(User.referral_code == token).first()
            if row:
                break
        if not row:
            session.commit()
            if not strict:          # only a spaced near-miss ("bench 135") — not a code at all
                logger.info("REFERRAL_RESOLVED user=%s credited=none body=%r", user_id, (text or "")[:40])
                return None
            logger.info("REFERRAL_RESOLVED user=%s credited=none unknown_code=%s", user_id, strict)
            return UNKNOWN_CODE_TEXT
        if row[0] == user_id:
            session.commit()
            logger.info("REFERRAL_RESOLVED user=%s credited=none own_code=%s", user_id, token)
            return OWN_CODE_TEXT
        u.referred_by_user_id = row[0]
        session.commit()
        logger.info("REFERRAL_CREDITED user=%s referrer=%s code=%s", user_id, row[0], token)
        return CREDITED_TEXT.format(name=row[1])
    finally:
        session.close()


def stats(user_id: int) -> dict:
    """{'code', 'signups', 'completed', 'names'} for the coach tool and the admin page."""
    from models import get_session, User
    code = ensure_code(user_id)
    session = get_session()
    try:
        rows = (session.query(User.name, User.onboarding_step)
                .filter(User.referred_by_user_id == user_id, User.archived_at.is_(None))
                .order_by(User.id).all())
    finally:
        session.close()
    return {
        "code": code,
        "signups": len(rows),
        "completed": sum(1 for _n, step in rows if (step or 0) >= 3),
        "names": [n for n, _s in rows],
    }


def leaderboard(session) -> list[dict]:
    """Every user with at least one credited sign-up, most first; ties by earliest id."""
    from models import User
    referred = (session.query(User)
                .filter(User.referred_by_user_id.isnot(None), User.archived_at.is_(None))
                .order_by(User.id).all())
    by_ref: dict[int, list] = {}
    for u in referred:
        by_ref.setdefault(u.referred_by_user_id, []).append(u)
    if not by_ref:
        return []
    referrers = {u.id: u for u in session.query(User).filter(User.id.in_(list(by_ref))).all()}
    out = []
    for rid, kids in by_ref.items():
        r = referrers.get(rid)
        out.append({
            "user_id": rid,
            "name": (r.name if r else f"user {rid}"),
            "code": (r.referral_code if r else None) or "—",
            "signups": len(kids),
            "completed": sum(1 for k in kids if (k.onboarding_step or 0) >= 3),
            "referred": [{"id": k.id, "name": k.name, "joined": k.created_at,
                          "completed": (k.onboarding_step or 0) >= 3} for k in kids],
        })
    out.sort(key=lambda r: (-r["signups"], r["user_id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out
