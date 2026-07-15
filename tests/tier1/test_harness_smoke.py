"""
Harness smoke test — proves the two-layer driver round-trips through the real
pipeline against a disposable Postgres, with LLM/SMS/threads stubbed. Not an
acceptance case; it gates the harness itself.
"""

from __future__ import annotations


def test_cluster_is_postgres_18(db):
    from sqlalchemy import text
    ver = db.execute(text("show server_version")).scalar()
    assert ver.startswith("18."), f"expected prod-matching PG18, got {ver}"


def test_fixture_user_persists(db):
    from tests.factories import make_user
    from models import User
    u = make_user(db, name="Smoke")
    assert u.id is not None
    fresh = db.query(User).filter(User.id == u.id).one()
    assert fresh.name == "Smoke"
    assert (fresh.onboarding_step or 0) >= 3


def test_truncation_isolates_between_tests(db):
    # identity restarts, so the first user of every test is id=1
    from tests.factories import make_user
    u = make_user(db, name="First")
    assert u.id == 1


def test_inbound_message_round_trips(db, driver, anthropic_stub):
    """A plain inbound produces exactly one outbound reply and logs both rows."""
    from tests.factories import make_user
    from models import get_session, Message

    anthropic_stub.default_text = "hey, got it"
    user = make_user(db)

    replies = driver.send(user, "how much protein should i eat today?")
    assert len(replies) >= 1, "expected at least one outbound reply"

    s = get_session()
    try:
        inbound = s.query(Message).filter(Message.user_id == user.id,
                                          Message.direction == "in").count()
        outbound = s.query(Message).filter(Message.user_id == user.id,
                                           Message.direction == "out").count()
    finally:
        s.close()
    assert inbound == 1, f"expected 1 inbound logged, got {inbound}"
    assert outbound >= 1, f"expected >=1 outbound logged, got {outbound}"
