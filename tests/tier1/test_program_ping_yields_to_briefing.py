"""
The program's standing "gym" ping yields to a morning briefing that is still owed.

Live 2026-10-30 (user 77, Alexey): wake model 08:44 → quiet till 08:59 → the quiet-gated
"gym time" catch-up landed at 09:04 (quiet-end + 5). A fired `reminder` is a PROACTIVE
type, so engagement_tracker.has_unanswered_proactive held every heartbeat tick (09:05 /
09:50 / 10:36 → guardrail:proactive_stack) for 180 min while the 90-min morning window
(08:44–10:14) closed underneath. No briefing. Same shape as user 50 on 2026-10-12 (a setup
water offer at wake+23 → proactive_stack every tick), fixed then by the setup sweep yielding
to the briefing, and as the water GIF's first slot (reminders._first_slot_waits_for_briefing).

Fix, mirroring the water rule: while _morning_open_signal still renders, a source='program'
ping re-arms a few minutes out instead of sending (the briefing tick is free to go first);
the sent briefing — which names today's session — then folds the HELD ping (today's
occurrence is skipped). A ping that is not held (a normal 7:30 briefing / 8:00 "gym" day)
is untouched. If no briefing ever comes, the ping fires when the window closes, within the
existing lateness tolerance (PROGRAM_REMINDER_QUIET_CATCHUP_MIN of its slot), else today's
occurrence is skipped as noise. User-asked reminders never wait — they named the time.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user

PT = ZoneInfo("America/Los_Angeles")
DAY = (2026, 10, 7)   # a Wednesday


def _local(h, mi=0, *, day=DAY):
    return datetime(*day, h, mi, tzinfo=PT)


def _naive(aware):
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def _program_row(db, user, slot_hhmm: str, fire_local, *, source="program", days="wed", text="gym"):
    from models import Reminder
    row = Reminder(user_id=user.id, text=text, local_time=slot_hhmm, recur_days=days,
                   fire_at=_naive(fire_local), active=True, source=source)
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def morning_on(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    for flag in ("HEARTBEAT_ENABLED", "HEARTBEAT_RHYTHM_ENABLED", "HEARTBEAT_STANDING_QUIET_ENABLED",
                 "QUIET_HOURS_FROM_PROFILE_ENABLED", "CALENDAR_ASSISTANT_ENABLED", "CALENDAR_DAILY_BRIEFING_ENABLED",
                 "PROGRAM_REMINDER_QUIET_GATE_ENABLED"):
        monkeypatch.setattr(config, flag, True, raising=False)
    for flag in ("WEATHER_ENABLED", "WATER_GIF_ENABLED"):
        monkeypatch.setattr(config, flag, False, raising=False)
    monkeypatch.setattr("events.current_blocks", lambda uid, now=None: [], raising=False)
    yield


def _fire(monkeypatch, row, when_local):
    import reminders
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append(body) or True)
    ok = reminders._fire_one(row.id, _naive(when_local))
    return ok, sent


# ─── knobs ────────────────────────────────────────────────────────────────────

def test_knob_defaults():
    assert config.PROGRAM_REMINDER_YIELDS_TO_BRIEFING_ENABLED is True
    assert config.PROGRAM_REMINDER_BRIEFING_WAIT_MIN == 10


# ─── the hold: a gym ping due while the briefing is still owed waits ──────────

def test_gym_ping_due_before_the_briefing_tick_waits(db, monkeypatch, morning_on, caplog):
    """Wake 07:00, "gym" slot 07:15, nobody has talked since they woke → the briefing is
    still owed: the ping re-arms WAIT_MIN out and marks itself held, nothing is sent."""
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _program_row(db, user, "07:15", _local(7, 15))
    with caplog.at_level(logging.INFO):
        ok, sent = _fire(monkeypatch, row, _local(7, 15))
    assert ok is False and not sent
    assert "REMINDER_GATED" in caplog.text and "reason=briefing_owed" in caplog.text and "source=program" in caplog.text
    db.refresh(row)
    assert row.fire_at == _naive(_local(7, 25)) and row.sent_count == 0 and row.active is True
    assert row.deferred_since == _naive(_local(7, 15))


def test_alexey_shape_quiet_catchup_landing_in_the_morning_window_waits(db, monkeypatch, morning_on):
    """Live 2026-10-30: wake 08:44 (quiet till 08:59), slot 08:00 quiet-gated to the 09:04
    catch-up. At 09:04 the briefing is owed (window 08:44–10:14) → held, not sent."""
    user = make_user(db, wake_time="08:44", sleep_time="00:30")
    row = _program_row(db, user, "08:00", _local(9, 4))
    ok, sent = _fire(monkeypatch, row, _local(9, 4))
    assert ok is False and not sent
    db.refresh(row)
    assert row.fire_at == _naive(_local(9, 14)) and row.deferred_since == _naive(_local(9, 4))


def test_a_held_ping_keeps_its_first_hold_time(db, monkeypatch, morning_on):
    """Second pass while still owed: fire_at slides again, deferred_since stays the first hold."""
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _program_row(db, user, "07:15", _local(7, 25))
    row.deferred_since = _naive(_local(7, 15))
    db.commit()
    ok, sent = _fire(monkeypatch, row, _local(7, 25))
    assert ok is False and not sent
    db.refresh(row)
    assert row.fire_at == _naive(_local(7, 35)) and row.deferred_since == _naive(_local(7, 15))


def test_once_they_have_talked_the_ping_goes_out(db, monkeypatch, morning_on):
    """A text at 07:05 → no briefing owed (a reactive thread) → the ping fires as before."""
    from models import Message
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    db.add(Message(user_id=user.id, direction="in", body="morning", created_at=_naive(_local(7, 5))))
    db.commit()
    row = _program_row(db, user, "07:15", _local(7, 15))
    ok, sent = _fire(monkeypatch, row, _local(7, 15))
    assert ok is True and len(sent) == 1
    db.refresh(row)
    assert row.sent_count == 1 and row.deferred_since is None


def test_once_the_coach_has_sent_the_briefing_the_ping_goes_out(db, monkeypatch, morning_on):
    """The normal day: briefing at 07:30 (a heartbeat outbound), gym slot 08:00 → not held,
    fires on time. The fold is only for a ping that was held (see below)."""
    from models import Message
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    db.add(Message(user_id=user.id, direction="out", body="51° and clear. legs at 8", message_type="heartbeat",
                   created_at=_naive(_local(7, 30))))
    db.commit()
    row = _program_row(db, user, "08:00", _local(8, 0))
    ok, sent = _fire(monkeypatch, row, _local(8, 0))
    assert ok is True and len(sent) == 1


def test_after_the_window_closes_a_held_ping_fires_within_the_lateness_tolerance(db, monkeypatch, morning_on):
    """Wake 07:00, slot 08:00, held through the window (closes 08:30) with no briefing sent;
    at 08:35 the morning open no longer renders and 35 min late is within CATCHUP_MIN → fire."""
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _program_row(db, user, "08:00", _local(8, 35))
    row.deferred_since = _naive(_local(8, 0))
    db.commit()
    ok, sent = _fire(monkeypatch, row, _local(8, 35))
    assert ok is True and len(sent) == 1
    db.refresh(row)
    assert row.deferred_since is None and row.sent_count == 1


def test_a_hold_that_would_run_past_the_lateness_tolerance_skips_today(db, monkeypatch, morning_on, caplog):
    """Slot 08:00, still owed at 09:35 (95 min late > CATCHUP_MIN 90): a 'gym time' that late is
    noise, not a reminder — today's occurrence is skipped, the row re-arms to next week."""
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    monkeypatch.setattr("heartbeat._morning_open_signal", lambda u, s, now=None, state=None: "owed")
    row = _program_row(db, user, "08:00", _local(9, 35))
    row.deferred_since = _naive(_local(8, 0))
    db.commit()
    with caplog.at_level(logging.INFO):
        ok, sent = _fire(monkeypatch, row, _local(9, 35))
    assert ok is False and not sent
    assert "reason=briefing_owed_too_late" in caplog.text
    db.refresh(row)
    assert row.fire_at == _naive(_local(8, 0, day=(2026, 10, 14))) and row.deferred_since is None
    assert row.sent_count == 0 and row.active is True


def test_user_asked_reminders_never_wait(db, monkeypatch, morning_on):
    """They named the time: a source='model' row at 07:15 fires with the briefing still owed."""
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _program_row(db, user, "07:15", _local(7, 15), source="model", text="take creatine")
    ok, sent = _fire(monkeypatch, row, _local(7, 15))
    assert ok is True and len(sent) == 1


def test_flag_off_sends_the_ping(db, monkeypatch, morning_on):
    monkeypatch.setattr(config, "PROGRAM_REMINDER_YIELDS_TO_BRIEFING_ENABLED", False)
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _program_row(db, user, "07:15", _local(7, 15))
    ok, sent = _fire(monkeypatch, row, _local(7, 15))
    assert ok is True and len(sent) == 1


def test_the_quiet_gate_still_comes_first(db, monkeypatch, morning_on, caplog):
    """Slot 08:00 while asleep (wake 08:44 → quiet till 08:59): the quiet gate owns the
    re-arm (quiet-end + 5), not the briefing hold."""
    user = make_user(db, wake_time="08:44", sleep_time="00:30")
    row = _program_row(db, user, "08:00", _local(8, 0))
    with caplog.at_level(logging.INFO):
        ok, sent = _fire(monkeypatch, row, _local(8, 0))
    assert ok is False and not sent
    assert "reason=quiet_hours" in caplog.text and "briefing_owed" not in caplog.text
    db.refresh(row)
    assert row.fire_at == _naive(_local(9, 4)) and row.deferred_since is None


def test_a_fired_gym_ping_arms_the_anti_stack_gate(db):
    """The bug, pinned: a sent 'gym' is message_type='reminder', a PROACTIVE type, so with no
    reply the heartbeat's anti-stack gate reads True for HEARTBEAT_STACK_WINDOW_MINUTES — every
    tick in the morning window returns proactive_stack. (The hold above keeps it from being sent.)"""
    from engagement_tracker import PROACTIVE_MESSAGE_TYPES, has_unanswered_proactive
    from models import Message
    assert "reminder" in PROACTIVE_MESSAGE_TYPES
    user = make_user(db)
    db.add(Message(user_id=user.id, direction="out", body="gym time", message_type="reminder",
                   created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)))
    db.commit()
    assert has_unanswered_proactive(user.id, config.HEARTBEAT_STACK_WINDOW_MINUTES) is True


# ─── the fold: a sent briefing covers the HELD ping ───────────────────────────

def test_fold_skips_todays_occurrence_of_a_held_ping(db, caplog):
    from reminders import fold_held_program_ping
    user = make_user(db, wake_time="08:44", sleep_time="00:30")
    row = _program_row(db, user, "08:00", _local(9, 14))
    row.deferred_since = _naive(_local(9, 4))
    db.commit()
    with caplog.at_level(logging.INFO):
        assert fold_held_program_ping(user.id, now=_naive(_local(9, 6))) == 1
    assert "PROGRAM_PING_FOLDED" in caplog.text
    db.refresh(row)
    assert row.fire_at == _naive(_local(8, 0, day=(2026, 10, 14)))
    assert row.deferred_since is None and row.sent_count == 0 and row.active is True


def test_fold_leaves_an_unheld_ping_alone(db):
    """Normal day: briefing 07:30, gym slot 08:00 not held → the 08:00 'gym' still goes out."""
    from reminders import fold_held_program_ping
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    row = _program_row(db, user, "08:00", _local(8, 0))
    assert fold_held_program_ping(user.id, now=_naive(_local(7, 30))) == 0
    db.refresh(row)
    assert row.fire_at == _naive(_local(8, 0))


def test_fold_ignores_water_rows_user_rows_and_a_stale_hold(db):
    from models import Reminder
    from reminders import fold_held_program_ping
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    assert fold_held_program_ping(user.id, now=_naive(_local(7, 30))) == 0
    water = Reminder(user_id=user.id, text="drink water", local_time="07:00", every_hours=2,
                     window_start="07:00", window_end="22:00", fire_at=_naive(_local(9, 0)), active=True,
                     source="offer", deferred_since=_naive(_local(7, 15)))
    asked = _program_row(db, user, "07:45", _local(7, 45), source="model", text="creatine")
    asked.deferred_since = _naive(_local(7, 40))
    stale = _program_row(db, user, "08:00", _local(8, 0, day=(2026, 10, 14)))
    stale.deferred_since = _naive(_local(8, 0, day=(2026, 9, 30)))   # held last week, never cleared
    db.add(water)
    db.commit()
    assert fold_held_program_ping(user.id, now=_naive(_local(7, 30))) == 0
    for r in (water, asked, stale):
        db.refresh(r)
    assert water.fire_at == _naive(_local(9, 0)) and asked.fire_at == _naive(_local(7, 45))
    assert stale.fire_at == _naive(_local(8, 0, day=(2026, 10, 14)))


# ─── the tick hook: the sent briefing folds the held ping ────────────────────

def _tick(monkeypatch, user, text, search, *, at):
    import heartbeat
    monkeypatch.setattr(heartbeat, "guardrail_reason", lambda u, s, now=None: None)
    monkeypatch.setattr(heartbeat, "decide", lambda uid, allow_skip=True: (True, text, dict(search)))
    sent = []
    monkeypatch.setattr(heartbeat, "send_sms", lambda phone, body, **kw: sent.append(body) or True)
    monkeypatch.setattr(heartbeat, "_naive_utcnow", lambda: _naive(at))
    heartbeat.heartbeat_tick(user.id)
    return sent


def test_a_sent_morning_briefing_folds_the_held_ping(db, monkeypatch):
    user = make_user(db, wake_time="08:44", sleep_time="00:30")
    row = _program_row(db, user, "08:00", _local(9, 14))
    row.deferred_since = _naive(_local(9, 4))
    db.commit()
    sent = _tick(monkeypatch, user, "56° and overcast. legs today, lab 12-7", {"morning_open": True}, at=_local(9, 5))
    assert sent
    db.refresh(row)
    assert row.fire_at == _naive(_local(8, 0, day=(2026, 10, 14))) and row.deferred_since is None


def test_an_ordinary_heartbeat_does_not_fold(db, monkeypatch):
    user = make_user(db, wake_time="08:44", sleep_time="00:30")
    row = _program_row(db, user, "08:00", _local(9, 14))
    row.deferred_since = _naive(_local(9, 4))
    db.commit()
    _tick(monkeypatch, user, "how'd the lab go", {}, at=_local(9, 5))
    db.refresh(row)
    assert row.fire_at == _naive(_local(9, 14))


def test_fold_flag_off_sends_the_briefing_and_keeps_the_ping(db, monkeypatch):
    monkeypatch.setattr(config, "PROGRAM_REMINDER_YIELDS_TO_BRIEFING_ENABLED", False)
    user = make_user(db, wake_time="08:44", sleep_time="00:30")
    row = _program_row(db, user, "08:00", _local(9, 14))
    row.deferred_since = _naive(_local(9, 4))
    db.commit()
    sent = _tick(monkeypatch, user, "56° and overcast. legs today", {"morning_open": True}, at=_local(9, 5))
    assert sent
    db.refresh(row)
    assert row.fire_at == _naive(_local(9, 14))


def test_decide_marks_a_morning_open_tick(db, monkeypatch, anthropic_stub):
    """End to end through decide(): a morning-open briefing tick sets search['morning_open']
    so heartbeat_tick knows to fold; a tick outside the window doesn't."""
    import heartbeat
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    for flag in ("HEARTBEAT_RHYTHM_ENABLED", "HEARTBEAT_STANDING_QUIET_ENABLED", "QUIET_HOURS_FROM_PROFILE_ENABLED",
                 "CALENDAR_ASSISTANT_ENABLED", "CALENDAR_DAILY_BRIEFING_ENABLED"):
        monkeypatch.setattr(config, flag, True)
    for flag in ("WEATHER_ENABLED", "HEARTBEAT_WEB_SEARCH", "HEARTBEAT_STALE_SKIP_ENABLED", "HEARTBEAT_BACKOFF_ENABLED"):
        monkeypatch.setattr(config, flag, False, raising=False)
    monkeypatch.setattr(heartbeat, "_now_aware", lambda: _local(7, 29).astimezone(timezone.utc))
    user = make_user(db, wake_time="07:00", sleep_time="23:00")
    anthropic_stub.push("51° and clear. big glass of water first. lab 12–7 today")
    spoke, payload, search = heartbeat.decide(user.id, allow_skip=False)
    assert spoke and search.get("morning_open") is True
    monkeypatch.setattr(heartbeat, "_now_aware", lambda: _local(14, 0).astimezone(timezone.utc))
    anthropic_stub.push("how's the lab going")
    spoke, payload, search = heartbeat.decide(user.id, allow_skip=False)
    assert spoke and not search.get("morning_open")
