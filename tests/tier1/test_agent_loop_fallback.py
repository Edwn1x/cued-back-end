"""
Phase 6 — the loop-is-sole-responder invariant. The legacy classifier->specialists
fallback is gone (deleted with the pipeline). When the single-agent loop fails at
runtime — or is turned off via the SINGLE_AGENT_LOOP_ENABLED lever, or returns
nothing — the webhook sends ONE safe minimal line in the coach's voice and logs
loudly at ERROR. It never reaches for a legacy orchestrator (there is none).
"""

from __future__ import annotations

import logging


def test_loop_failure_sends_safe_reply_and_logs_no_legacy(db, driver, monkeypatch, caplog):
    """Flag ON + the loop raises → exactly one safe reply, a loud ERROR, and NO
    legacy orchestrator call (the reply is the safe line, not legacy output)."""
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

    # (a) exactly one safe minimal reply is sent — no user-visible gap. (The SMS layer
    # normalizes the em-dash to a hyphen, so match on the stable substring.)
    assert len(replies) == 1, f"expected exactly one reply, got {replies!r}"
    assert "glitched for a sec" in replies[0], f"expected the safe line, got {replies!r}"
    # ...and NOT the outer catch-all message (that would mean an unhandled crash / legacy)
    assert "Something went wrong" not in replies[0]
    # (b) the failure was logged loudly with a traceback marker
    assert any("AGENT_LOOP_FAILED" in r.getMessage() for r in caplog.records), \
        "loop failure not logged at ERROR"
    # (c) the retired legacy fallback marker must NOT appear
    assert not any("AGENT_LOOP_FALLBACK" in r.getMessage() for r in caplog.records), \
        "legacy fallback marker should be gone"


def test_loop_disabled_sends_safe_reply_and_logs(db, driver, monkeypatch, caplog):
    """Flag OFF (the permanently-on lever turned down) → no loop call, but the user
    still gets the safe line and an ERROR marker is logged."""
    import app
    import config
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", False)

    def should_not_run(*a, **k):
        raise AssertionError("run_agent_loop must not be called when the flag is off")
    monkeypatch.setattr(app, "run_agent_loop", should_not_run)

    user = make_user(db)
    with caplog.at_level(logging.ERROR):
        replies = driver.send(user, "hey what's up")

    assert len(replies) == 1 and "glitched for a sec" in replies[0], \
        f"expected the safe line, got {replies!r}"
    assert any("AGENT_LOOP_NO_RESPONSE" in r.getMessage() for r in caplog.records), \
        "disabled-loop path not logged at ERROR"


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
