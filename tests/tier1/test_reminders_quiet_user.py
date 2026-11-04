"""
Live 2026-11-03: user 42's 'go run' fired 13 times across six silent weeks; users 33/53 kept
getting water pings two weeks into silence. Founder 2026-11-04: don't pause them — "what if
they're actually using them?" — ASK. Past REMINDER_MAX_QUIET_DAYS one occurrence becomes the
check-in, the next ones go out as usual, and it's re-asked only after another
REMINDER_QUIET_CHECKIN_EVERY_DAYS of silence. One-offs are untouched.
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
    import sms, reminders
    monkeypatch.setattr(sms, "send_sms", lambda phone, text, **k: calls.append((text, k.get("message_type"))) or {"ok": True})
    monkeypatch.setattr(reminders, "_compose", lambda user, row: f"reminder: {row.text}")
    import config
    monkeypatch.setattr(config, "WATER_GIF_ENABLED", False)
    monkeypatch.setattr(config, "INTERVAL_GATE_V2_ENABLED", False)
    monkeypatch.setattr(config, "PROGRAM_REMINDER_YIELDS_TO_BRIEFING_ENABLED", False)
    monkeypatch.setattr(config, "PROGRAM_REMINDER_QUIET_GATE_ENABLED", False)
    return calls


def _record_checkin(db, uid, text, days_ago=0):
    from models import Message
    from reminders import QUIET_CHECKIN_TYPE
    db.add(Message(user_id=uid, direction="out", body=text, message_type=QUIET_CHECKIN_TYPE,
                   created_at=_now() - timedelta(days=days_ago)))
    db.commit()


def test_a_quiet_user_gets_asked_once_then_the_pings_keep_going(db, sent, caplog):
    import logging
    from reminders import create_reminder, _fire_one, QUIET_CHECKIN_TYPE
    caplog.set_level(logging.INFO)
    u = make_user(db, user_timezone="America/Los_Angeles")
    _inbound(db, u.id, days_ago=40)                                  # six silent weeks
    rid = create_reminder(u.id, "go run", "19:30", days="tue,thu", source="onboarding")["id"]
    _due(db, rid)
    assert _fire_one(rid, _now()) is True
    assert sent == [("been a min. still want the 'go run' reminders? 👍 and they stay, say stop and they're gone", QUIET_CHECKIN_TYPE)]
    assert "REMINDER_QUIET_CHECKIN" in caplog.text
    row = _row(db, rid)
    assert row.active and row.fire_at > _now()                       # re-armed like a send
    # the ask is a Message row in prod (send_sms writes it); mirror that here
    _record_checkin(db, u.id, sent[0][0])
    # next occurrence: still silent → the ping itself goes, no second ask
    _due(db, rid)
    assert _fire_one(rid, _now()) is True and sent[-1] == ("reminder: go run", "reminder")
    # and again — not re-asked inside the 14-day window
    _due(db, rid)
    assert _fire_one(rid, _now()) is True and sent[-1][1] == "reminder"
    assert sum(1 for t, k in sent if k == QUIET_CHECKIN_TYPE) == 1


def test_water_pings_ask_too_and_reask_after_two_more_silent_weeks(db, sent):
    from reminders import create_reminder, _fire_one, QUIET_CHECKIN_TYPE
    from engagement_tracker import WATER_REMINDER_TYPE
    u = make_user(db, user_timezone="America/Los_Angeles", wake_time="07:00", sleep_time="23:00")
    _inbound(db, u.id, days_ago=20)
    water = create_reminder(u.id, "drink water", "09:00", every_hours=2, source="offer")["id"]
    _record_checkin(db, u.id, "been a min. still want the water pings? …", days_ago=15)   # asked 15 days ago
    _due(db, water)
    assert _fire_one(water, _now()) is True
    assert sent[-1] == ("been a min. still want the water pings? 👍 and they stay, say stop and they're gone", QUIET_CHECKIN_TYPE)
    _record_checkin(db, u.id, sent[-1][0])
    _due(db, water)
    assert _fire_one(water, _now()) is True and sent[-1][1] == WATER_REMINDER_TYPE     # pings continue


def test_one_offs_fresh_accounts_and_the_flag(db, sent, monkeypatch):
    import config
    from reminders import create_reminder, _fire_one, QUIET_CHECKIN_TYPE
    u = make_user(db, user_timezone="America/Los_Angeles")
    _inbound(db, u.id, days_ago=30)
    one_off = create_reminder(u.id, "call the dentist", "09:00", date_str="tomorrow")["id"]
    _due(db, one_off)
    assert _fire_one(one_off, _now()) is True and sent[-1] == ("reminder: call the dentist", "reminder")
    # nothing on record → never quiet
    v = make_user(db, user_timezone="America/Los_Angeles", phone="+15550005001")
    vid = create_reminder(v.id, "gym", "18:00", days="mon,tue,wed,thu,fri,sat,sun", source="program")["id"]
    _due(db, vid)
    assert _fire_one(vid, _now()) is True and sent[-1][1] == "reminder"
    # inside the limit → no ask
    w = make_user(db, user_timezone="America/Los_Angeles", phone="+15550005002")
    _inbound(db, w.id, days_ago=4)
    wid = create_reminder(w.id, "gym", "18:00", days="mon,tue,wed,thu,fri,sat,sun", source="program")["id"]
    _due(db, wid)
    assert _fire_one(wid, _now()) is True and sent[-1][1] == "reminder"
    # past it → the gym-pings wording; flag off → plain ping
    x = make_user(db, user_timezone="America/Los_Angeles", phone="+15550005003")
    _inbound(db, x.id, days_ago=6)
    xid = create_reminder(x.id, "gym", "18:00", days="mon,tue,wed,thu,fri,sat,sun", source="program")["id"]
    _due(db, xid)
    assert _fire_one(xid, _now()) is True and sent[-1] == ("been a min. still want the gym pings? 👍 and they stay, say stop and they're gone", QUIET_CHECKIN_TYPE)
    monkeypatch.setattr(config, "REMINDER_QUIET_CHECKIN_ENABLED", False)
    y = make_user(db, user_timezone="America/Los_Angeles", phone="+15550005004")
    _inbound(db, y.id, days_ago=6)
    yid = create_reminder(y.id, "gym", "18:00", days="mon,tue,wed,thu,fri,sat,sun", source="program")["id"]
    _due(db, yid)
    assert _fire_one(yid, _now()) is True and sent[-1][1] == "reminder"
