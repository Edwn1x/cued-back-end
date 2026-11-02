"""
users.which_gym was a column nobody wrote (12/12 live rows empty on 2026-11-02). The RSF
hours fit (reminders / context / briefing window) must apply only to people whose gym IS the
RSF, so this is the one writer: their own words name it → 'rsf' goes on file, once, never
overwriting a value. Founder 2026-11-02: "this only applies if their gym is actually rsf."
"""
from __future__ import annotations

import logging
import re

import config
from models import get_session, User

logger = logging.getLogger("cued.gym_on_file")

_RSF_RE = re.compile(r"\b(?:rsf|rec(?:reational)? sports facility)\b", re.I)
# "not the rsf" / "no rsf" / "rsf is closed so i'm at …" style denials within a few words before.
_DENY_RE = re.compile(r"\b(?:not|no|never|isn'?t|aint|ain'?t|instead of)\s+(?:\w+\s+){0,2}(?:the\s+)?(?:rsf|rec(?:reational)? sports facility)\b", re.I)


def trains_at_rsf(user) -> bool:
    return (getattr(user, "which_gym", None) or "").strip().lower() == "rsf"


def names_the_rsf(text: str) -> bool:
    text = text or ""
    return bool(_RSF_RE.search(text)) and not _DENY_RE.search(text)


def note_gym_from_text(user_id: int, text: str) -> str | None:
    """Their own message names the RSF and no gym is on file → which_gym='rsf'. Returns the
    value written, else None. Never overwrites."""
    if not getattr(config, "GYM_ON_FILE_FROM_TEXT_ENABLED", True) or not names_the_rsf(text):
        return None
    session = get_session()
    try:
        u = session.get(User, user_id)
        if not u or (u.which_gym or "").strip():
            return None
        u.which_gym = "rsf"
        session.commit()
        logger.info("GYM_ON_FILE user=%s which_gym=rsf from=%r", user_id, (text or "")[:80])
        return "rsf"
    finally:
        session.close()
