"""
Live 2026-11-03: user 42's 'go run' (tue/thu, onboarding) fired 13 times across six silent
weeks; users 33 and 53 kept getting water pings two weeks into silence; user 40 got seven in
five silent days and the founder hand-sent "want me to pause the water reminders?". Standing
rows now skip past REMINDER_MAX_QUIET_DAYS and resume the day they're back; one-offs they
asked for still fire.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _inbound(db, uid, days_ago):
    from models import Message
    db.add(Message(user_id=uid, direction="in", body="hey", message_type="freeform",
                   created_at=_now() - timedelta(days=days_ago)))
    db.commit()


def _due(db, rid):
    from models import Reminder
    row = db.get(Reminder, rid)
    row.fire_at = _now() - timedelta(minutes=1)
    db.commit()


def _row(db, rid):
    from models import Reminder
    db.expire_all()
    return db.get(Reminder, rid)


@pytest.fixture
def sent(monkeypatch):
    calls = []
    import reminders
    monkeypatch.setattr(reminders, "_quiet_user_sms_calls", calls, raising=False)
    import sms
    monkeypatch.setattr(sms, "send_sms", lambda *a, **k: calls.append((a, k)) or {"ok": True})
    return calls


def test_a_weekly_row_skips_while_quiet_and_resumes_when_they_are_back(db, sent, caplog):
    import logging
    from reminders import create_reminder, _fire_one
    caplog.set_level(logging.INFO)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _inbound(db, u.id, days_ago=40)                                  # six silent weeks
    rid = create_reminder(u.id, "go run", "19:30", days="tue,thu", source="onboarding")["id"]
    _due(db, rid)
    assert _fire_one(rid, _now()) is False and sent == []
    row = _row(db, rid)
    assert row.active and row.fire_at > _now()                       # re-armed, not cancelled
    assert "reason=quiet_user" in caplog.text
    # they text → the next due occurrence sends
    _inbound(db, u.id, days_ago=0)
    _due(db, rid)
    assert _fire_one(rid, _now()) is True and len(sent) == 1


def test_water_interval_rows_skip_too_but_one_offs_still_fire(db, sent, monkeypatch):
    import config
    from reminders import create_reminder, _fire_one
    monkeypatch.setattr(config, "INTERVAL_GATE_V2_ENABLED", False)
    u = make_user(db, user_timezone="America/Los_Angeles", wake_time="07:00", sleep_time="23:00")
    _inbound(db, u.id, days_ago=14)
    water = create_reminder(u.id, "drink water", "09:00", every_hours=2, source="offer")["id"]
    _due(db, water)
    assert _fire_one(water, _now()) is False and sent == [] and _row(db, water).active
    one_off = create_reminder(u.id, "call the dentist", "09:00", date_str="tomorrow")["id"]
    _due(db, one_off)
    assert _fire_one(one_off, _now()) is True and len(sent) == 1


def test_threshold_edges_new_account_and_flag_off(db, sent, monkeypatch):
    import config
    from reminders import create_reminder, _fire_one, _quiet_user
    from models import get_session, User
    u = make_user(db, user_timezone="America/Los_Angeles")
    rid = create_reminder(u.id, "gym", "18:00", days="mon,tue,wed,thu,fri,sat,sun", source="program")["id"]
    s = get_session()
    try:
        user = s.get(User, u.id)
        assert _quiet_user(user, s, _now()) is False                 # nothing on record → never quiet
        _inbound(db, u.id, days_ago=4)
        assert _quiet_user(user, s, _now()) is False                 # inside the limit
        _inbound(db, u.id, days_ago=6)                               # the 4-day one is still the latest
        assert _quiet_user(user, s, _now()) is False
    finally:
        s.close()
    v = make_user(db, user_timezone="America/Los_Angeles", phone="+15550005001")
    _inbound(db, v.id, days_ago=6)
    vid = create_reminder(v.id, "gym", "18:00", days="mon,tue,wed,thu,fri,sat,sun", source="program")["id"]
    _due(db, vid)
    assert _fire_one(vid, _now()) is False and sent == []
    monkeypatch.setattr(config, "REMINDER_MAX_QUIET_DAYS", 0)
    _due(db, vid)
    assert _fire_one(vid, _now()) is True and len(sent) == 1
