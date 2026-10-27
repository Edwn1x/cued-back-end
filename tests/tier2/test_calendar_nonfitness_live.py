"""
Tier-2 (live): calendar create / move / delete are NOT gym-only. A dentist appointment, a
club meeting, a flight — the coach adds, moves and removes them the same way (founder asked
2026-10-27: "what about non gym or fitness related events?").
"""
from __future__ import annotations

from datetime import timezone
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

pytestmark = pytest.mark.tier2


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    import config
    from cryptography.fernet import Fernet
    for f in ("GCAL_ENABLED", "CALENDAR_WRITE_ENABLED", "SEND_CONNECT_LINK_TOOL_ENABLED", "SINGLE_AGENT_LOOP_ENABLED",
              "LOOKUP_EVENTS_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(config, "GOOGLE_OAUTH_TESTING_MODE", False)
    monkeypatch.setattr(config, "INTEGRATION_TOKEN_ENC_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(config, "CONNECT_TOKEN_SECRET", "connect-test-secret")
    monkeypatch.setattr(config, "PHOTON_PROVISIONING_ENABLED", False)


def _connected_user(db):
    from models import get_session, Integration
    u = make_user(db, name="Nau", age=20, goal="muscle_building", experience="intermediate", occupation="student",
                  user_timezone="America/Los_Angeles")
    s = get_session()
    try:
        s.add(Integration(user_id=u.id, provider="gcal", status="connected", meta={}, external_id="nau@gmail.com"))
        s.commit()
    finally:
        s.close()
    return u


def _yes_until(driver, u, writes, tries=2):
    out = []
    for _ in range(tries):
        if writes:
            break
        out.extend(driver.send(u, "yes"))
    return out


def test_dentist_appointment_is_added_then_deleted(db, driver, monkeypatch):
    from integrations import gcal
    creates, deletes = [], []
    monkeypatch.setattr(gcal, "create_event", lambda uid, summary, s, e, **k: creates.append((summary, s, e)) or {"id": "dent1"})
    monkeypatch.setattr(gcal, "delete_event", lambda uid, cal, eid, **k: deletes.append((cal, eid)) or True)
    u = _connected_user(db)

    r1 = "\n".join(driver.send(u, "add my dentist appointment tomorrow at 3pm to my calendar")).lower()
    print(f"\n[dentist add] coach: {r1!r}")
    assert creates == [], "nothing may be written on the first ask"
    assert "dentist" in r1 and "3" in r1 and "pm" in r1, r1
    assert "can't" not in r1 and "only" not in r1, r1          # no "i only do gym stuff"

    r2 = " / ".join(_yes_until(driver, u, creates)).lower()
    print(f"[dentist yes] coach: {r2!r}")
    assert len(creates) == 1, creates
    summary, s, _ = creates[0]
    assert "dentist" in summary.lower(), summary
    assert s.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles")).hour == 15

    r3 = "\n".join(driver.send(u, "actually cancel the dentist thing, take it off")).lower()
    print(f"[dentist delete ask] coach: {r3!r}")
    assert deletes == [], "nothing may be deleted on the first ask"
    assert "dentist" in r3, r3

    r4 = " / ".join(_yes_until(driver, u, deletes)).lower()
    print(f"[dentist delete yes] coach: {r4!r}")
    assert deletes == [("nau@gmail.com", "dent1")], deletes
