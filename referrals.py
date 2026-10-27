"""
Referral codes (founder, 2026-10-27): a frat challenge — who can get the most sign-ups.

- Every signed-up user has a code (users.referral_code): their first name + four digits
  ("NAU4821" — founder 2026-10-27: four, harder to guess or impersonate), made the first
  time anything needs it (ensure_code) and for everyone at once from the admin page
  (backfill). A code with fewer digits (the first hour's two-digit ones) is regenerated on
  touch. Only an account has one — there is nothing to hand out
  before sign-up.
- They get it by ASKING the coach ("what's my referral code") → the get_referral_code
  tool (agent_tools), which also returns how many sign-ups they've been credited with.
  During onboarding the code sits in the system prompt for the same ask.
- The sign-up chat on cued.fit asks for a friend's code as its last optional field (site:
  index.html, after the gmail ask) and posts it as `referral_code`. /waitlist hands it to
  credit_signup: a match on another user's code credits that user
  (users.referred_by_user_id); what they typed is kept either way
  (users.referral_code_entered) so a "but i typed it" can be checked. Nothing is asked in
  the text thread.
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

# A code is 2–10 letters then 4–5 digits ("NAU4821"). normalize() forgives case, spaces
# and dashes ("nau 4821", "Nau-4821"). Letters are ASCII only: the name part is
# normalised the same way at generation time.
_CODE_RE = re.compile(r"^([A-Za-z]{2,10})(\d{4,5})$")
_MIN_DIGITS = 4
_FALLBACK_PREFIX = "CUED"
_MAX_PREFIX = 8


def _naive_utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enabled() -> bool:
    return bool(getattr(config, "REFERRALS_ENABLED", True))


def _prefix_from_name(name: str | None) -> str:
    first = (name or "").strip().split()[0] if (name or "").strip() else ""
    ascii_name = unicodedata.normalize("NFKD", first).encode("ascii", "ignore").decode()
    letters = re.sub(r"[^A-Za-z]", "", ascii_name).upper()[:_MAX_PREFIX]
    return letters if len(letters) >= 2 else _FALLBACK_PREFIX


def _candidate(prefix: str, attempt: int, rng: random.Random) -> str:
    digits = rng.randint(1000, 9999) if attempt < 20 else rng.randint(10000, 99999)
    return f"{prefix}{digits}"


def _is_current(code: str | None) -> bool:
    """True for a code in today's shape (>= 4 digits); the first hour's 2-digit codes aren't."""
    m = re.match(r"^[A-Z]+(\d+)$", code or "")
    return bool(m) and len(m.group(1)) >= _MIN_DIGITS


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
        if user.referral_code and _is_current(user.referral_code):
            return user.referral_code
        old = user.referral_code
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
                if user.referral_code and _is_current(user.referral_code):
                    return user.referral_code
                continue
            logger.info("REFERRAL_CODE_SET user=%s code=%s attempt=%s%s", user_id, code, attempt,
                        f" replaced={old}" if old else "")
            return code
        logger.error("REFERRAL_CODE_EXHAUSTED user=%s prefix=%s", user_id, prefix)
        return None
    finally:
        session.close()


def backfill(limit: int = 500) -> int:
    """Give every code-less user a code, and a 4-digit one to anyone still on a 2-digit
    code (idempotent; the admin page calls it on load)."""
    from models import get_session, User
    session = get_session()
    try:
        ids = [r[0] for r in session.query(User.id, User.referral_code).order_by(User.id).all()
               if not _is_current(r[1])][:limit]
    finally:
        session.close()
    done = 0
    for uid in ids:
        if ensure_code(uid):
            done += 1
    if done:
        logger.info("REFERRAL_CODES_BACKFILLED n=%s", done)
    return done


def normalize(raw: str | None) -> str | None:
    """"nau 4821" / "Nau-4821" / " NAU4821 " → "NAU4821"; anything not code-shaped → None."""
    cleaned = re.sub(r"[\s\-_.]", "", (raw or "")).strip()
    m = _CODE_RE.match(cleaned)
    if not m:
        return None
    return f"{m.group(1).upper()}{m.group(2)}"


def lookup(code: str):
    """(user_id, name) for a code, or None."""
    from models import get_session, User
    norm = normalize(code)
    if not norm:
        return None
    session = get_session()
    try:
        row = session.query(User.id, User.name).filter(User.referral_code == norm).first()
        return (row[0], row[1]) if row else None
    finally:
        session.close()


def credit_signup(user_id: int, raw_code: str | None) -> dict:
    """Apply the code a NEW sign-up typed on the site. Keeps what they typed
    (referral_code_entered), credits the referrer when it matches someone else's code.
    Returns {"entered", "credited", "referrer_id", "referrer_name"} — never raises."""
    from models import get_session, User
    out = {"entered": None, "credited": False, "referrer_id": None, "referrer_name": None}
    entered = (raw_code or "").strip()[:32] or None
    if not entered:
        return out
    out["entered"] = entered
    norm = normalize(entered)
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u:
            return out
        u.referral_code_entered = entered
        row = (session.query(User.id, User.name).filter(User.referral_code == norm).first()
               if norm else None)
        if not row or row[0] == user_id:
            session.commit()
            logger.info("REFERRAL_SIGNUP_NO_MATCH user=%s entered=%r norm=%s%s", user_id, entered, norm,
                        " (own code)" if row else "")
            return out
        u.referred_by_user_id = row[0]
        session.commit()
        out.update(credited=True, referrer_id=row[0], referrer_name=row[1])
        logger.info("REFERRAL_CREDITED user=%s referrer=%s code=%s", user_id, row[0], norm)
        return out
    except Exception as e:  # noqa: BLE001 — never block a sign-up on the credit
        logger.warning("REFERRAL_CREDIT_FAILED user=%s err=%s", user_id, e)
        return out
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
