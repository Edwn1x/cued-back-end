"""
Tier-2 (live): repeating events end to end — the coach adds a weekly series after a yes, asks
"this one or all of them?" before touching a repeating event, and clears everything matching
a course in ONE confirmed call (Alexey's ask, 2026-10-27: "Remove all things related to Eleng
105"). Google mocked at the gcal client; model + tool loop + confirm gate are real.
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
    u = make_user(db, name="Nau", age=20, goal="muscle_building", experience="intermediate", occupation="student",
                  user_timezone=TZ)
    s = get_session()
    try:
        s.add(Integration(user_id=u.id, provider="gcal", status="connected", meta={}, external_id="nau@gmail.com"))
        s.commit()
    finally:
        s.close()
    return u


def _local(days_ahead, hour):
    loc = (datetime.now(ZoneInfo(TZ)) + timedelta(days=days_ahead)).replace(hour=hour, minute=0, second=0, microsecond=0)
    return loc.astimezone(timezone.utc).replace(tzinfo=None)


def _series(user_id, title, master, first_days_ahead, hour, n=4):
    from events import upsert_external_events
    rows = []
    for k in range(n):
        s = _local(first_days_ahead + 7 * k, hour)
        rows.append({"external_id": f"c@group:{master}_{s.strftime('%Y%m%dT%H%M%SZ')}", "title": title, "occurred_at": s,
                     "ends_at": s + timedelta(hours=1), "all_day": False, "series_id": master})
    upsert_external_events(user_id, source="gcal", rows=rows)


def _yes_until(driver, u, writes, tries=2):
    out = []
    for _ in range(tries):
        if writes:
            break
        out.extend(driver.send(u, "yes"))
    return out


def test_weekly_series_is_reflected_then_created_once(db, driver, monkeypatch):
    from integrations import gcal
    creates = []
    monkeypatch.setattr(gcal, "create_event", lambda uid, summary, s, e, **k: creates.append((summary, s, e, k)) or {"id": "ser1"})
    monkeypatch.setattr(gcal, "list_instances", lambda *a, **k: [])
    u = _connected_user(db)
    r1 = "\n".join(driver.send(u, "add gym every mon wed fri at 4pm till dec 12")).lower()
    print(f"\n[series add] coach: {r1!r}")
    assert creates == [], "nothing may be written on the first ask"
    assert "mon" in r1 and "wed" in r1 and "fri" in r1 and "4" in r1 and "pm" in r1, r1
    assert "dec" in r1, r1
    r2 = " / ".join(_yes_until(driver, u, creates)).lower()
    print(f"[series yes] coach: {r2!r}")
    assert len(creates) == 1, creates
    summary, s, e, k = creates[0]
    assert "gym" in summary.lower()
    assert k["recurrence"] and k["recurrence"][0].startswith("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=202612")
    assert s.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(TZ)).hour == 16


def test_remove_everything_for_a_course_is_one_confirmed_bulk_delete(db, driver, monkeypatch):
    from integrations import gcal
    deletes = []
    monkeypatch.setattr(gcal, "delete_event", lambda uid, cal, eid, **k: deletes.append(eid) or True)
    u = _connected_user(db)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17)
    _series(u.id, "ELENG 105 LEC", "lec1", 2, 10)
    _series(u.id, "gym", "gym1", 1, 16)
    r1 = "\n".join(driver.send(u, "Remove all things related to Eleng 105 off of my calendar.")).lower()
    print(f"\n[bulk ask] coach: {r1!r}")
    assert deletes == [], "nothing may be deleted on the first ask"
    assert "eleng" in r1 and ("?" in r1 or "all" in r1), r1
    assert "gone" not in r1 and "deleted" not in r1 and "removed" not in r1, r1
    r2 = " / ".join(_yes_until(driver, u, deletes)).lower()
    print(f"[bulk yes] coach: {r2!r}")
    assert sorted(deletes) == ["dis1", "lec1"], deletes          # each series once at its master; gym untouched
    from models import get_session, Event, active
    s = get_session()
    try:
        assert {e.title for e in active(s, Event, user_id=u.id).all()} == {"gym"}
    finally:
        s.close()


def test_repeating_event_delete_asks_one_or_all(db, driver, monkeypatch):
    from integrations import gcal
    deletes = []
    monkeypatch.setattr(gcal, "delete_event", lambda uid, cal, eid, **k: deletes.append(eid) or True)
    u = _connected_user(db)
    _series(u.id, "ELENG 105 DIS", "dis1", 1, 17)
    r1 = "\n".join(driver.send(u, "delete my eleng discussion")).lower()
    print(f"\n[one-or-all ask] coach: {r1!r}")
    assert deletes == []
    assert ("all" in r1 or "every" in r1) and ("just" in r1 or "one" in r1 or "this" in r1), r1
    r2 = "\n".join(driver.send(u, "all of them")).lower()
    print(f"[one-or-all answer] coach: {r2!r}")
    r3 = " / ".join(_yes_until(driver, u, deletes)).lower()
    print(f"[one-or-all yes] coach: {r3!r}")
    assert deletes == ["dis1"], deletes
