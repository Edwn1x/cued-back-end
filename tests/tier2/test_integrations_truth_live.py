"""
Tier-2 (live): the INTEGRATIONS block outranks memory (2026-10-02, founder mid demo).
Coaching summary says Google Calendar is connected; there is no gcal row. "Can you see my
calendar?" must get a no + the connect link, and a correction must not be argued with.
"""
from __future__ import annotations

import pytest

from tests.factories import make_user

pytestmark = pytest.mark.tier2


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    import config
    from cryptography.fernet import Fernet
    for f in ("GCAL_ENABLED", "BCOURSES_ENABLED", "GOOGLE_HEALTH_ENABLED", "SEND_CONNECT_LINK_TOOL_ENABLED",
              "SINGLE_AGENT_LOOP_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)
    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "connect-test-secret")
    monkeypatch.setattr(config, "INTEGRATIONS_BASE_URL", "https://app.example")
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)


def _founder_like(db):
    from models import get_session, Integration
    u = make_user(db, name="Nau", age=20, goal="fat_loss,muscle_building", experience="intermediate", occupation="student",
                  coaching_summary=("## Coaching Decisions\n- Goal: recomp; 2450/day, 136g protein\n"
                                    "- Connected feeds: **Google Calendar, bCourses, house menu, Google Health, Fitbit (connected Oct 2)**\n"
                                    "## Recent Themes\n- Tomorrow looks packed (Oct 2)"))
    s = get_session()
    try:
        s.add(Integration(user_id=u.id, provider="bcourses", status="connected", meta={"feed_url": "https://x/feed.ics"}))
        s.add(Integration(user_id=u.id, provider="google_health", status="connected", meta={}))
        s.commit()
    finally:
        s.close()
    return u


def test_stale_summary_does_not_beat_the_block(db, driver, caplog):
    u = _founder_like(db)
    replies = driver.send(u, "Can you see my calendar? Tomorrow is packed for me")
    reply = "\n".join(replies).lower()
    print(f"\n[truth] coach: {reply!r}")
    assert any("/c/gcal?t=" in r for r in replies), "the connect link must go out"
    assert not any(k in reply for k in ("yeah i can see", "i can see it", "it's connected", "i can see ur calendar")), reply


def test_correction_is_not_argued_with(db, driver, caplog):
    from models import get_session, Message
    u = _founder_like(db)
    s = get_session()
    try:
        s.add(Message(user_id=u.id, direction="in", body="Can you see my calendar? Tomorrow is packed for me", message_type="freeform"))
        s.add(Message(user_id=u.id, direction="out", body="yeah i can see it", message_type="freeform"))
        s.commit()
    finally:
        s.close()
    replies = driver.send(u, "So you were supposed to say no since you can't see my calendar right now")
    reply = "\n".join(replies).lower()
    print(f"\n[truth 2] coach: {reply!r}")
    assert not any(k in reply for k in ("i actually can see", "it's connected", "i can see it fine")), reply
    assert any(k in reply for k in ("my bad", "ur right", "you're right", "can't see", "not connected", "mb")), reply
