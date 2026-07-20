"""
Phase 1 — webhook MessageSid idempotency (claim-at-top + release-on-crash).

Ships live to the prod webhook on merge (dedup can't be flag-gated), so it gets
its own tests for both failure modes: a duplicate delivery during/after
processing writes state once; a crash after the claim releases it so a retry
reprocesses. Fail-open on missing sid / unexpected errors.
"""

from __future__ import annotations


def test_claim_is_idempotent_and_releasable(db):
    from tests.factories import make_user
    from models import claim_message_sid, release_message_sid

    user = make_user(db)
    assert claim_message_sid("SMa", user.id) is True    # first claim proceeds
    assert claim_message_sid("SMa", user.id) is False   # duplicate is caught
    release_message_sid("SMa")
    assert claim_message_sid("SMa", user.id) is True     # reclaimable after release
    # fail-open: an empty/missing sid must never block processing
    assert claim_message_sid("", user.id) is True


def test_duplicate_sid_writes_state_once(db, driver):
    """The observed bug: a re-delivered inbound (same MessageSid) must not
    double-write. One inbound row, one ledger row."""
    from tests.factories import make_user
    from models import get_session, Message, ProcessedMessage

    user = make_user(db)
    driver.send(user, "just ate a chicken burrito bowl", message_sid="SMdup")
    driver.send(user, "just ate a chicken burrito bowl", message_sid="SMdup")

    s = get_session()
    try:
        inbound = s.query(Message).filter(
            Message.user_id == user.id, Message.direction == "in").count()
        ledger = s.query(ProcessedMessage).filter_by(message_sid="SMdup").count()
    finally:
        s.close()
    assert inbound == 1, f"duplicate delivery wrote {inbound} inbound rows"
    assert ledger == 1


def test_crash_after_claim_releases_sid_so_redelivery_reprocesses(db, driver, monkeypatch):
    """A crash in the synchronous pass after the claim must not permanently
    dedupe the message — the claim is released so a genuine re-delivery of the
    same sid (or a user resend) can reprocess. NOTE: Twilio does not auto-retry
    inbound webhooks by default, so the crash itself is logged WEBHOOK_DROPPED;
    the release only prevents a poisoned claim from blocking a later delivery."""
    import app
    from tests.factories import make_user
    from models import get_session, Message, ProcessedMessage

    user = make_user(db)

    # Force a crash right after the claim, before any state write (log_incoming
    # runs immediately after the claim), so the crashed pass leaves nothing.
    def boom(*a, **k):
        raise RuntimeError("simulated mid-pass crash")
    monkeypatch.setattr(app, "log_incoming", boom)

    driver.send(user, "hey coach", message_sid="SMcrash")

    s = get_session()
    try:
        assert s.query(ProcessedMessage).filter_by(message_sid="SMcrash").count() == 0, \
            "claim not released after crash — a retry would be silently dropped"
    finally:
        s.close()

    # The retry (same sid) now reprocesses cleanly.
    monkeypatch.undo()
    driver.send(user, "hey coach", message_sid="SMcrash")

    s = get_session()
    try:
        inbound = s.query(Message).filter(
            Message.user_id == user.id, Message.direction == "in").count()
        ledger = s.query(ProcessedMessage).filter_by(message_sid="SMcrash").count()
    finally:
        s.close()
    assert inbound == 1, f"retry did not reprocess cleanly (inbound={inbound})"
    assert ledger == 1
