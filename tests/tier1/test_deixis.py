"""
Memory-freshness Fix 2 — de-deixis at capture.

Durable stores must never carry bare relative-time words: a note's text saying
"today" is re-resolved by the model against NOW, not against the note's date —
that's how a Jul 31 "interview 2:15pm today" resurfaced on Aug 3 as "you've got
the interview this afternoon" (live, prod). The code guarantee is
`timefmt.resolve_deixis`: a precision-biased annotator that appends the resolved
absolute local date after each day-level relative term. Annotation, not
replacement — meaning survives a rare mis-resolve — and idempotent, so a second
pass (or a writer echoing an annotated string) can't stack parens.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone


# Fixed clock for the unit cases: Thu Aug 6 2026, 12:00 PDT (19:00 UTC).
_NOW = datetime(2026, 8, 6, 19, 0, tzinfo=timezone.utc)


class _U:
    def __init__(self, tz="America/Los_Angeles"):
        self.id = 1
        self.user_timezone = tz


def _r(text, tz="America/Los_Angeles", now=_NOW):
    from timefmt import resolve_deixis
    return resolve_deixis(text, _U(tz), now=now)


# ── 6a. resolution against a fixed now + user tz ─────────────────────────────

def test_resolves_each_day_level_term():
    assert _r("big orgo midterm tomorrow") == "big orgo midterm tomorrow (Fri Aug 7)"
    assert _r("interview at 2:15pm today") == "interview at 2:15pm today (Thu Aug 6)"
    assert _r("appointment at Li Ka Shing this morning") == \
        "appointment at Li Ka Shing this morning (Thu Aug 6)"
    assert _r("pretty wiped tonight") == "pretty wiped tonight (Thu Aug 6)"
    assert _r("was sick yesterday") == "was sick yesterday (Wed Aug 5)"
    assert _r("barely slept last night") == "barely slept last night (Wed Aug 5)"


def test_part_of_day_binds_to_the_phrase():
    """'tomorrow morning' annotates after the whole phrase, not mid-phrase."""
    assert _r("midterm tomorrow morning — stressed") == \
        "midterm tomorrow morning (Fri Aug 7) — stressed"


def test_dates_resolve_in_the_users_tz_not_utc():
    # 03:30 UTC Aug 7 is still Thu Aug 6 in New York — a UTC-naive resolver says Fri.
    now = datetime(2026, 8, 7, 3, 30, tzinfo=timezone.utc)
    assert _r("quiz today", tz="America/New_York", now=now) == "quiz today (Thu Aug 6)"


def test_no_deixis_no_change():
    for s in ("hit 180g protein", "interview went well on Jul 31", ""):
        assert _r(s) == s


# ── 6b. idempotent, annotation-not-replacement ───────────────────────────────

def test_idempotent_and_preserves_original_words():
    once = _r("midterm tomorrow morning")
    assert _r(once) == once, "re-annotating an annotated string must be a no-op"
    assert "tomorrow" in once, "annotation must not replace the user's words"
    # already-annotated input (e.g. the model resolved it natively) passes through
    assert _r("midterm tomorrow (Fri Aug 7)") == "midterm tomorrow (Fri Aug 7)"


# ── 7. the writers store annotated text ──────────────────────────────────────

_ANNOTATED = re.compile(
    r"tomorrow(?: (?:morning|afternoon|evening|night))? \([A-Z][a-z]{2} [A-Z][a-z]{2} \d{1,2}\)")


def _seed_convo(db, user_id, n=4, minutes_ago=200):
    from datetime import timedelta
    from models import get_session, Message
    s = get_session()
    try:
        base = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=minutes_ago)
        for i in range(n):
            s.add(Message(user_id=user_id, direction="in" if i % 2 == 0 else "out",
                          body=f"line {i}", created_at=base + timedelta(seconds=i)))
        s.commit()
    finally:
        s.close()


def test_episodic_digest_stores_annotated_text(db, anthropic_stub):
    """The digest writer is the worst offender (its old example MODELED the bug)."""
    from tests.factories import make_user
    from models import get_session, EpisodicDigest
    import episodic

    anthropic_stub.reply_with(lambda kw: "Big orgo midterm tomorrow — pretty stressed.")
    user = make_user(db, name="Priya")
    _seed_convo(db, user.id)

    res = episodic.digest_user(user.id)
    assert res["status"] == "wrote", res
    s = get_session()
    try:
        note = s.query(EpisodicDigest).filter(EpisodicDigest.user_id == user.id).one()
    finally:
        s.close()
    assert _ANNOTATED.search(note.text), \
        f"digest stored bare deixis: {note.text!r}"


def test_remember_tool_stores_annotated_text(db):
    from tests.factories import make_user
    from agent_tools import handle_remember
    from models import get_session, User

    user = make_user(db)
    out = handle_remember(user.id, {"action": "add", "category": "schedule",
                                    "text": "has a code interview tomorrow at 2:15pm"})
    assert out.startswith("ok"), out
    s = get_session()
    try:
        entries = (s.get(User, user.id).user_profile_memory or {}).get("schedule") or []
    finally:
        s.close()
    assert entries and _ANNOTATED.search(entries[0]["text"]), \
        f"remember stored bare deixis: {entries}"


def test_legacy_extraction_stores_annotated_text(db, anthropic_stub):
    """The parallel per-turn extractor produced the live 'this afternoon' entries;
    until its Phase-6 parity deletion it must get the same floor."""
    import json
    from tests.factories import make_user
    from models import get_session, User
    from app import extract_and_store_memory

    anthropic_stub.reply_with(lambda kw: json.dumps({"facts": [{
        "action": "add", "category": "schedule",
        "text": "has a code interview tomorrow afternoon",
        "replaces_text": None, "safety_critical": False}]}))
    user = make_user(db)
    extract_and_store_memory(user.id, "got my interview tomorrow afternoon", "good luck")

    s = get_session()
    try:
        entries = (s.get(User, user.id).user_profile_memory or {}).get("schedule") or []
    finally:
        s.close()
    assert entries and _ANNOTATED.search(entries[0]["text"]), \
        f"legacy extraction stored bare deixis: {entries}"
