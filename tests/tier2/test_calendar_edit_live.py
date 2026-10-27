"""
Tier-2 (live): the coach moves and deletes ONE named calendar event only after the user's
yes — never on the first ask, never a different event. Sibling of test_calendar_confirm_live.py
(create). Google is mocked at the gcal client; the model, the tool loop and the confirm gate
are real.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
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


def _seed(u):
    """Tomorrow 4–5pm 'gym — push' and tomorrow 7–9pm 'study block' on their synced calendar."""
    from events import upsert_external_event
    tz = ZoneInfo("America/Los_Angeles")
    tmr = (datetime.now(tz) + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    gym = tmr.replace(hour=16).astimezone(timezone.utc).replace(tzinfo=None)
    study = tmr.replace(hour=19).astimezone(timezone.utc).replace(tzinfo=None)
    upsert_external_event(u.id, source="gcal", external_id="nau@gmail.com:gym1", title="gym — push",
                          occurred_at=gym, ends_at=gym + timedelta(hours=1))
    upsert_external_event(u.id, source="gcal", external_id="nau@gmail.com:study1", title="study block",
                          occurred_at=study, ends_at=study + timedelta(hours=2))
    return gym, study


def _say_yes_until(driver, u, writes, tries=2):
    out = []
    for _ in range(tries):
        if writes:
            break
        out.extend(driver.send(u, "yes"))
    return out


def test_delete_asks_then_deletes_only_the_named_event(db, driver, monkeypatch):
    from integrations import gcal
    deletes, patches = [], []
    monkeypatch.setattr(gcal, "delete_event", lambda uid, cal, eid, **k: deletes.append((cal, eid)) or True)
    monkeypatch.setattr(gcal, "update_event", lambda *a, **k: patches.append((a, k)) or {})
    u = _connected_user(db)
    _seed(u)

    replies = driver.send(u, "take tomorrow's gym block off my calendar")
    reply = "\n".join(replies).lower()
    print(f"\n[delete ask] coach: {reply!r}")
    assert deletes == [], "nothing may be deleted on the first ask"
    assert "gym" in reply and ("?" in reply or "want me" in reply or "remove" in reply or "delete" in reply), reply
    assert "deleted" not in reply and "gone" not in reply, reply

    replies2 = _say_yes_until(driver, u, deletes)
    print(f"[delete yes] coach: {' / '.join(replies2).lower()!r}")
    assert deletes == [("nau@gmail.com", "gym1")], f"exactly the gym event, once: {deletes}"
    assert patches == []

    from models import get_session, Event, active
    s = get_session()
    try:
        titles = {e.title for e in active(s, Event, user_id=u.id).all()}
        assert titles == {"study block"}, titles          # the mirror dropped the gym, kept study
    finally:
        s.close()


def test_move_asks_with_ampm_then_patches_once(db, driver, monkeypatch):
    from integrations import gcal
    deletes, patches = [], []
    monkeypatch.setattr(gcal, "delete_event", lambda *a, **k: deletes.append(a) or True)
    monkeypatch.setattr(gcal, "update_event", lambda uid, cal, eid, **k: patches.append((cal, eid, k)) or {"id": eid})
    u = _connected_user(db)
    gym, _ = _seed(u)

    replies = driver.send(u, "push my gym block tomorrow to 6pm")
    reply = "\n".join(replies).lower()
    print(f"\n[move ask] coach: {reply!r}")
    assert patches == [] and deletes == [], "nothing may change on the first ask"
    assert "6" in reply and "pm" in reply, reply
    assert "moved" not in reply, reply

    replies2 = _say_yes_until(driver, u, patches)
    print(f"[move yes] coach: {' / '.join(replies2).lower()!r}")
    assert len(patches) == 1 and patches[0][:2] == ("nau@gmail.com", "gym1"), patches
    k = patches[0][2]
    start_local = k["start_dt"].replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
    assert start_local.hour == 18, f"moved to {start_local}"
    assert (k["end_dt"] - k["start_dt"]) == timedelta(hours=1)   # length kept
    assert deletes == []

    from models import get_session, Event, active
    s = get_session()
    try:
        row = active(s, Event, user_id=u.id).filter(Event.external_id == "nau@gmail.com:gym1").one()
        assert row.occurred_at == gym + timedelta(hours=2)        # mirror moved in place
    finally:
        s.close()
