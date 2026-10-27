"""
Tier-2 (live): Alexey's thread from 2026-10-27 13:09–13:12 PT, replayed. Then the coach added
one Friday at a time and said "i can't choose which calendar it goes on or set the color".
Now: ONE reflect-back covering every friday + Class schedule calendar + yellow (+ skipping
thanksgiving if they say so) → yes → one create. Google mocked at the gcal client.
"""
from __future__ import annotations

from datetime import timezone, timedelta, datetime
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

pytestmark = pytest.mark.tier2
TZ = "America/Los_Angeles"


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
    u = make_user(db, name="Alexey", age=22, goal="muscle_building", experience="intermediate", occupation="student",
                  user_timezone=TZ)
    s = get_session()
    try:
        s.add(Integration(user_id=u.id, provider="gcal", status="connected", meta={}, external_id="alexey@berkeley.edu"))
        s.commit()
    finally:
        s.close()
    return u


def _yes_until(driver, u, writes, tries=2):
    out = []
    for _ in range(tries):
        if writes:
            break
        out.extend(driver.send(u, "Yes"))
    return out


def test_every_friday_on_class_schedule_in_yellow_is_one_confirm_one_create(db, driver, monkeypatch):
    from integrations import gcal
    creates = []
    monkeypatch.setattr(gcal, "writable_calendars", lambda uid, **k: [
        {"id": "alexey@berkeley.edu", "name": "alexey@berkeley.edu", "primary": True},
        {"id": "c_class@group.calendar.google.com", "name": "Class schedule", "primary": False}])
    monkeypatch.setattr(gcal, "create_event", lambda uid, summary, s, e, **k: creates.append((summary, s, e, k)) or {"id": "oh1"})
    monkeypatch.setattr(gcal, "list_instances", lambda *a, **k: [])
    u = _connected_user(db)

    r1 = "\n".join(driver.send(u, "Add INDENG 142A OH every friday 1-3pm in Etcheverry 4176A through dec 4, skip thanksgiving week. "
                                  "Make sure it's added to the Class schedule calendar and is yellow")).lower()
    print(f"\n[alexey ask] coach: {r1!r}")
    assert creates == [], "nothing may be written on the first ask"
    assert "fri" in r1 and "1" in r1 and "3" in r1 and "pm" in r1, r1
    assert "class schedule" in r1 and "yellow" in r1, r1
    assert "can't" not in r1 and "cannot" not in r1 and "one at a time" not in r1 and "separately" not in r1, r1

    r2 = " / ".join(_yes_until(driver, u, creates)).lower()
    print(f"[alexey yes] coach: {r2!r}")
    assert len(creates) == 1, creates
    summary, s, e, k = creates[0]
    assert "indeng" in summary.lower()
    assert s.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TZ)).hour == 13 and (e - s) == timedelta(hours=2)
    assert k["calendar_id"] == "c_class@group.calendar.google.com", k
    assert k["color_id"] == "5", k
    assert k["recurrence"][0].startswith("RRULE:FREQ=WEEKLY;BYDAY=FR;UNTIL=202612"), k
    assert any(line.startswith("EXDATE;TZID=") and "20261127T130000" in line for line in k["recurrence"]), k
    assert k.get("location") and "etcheverry" in k["location"].lower(), k
    assert "can't" not in r2 and "urself" not in r2 and "yourself" not in r2, r2
