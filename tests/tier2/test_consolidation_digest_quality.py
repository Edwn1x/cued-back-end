"""
Tier-2 (live) — Phase 5 judged quality. The deterministic guardrails are proven in
tier-1; here the model-dependent parts get read: does the episodic digest capture the
NON-fitness life substance (and NOT re-summarize coaching decisions — the watermark
summarizer's job), and does a real messy profile's human-readable summary read cleanly?
Run: pytest --run-tier2 -s.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_episodic_digest_captures_life_not_coaching(db):
    import episodic
    from tests.factories import make_user
    from models import get_session, Message, EpisodicDigest

    user = make_user(db, name="Priya")
    convo = [
        ("in", "hey did 5x5 squats at 185 today, felt strong"),
        ("out", "love that, 185 for 5x5 is real progress"),
        ("in", "yeah but honestly i'm dead — got a huge orgo midterm tmrw morning and barely slept"),
        ("out", "get some rest then, the work's done"),
        ("in", "ya my roommate moved out too so apt's been chaos this week"),
        ("out", "one thing at a time. crush the midterm."),
    ]
    s = get_session()
    try:
        base = _utcnow_naive() - timedelta(minutes=200)
        for i, (d, b) in enumerate(convo):
            s.add(Message(user_id=user.id, direction=d, body=b,
                         created_at=base + timedelta(seconds=i)))
        s.commit()
    finally:
        s.close()

    res = episodic.digest_user(user.id)
    note = _digest_text(user.id)
    print(f"\n[EPISODIC] status={res['status']} note={note!r}")

    assert res["status"] == "wrote", res
    low = note.lower()
    # captured the life substance (at least one of the non-fitness threads)
    assert ("midterm" in low or "orgo" in low or "roommate" in low or "apartment" in low
            or "apt" in low or "moved" in low), "digest missed the non-fitness life context"
    # did NOT turn into a coaching/fitness log
    assert "5x5" not in low and "185" not in low, \
        "digest re-covered coaching ground (that's the watermark summarizer's job)"
    assert len(note) < 300, "digest should be a short note, not a paragraph"


def _digest_text(user_id):
    from models import get_session, EpisodicDigest
    s = get_session()
    try:
        row = (s.query(EpisodicDigest)
               .filter(EpisodicDigest.user_id == user_id)
               .order_by(EpisodicDigest.id.desc()).first())
        return row.text if row else ""
    finally:
        s.close()
