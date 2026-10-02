"""
Tier-2 (live): a bare-hour calendar ask must be reflected back with am/pm and NOT written
in the same turn (2026-10-02, founder's demo: "add a quick gym session at 8" at 4am → an
8am block written immediately; he meant 8pm). Code now refuses an un-staged confirm; this
pins that the model's behaviour reads right end to end.
"""
from __future__ import annotations

import pytest

from tests.factories import make_user

pytestmark = pytest.mark.tier2


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    import config
    from cryptography.fernet import Fernet
    for f in ("GCAL_ENABLED", "CALENDAR_WRITE_ENABLED", "SEND_CONNECT_LINK_TOOL_ENABLED", "SINGLE_AGENT_LOOP_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)
    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "connect-test-secret")
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)


def _connected_user(db):
    from models import get_session, Integration
    u = make_user(db, name="Nau", age=20, goal="muscle_building", experience="intermediate", occupation="student")
    s = get_session()
    try:
        s.add(Integration(user_id=u.id, provider="gcal", status="connected", meta={}, external_id="nau@gmail.com"))
        s.commit()
    finally:
        s.close()
    return u


def test_bare_hour_is_asked_not_written(db, driver, monkeypatch):
    from integrations import gcal
    writes = []
    monkeypatch.setattr(gcal, "create_event", lambda *a, **k: writes.append((a, k)) or {"id": "evt1"})
    u = _connected_user(db)
    replies = driver.send(u, "Can you add a quick gym session at 8")
    reply = "\n".join(replies).lower()
    print(f"\n[bare hour] coach: {reply!r}")
    assert writes == [], "nothing may be written on a bare-hour ask"
    assert "8" in reply and ("am" in reply and "pm" in reply or "?" in reply), reply
    assert "added" not in reply, reply

    # they answer; a yes in the NEXT turn writes
    replies2 = driver.send(u, "8pm")
    reply2 = "\n".join(replies2).lower()
    print(f"[bare hour 2] coach: {reply2!r}")
    if not writes:
        replies3 = driver.send(u, "yes")
        print(f"[bare hour 3] coach: {' / '.join(replies3).lower()!r}")
    assert len(writes) == 1, "exactly one write after the user's yes"
    a, k = writes[0]
    from datetime import timezone
    from zoneinfo import ZoneInfo
    start_local = a[2].replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
    assert start_local.hour == 20, f"wrote {start_local}"
