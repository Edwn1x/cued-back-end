"""
Interval (water) reminders honour TODAY's observed wake, not just the stated one.

Live 2026-10-27 (user 51, stated wake 12:30 / bed 03:00): he texted at 11:01 and chatted till
11:32; the 12:30 water slot — anchored on his stated wake minute — was gated `quiet_hours`
and pushed to 14:30, then 14:30 fell inside the 90-min calendar block before his 15:30
lecture → the first ping of the day landed at 16:30. `_interval_gate_reason` called
`_in_standing_quiet_hours(user, now=now)` with NO session, so the layered wake model
(wake_model.resolve_wake needs one) never saw the 11:01 text and the window ran to the
stated wake+15 = 12:45. The heartbeat passes the session; the reminder gate now does too.
"""
import logging
from datetime import timedelta

import pytest

from tests.tier1.test_wake_model import _late_riser, _text_in, _fresh, _utc, _naive, _local, model_on  # noqa: F401


@pytest.fixture
def gates_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "WATER_REMINDER_GATES_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "QUIET_HOURS_FROM_PROFILE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "WATER_GIF_ENABLED", False, raising=False)
    import reminders
    monkeypatch.setattr("events.calendar_block_soon", lambda uid, now=None: False, raising=False)
    yield


def test_gate_sees_todays_activity_wake_with_a_session(db, model_on, gates_on):
    """Stated wake 12:00; a text at 11:00. At 12:00 (the stated wake minute, inside the old
    wake+15 window) the gate is OPEN with a session and still 'quiet_hours' without one."""
    from reminders import _interval_gate_reason
    user = _late_riser(db)
    _text_in(db, user.id, 11, 0, "up, what's the plan")
    s, u = _fresh(db, user)
    try:
        now = _naive(_utc(12, 0))
        assert _interval_gate_reason(u, now, session=s) is None
        assert _interval_gate_reason(u, now) == "quiet_hours"          # the live path, pre-fix
        # a real quiet instant is still gated either way
        assert _interval_gate_reason(u, _naive(_utc(3, 0)), session=s) == "quiet_hours"
    finally:
        s.close()


def test_fire_one_sends_the_wake_minute_slot_after_an_early_text(db, model_on, gates_on, monkeypatch, caplog):
    """End to end: an every-2h row due at the stated wake minute fires instead of re-arming."""
    import reminders
    from models import Reminder
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append((phone, body)) or True)
    user = _late_riser(db)
    _text_in(db, user.id, 11, 0, "up")
    row = Reminder(user_id=user.id, text="drink water", local_time="12:00", every_hours=2,
                   fire_at=_naive(_utc(12, 0)), active=True, source="model")
    db.add(row)
    db.commit()
    with caplog.at_level(logging.INFO):
        ok = reminders._fire_one(row.id, _naive(_utc(12, 0)))
    assert ok is True and sent, "the slot on the wake minute fires when they are already up"
    assert "REMINDER_GATED" not in caplog.text
    db.refresh(row)
    assert row.fire_at == _naive(_utc(14, 0)) and row.sent_count == 1


def test_fire_one_still_defers_a_truly_quiet_slot(db, model_on, gates_on, monkeypatch, caplog):
    import reminders
    from models import Reminder
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append(1) or True)
    user = _late_riser(db)                       # no activity today; stated wake 12:00
    row = Reminder(user_id=user.id, text="drink water", local_time="12:00", every_hours=2,
                   fire_at=_naive(_utc(12, 0)), active=True, source="model")
    db.add(row)
    db.commit()
    with caplog.at_level(logging.INFO):
        ok = reminders._fire_one(row.id, _naive(_utc(12, 0)))
    assert ok is False and not sent
    assert "REMINDER_GATED" in caplog.text and "reason=quiet_hours" in caplog.text
