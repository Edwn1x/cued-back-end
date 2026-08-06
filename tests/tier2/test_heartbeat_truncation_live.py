"""
Tier-2 (live) — heartbeat decide completes under the raised ceiling (spec case 6,
rewrite/heartbeat-truncation).

The Aug 6 prod tick truncated at the old 400-token cap (output=400 exactly,
reason="no message composed") after the memory-freshness slice grew the decision
context. This runs one REAL decide against a comparably rich context — events in
all three lifecycle blocks, momentum, tick history, memory — and asserts the
decision now terminates cleanly: a decision tool was reached (send_text or
stay_silent), never the max_tokens cutoff. Direction-agnostic on purpose: speak
vs silent is calibration (test_heartbeat_proactive.py); this only pins that the
decision ARRIVES.

Run: pytest tests/tier2/test_heartbeat_truncation_live.py --run-tier2 -s
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

pytestmark = pytest.mark.tier2


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_decide_completes_without_truncation_on_rich_context(db, monkeypatch):
    import config, heartbeat
    from models import get_session, Message, Workout
    from events import record_event
    from tests.factories import make_user

    user = make_user(db)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [user.phone])

    now = _utcnow_naive()
    s = get_session()
    try:
        u = s.get(type(user), user.id)
        # profile memory through the REAL writer (apply_facts) so entries carry
        # the full schema (id, uses, …) — hand-built dicts break render_categories
        from memory import apply_facts
        new_profile, _stats = apply_facts(dict(u.user_profile_memory or {}), [
            {"action": "add", "category": "schedule",
             "text": "Tuesdays are class-heavy (CS + orgo back to back)"},
            {"action": "add", "category": "schedule",
             "text": "Coding interview with Stripe on Fri Aug 8 (morning)"},
            {"action": "add", "category": "goals",
             "text": "Wants to hit 3500 cal on training days"},
            {"action": "add", "category": "social",
             "text": "Trains with roommate Danny most evenings"},
        ], user_id=user.id)
        u.user_profile_memory = new_profile
        s.commit()
        # momentum: real workouts this week
        for d in (1, 3, 5):
            s.add(Workout(user_id=user.id, completed=True, workout_type="push",
                          date=now - timedelta(days=d)))
        # conversation history so TIME SINCE / RECENT blocks are populated
        for i, (direction, body) in enumerate([
            ("in", "leg day done, quads are gone"),
            ("out", "that's 3 this week — solid. eat something real tonight"),
            ("in", "got the stripe interview friday morning btw"),
            ("out", "noted — friday morning. we'll keep thursday light"),
        ]):
            s.add(Message(user_id=user.id, direction=direction, body=body,
                          message_type="freeform",
                          created_at=now - timedelta(hours=30 - i)))
        s.commit()
    finally:
        s.close()

    # events across all three lifecycle blocks (the PR #22 context growth)
    record_event(user.id, "scheduled", source="model", raw_text="CS midterm",
                 occurred_at=now - timedelta(hours=20),
                 ends_at=now - timedelta(hours=18))          # recently passed
    record_event(user.id, "scheduled", source="model", raw_text="orgo problem session",
                 occurred_at=now + timedelta(hours=3),
                 ends_at=now + timedelta(hours=4))           # today
    record_event(user.id, "scheduled", source="model", raw_text="Stripe coding interview",
                 occurred_at=now + timedelta(days=2),
                 ends_at=now + timedelta(days=2, hours=1))   # upcoming

    spoke, payload, search = heartbeat.decide(user.id)

    print(f"\nDECIDE: spoke={spoke} stop={search.get('stop')} payload={payload[:200]!r}")

    assert search.get("stop") != "max_tokens", (
        f"decide truncated at HEARTBEAT_DECIDE_MAX_TOKENS={config.HEARTBEAT_DECIDE_MAX_TOKENS} "
        f"— raise the ceiling; payload={payload!r}")
    assert not payload.startswith("truncated:"), payload
    assert payload != "no message composed", (
        "decision ended with neither tool nor text — response-shape anomaly, "
        "see HEARTBEAT_NO_OUTPUT in the log")
