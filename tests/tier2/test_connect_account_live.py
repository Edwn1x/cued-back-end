"""
Tier-2 (live): the account-first connect flow while Google OAuth is in Testing
(connect_offers.py, 2026-10-02). The coach, asked for a calendar connection with no Google
account on file, must ASK which account (no link, no promise); given the address, it must
call set_google_account and say the link comes once it's set up.

Run: pytest tests/tier2/test_connect_account_live.py --run-tier2 -s
"""
from __future__ import annotations

import logging

import pytest

from tests.factories import make_user

pytestmark = pytest.mark.tier2


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    import config
    from cryptography.fernet import Fernet
    for f in ("GOOGLE_OAUTH_TESTING_MODE", "GCAL_ENABLED", "GOOGLE_HEALTH_ENABLED", "SEND_CONNECT_LINK_TOOL_ENABLED",
              "SINGLE_AGENT_LOOP_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "connect-test-secret")
    monkeypatch.setattr(config, "INTEGRATIONS_BASE_URL", "https://app.example")
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


def test_calendar_ask_without_an_account_asks_for_the_account(db, driver, caplog):
    user = make_user(db, name="Nau", age=20, goal="muscle_building", experience="beginner", occupation="student")
    with caplog.at_level(logging.INFO):
        replies = driver.send(user, "can u see my google calendar? my week is packed")
    reply = "\n".join(replies).lower()
    print(f"\n[account ask] coach: {reply!r}")
    assert replies, "a reply went out"
    assert "/c/gcal" not in reply, "no link before the account is set up"
    assert any(k in reply for k in ("which google", "what google", "google account", "gmail")), reply
    assert not any(k in reply for k in ("sent", "here's the link", "tap", "sending it now")), reply


def test_given_the_address_the_coach_saves_it_and_promises_the_link_later(db, driver, caplog):
    from models import get_session, Message
    user = make_user(db, name="Nau", age=20, goal="muscle_building", experience="beginner", occupation="student")
    s = get_session()
    try:
        s.add(Message(user_id=user.id, direction="in", body="can u see my google calendar? my week is packed", message_type="freeform"))
        s.add(Message(user_id=user.id, direction="out", body="yeah, which google account is ur calendar on", message_type="freeform"))
        s.commit()
    finally:
        s.close()
    with caplog.at_level(logging.INFO):
        replies = driver.send(user, "it's on nau.test.user@gmail.com")
    reply = "\n".join(replies).lower()
    print(f"\n[account given] coach: {reply!r}")
    assert _u(user.id).google_email == "nau.test.user@gmail.com", "set_google_account was called"
    assert "/c/gcal" not in reply, "not set up yet → no link"
    assert any(k in reply for k in ("once", "when it", "set up", "ready", "tomorrow", "within a day", "soon")), reply
    assert any("GOOGLE_ACCOUNT_SET" in r.getMessage() for r in caplog.records)
