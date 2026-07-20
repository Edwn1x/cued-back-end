"""
Phase 2 — the flag-fallback invariant (Rule 3 #5), the FIRST flag in front of
every inbound message. When the single-agent loop fails at runtime, the webhook
falls back to the legacy pipeline and logs loudly — the user never sees a gap.
Built as a real fallback, verified here, not a hopeful try/except.
"""

from __future__ import annotations

import logging


def test_loop_failure_falls_back_to_legacy_no_gap(db, driver, monkeypatch, caplog):
    """Flag ON + the loop raises → the legacy path still answers, and a loud
    error is logged. The user must not see a gap."""
    import app
    import config
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)

    def boom(*a, **k):
        raise RuntimeError("simulated agent-loop failure")
    monkeypatch.setattr(app, "run_agent_loop", boom)

    user = make_user(db)
    with caplog.at_level(logging.ERROR):
        replies = driver.send(user, "what should i eat for lunch?")

    # (a) a reply is still produced — no user-visible gap
    assert len(replies) >= 1, "loop failed and no fallback reply was sent"
    # (b) the fallback was logged loudly
    assert any("AGENT_LOOP_FALLBACK" in r.getMessage() for r in caplog.records), \
        "fallback not logged at ERROR"


def test_loop_handles_inbound_when_flag_on(db, driver, monkeypatch):
    """Flag ON + the loop works → one reply via the loop path (LLM stubbed)."""
    import config
    from tests.factories import make_user
    from models import get_session, Message

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    user = make_user(db)
    replies = driver.send(user, "how much protein should i get today?")
    assert len(replies) >= 1

    s = get_session()
    try:
        outbound = s.query(Message).filter(
            Message.user_id == user.id, Message.direction == "out").count()
    finally:
        s.close()
    assert outbound >= 1
