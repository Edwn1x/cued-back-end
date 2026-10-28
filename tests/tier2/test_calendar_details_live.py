"""
Tier-2 (live): the rest of an event in ONE ask — guests, meet link, reminder, every-other-week,
room — and an RSVP decline. Google mocked at the gcal client; model + loop + confirm gate real.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
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
    u = make_user(db, name="Nau", age=20, goal="muscle_building", experience="intermediate", occupation="student", user_timezone=TZ)
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


def test_study_group_with_guest_meet_reminder_biweekly_is_one_confirm(db, driver, monkeypatch):
    from integrations import gcal
    creates = []
    monkeypatch.setattr(gcal, "writable_calendars", lambda uid, **k: [{"id": "nau@gmail.com", "name": "nau@gmail.com", "primary": True}])
    monkeypatch.setattr(gcal, "create_event", lambda uid, summary, s, e, **k: creates.append((summary, s, e, k)) or
                        {"id": "sg1", "hangoutLink": "https://meet.google.com/abc-defg-hij"})
    monkeypatch.setattr(gcal, "list_instances", lambda *a, **k: [])
    u = _connected_user(db)
    r1 = "\n".join(driver.send(u, "set up a cs61a study group every other thursday 7-9pm at moffitt, invite sam@berkeley.edu, "
                                  "add a meet link and remind me 30 min before")).lower()
    print(f"\n[details ask] coach: {r1!r}")
    assert creates == []
    assert "thu" in r1 and "7" in r1 and "9" in r1 and "pm" in r1, r1
    assert "sam@berkeley.edu" in r1 and "meet" in r1 and "30" in r1, r1
    assert "can't" not in r1 and "cannot" not in r1, r1
    r2 = " / ".join(_yes_until(driver, u, creates)).lower()
    print(f"[details yes] coach: {r2!r}")
    assert len(creates) == 1, creates
    summary, s, e, k = creates[0]
    assert "study" in summary.lower()
    assert s.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TZ)).hour == 19 and (e - s) == timedelta(hours=2)
    assert k["guests"] == ["sam@berkeley.edu"] and k["meet"] is True and k["reminders"] == [30], k
    assert k["recurrence"][0].startswith("RRULE:FREQ=WEEKLY;BYDAY=TH;INTERVAL=2"), k
    assert k.get("location") and "moffitt" in k["location"].lower(), k
    assert "meet.google.com/abc-defg-hij" in r2, r2                       # the link reached the user


def test_decline_an_invite_confirms_then_answers(db, driver, monkeypatch):
    from integrations import gcal
    from events import upsert_external_event
    answers = []
    monkeypatch.setattr(gcal, "rsvp_event", lambda uid, cal, eid, response, **k: answers.append((eid, response)) or {"_organizer": "Jake"})
    u = _connected_user(db)
    s = (datetime.now(ZoneInfo(TZ)) + timedelta(days=2)).replace(hour=20, minute=0, second=0, microsecond=0).astimezone(timezone.utc).replace(tzinfo=None)
    upsert_external_event(u.id, source="gcal", external_id="nau@gmail.com:fm1", title="frat chapter meeting",
                          occurred_at=s, ends_at=s + timedelta(hours=1))
    r1 = "\n".join(driver.send(u, "decline the frat chapter meeting, i can't make it")).lower()
    print(f"\n[rsvp ask] coach: {r1!r}")
    assert answers == []
    assert "frat" in r1 and ("?" in r1 or "decline" in r1), r1
    r2 = " / ".join(_yes_until(driver, u, answers)).lower()
    print(f"[rsvp yes] coach: {r2!r}")
    assert answers == [("fm1", "declined")], answers
