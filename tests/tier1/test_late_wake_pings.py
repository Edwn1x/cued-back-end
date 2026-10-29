"""
Late wakers without a wearable: the morning is held until they're usually up.

Live 2026-10-28 (user 77, Alexey): stated wake 07:00, bed 00:00 — but his first text of the
day has been 11:05 and 11:20. The 7:29 briefing, the 8:00 "gym time" (the program's standing
reminder, which goes through no quiet gate at all) and the 9:00 and 11:00 water GIFs all
went out while he slept; he woke at 11:20 ("just up?" "yeah"). The heartbeat's own tick at
10:29 reasoned "he's probably asleep" — reminders had no such idea.

The layered wake model gains one layer for people without a watch: the OBSERVED TYPICAL
wake — the EARLIEST first plausible activity (still-up guard applied, morning-only samples)
over the last few days. The earliest, not the median: a first text only says they were up
BY then, and a student who texts between classes (user 40: 07:32 / 10:47 / 12:31, up at
06:30 for school) must be held to 07:32, not 10:47. It only ever pushes the morning LATER (the #156 rule: the profile
is the base; what we know of them can hold the morning, never shorten it), and it is capped
at 14:00 like every other late-wake extension.

Consumers: the standing quiet window's morning END, the MORNING OPEN anchor, the interval
(water) gate — which now also passes its DB session so TODAY's observed wake counts (#284)
and re-arms a quiet-gated slot to the end of quiet instead of losing it to the next grid
step — and the program's "gym" reminder, which is gated like the water pings (catch-up
when quiet ends soon after its time, skipped when it doesn't). User-asked reminders ("remind
me at 6am") are untouched: they named the time.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_wake_model import (_late_riser, _text_in, _fresh, _utc, _naive, _local, PT,  # noqa: F401
                                         model_on)


@pytest.fixture
def gates_on(monkeypatch):
    monkeypatch.setattr(config, "WATER_REMINDER_GATES_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "WATER_GIF_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "INTERVAL_GATE_V2_ENABLED", True, raising=False)
    monkeypatch.setattr("events.calendar_block_soon", lambda uid, now=None: False, raising=False)
    monkeypatch.setattr("events.current_blocks", lambda uid, now=None: [], raising=False)
    yield


def _alexey(db, **kw):
    """Profile wake 07:00 / bed 00:00 (quiet 23:30 .. 07:15 under today's logic)."""
    kw.setdefault("wake_time", "07:00")
    kw.setdefault("sleep_time", "00:00")
    kw.setdefault("name", "Alexey")
    return make_user(db, **kw)


def _history(db, user_id, *days):
    """days = [(day_offset, [(h, m), ...]), ...] — inbound texts on PAST local days."""
    from models import Message
    for off, stamps in days:
        for h, m in stamps:
            db.add(Message(user_id=user_id, direction="in", body="hey",
                           created_at=_naive(_local(h, m, day_offset=off))))
    db.commit()


def _alexey_with_history(db, **kw):
    u = _alexey(db, **kw)
    # 10-27: first text 11:05. 10-28: 01:06 (still up — fails the guard), then 11:20.
    _history(db, u.id, (-2, [(11, 5), (14, 0)]), (-1, [(1, 6), (11, 20), (12, 0)]))
    return u


# ─── knobs ────────────────────────────────────────────────────────────────────

def test_knob_defaults():
    assert config.ACTIVITY_TYPICAL_WAKE_ENABLED is True
    assert config.ACTIVITY_WAKE_WINDOW_DAYS == 7
    assert config.ACTIVITY_WAKE_MIN_DAYS == 2
    assert config.ACTIVITY_WAKE_LATEST_LOCAL_HOUR == 13
    assert config.ACTIVITY_WAKE_MIN_SHIFT_MIN == 45
    assert config.PROGRAM_REMINDER_QUIET_GATE_ENABLED is True
    assert config.PROGRAM_REMINDER_QUIET_CATCHUP_MIN == 90
    assert config.INTERVAL_QUIET_CATCHUP_ENABLED is True


# ─── the layer ────────────────────────────────────────────────────────────────

def test_observed_typical_wake_is_the_earliest_first_plausible_activity(db, model_on):
    from wake_model import resolve_wake
    user = _alexey_with_history(db)
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(9, 0))        # nothing yet today
        assert info and info.source == "activity_typical", info
        assert info.local_hm == (11, 5), info              # earliest of 11:05 and 11:20; the 01:06 never counted
        assert "2 days" in info.detail
    finally:
        s.close()


def test_the_quiet_window_holds_until_they_are_usually_up(db, model_on):
    """Stated wake 07:00 → old end 07:15. With the layer: 11:20. 06:50 quiet either way."""
    from heartbeat import _in_standing_quiet_hours
    user = _alexey_with_history(db)
    s, u = _fresh(db, user)
    try:
        assert _in_standing_quiet_hours(u, now=_utc(6, 50), session=s) is True
        assert _in_standing_quiet_hours(u, now=_utc(9, 0), session=s) is True      # the 9am water GIF
        assert _in_standing_quiet_hours(u, now=_utc(11, 0), session=s) is True     # the 11am one
        assert _in_standing_quiet_hours(u, now=_utc(11, 30), session=s) is False
        assert _in_standing_quiet_hours(u, now=_utc(9, 0)) is False                # no session: today's path exactly
    finally:
        s.close()


def test_a_text_today_still_wins(db, model_on):
    """Activity today is layer 1: a 10:00 text ends quiet at 10:15 even though they usually
    surface at 11:05 — we KNOW they're up."""
    from heartbeat import _in_standing_quiet_hours
    from wake_model import resolve_wake
    user = _alexey_with_history(db)
    _text_in(db, user.id, 10, 0, "up early today")
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(10, 20))
        assert info and info.source == "activity" and info.local_hm == (10, 0)
        assert _in_standing_quiet_hours(u, now=_utc(10, 20), session=s) is False
    finally:
        s.close()


def test_afternoon_first_texts_are_not_wake_samples(db, model_on):
    """Someone who's up at 7 but only texts at 15:00 must not be held till 15:00."""
    from heartbeat import _in_standing_quiet_hours
    from wake_model import resolve_wake
    user = _alexey(db)
    _history(db, user.id, (-2, [(15, 0)]), (-1, [(15, 30)]), (-3, [(16, 0)]))
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(9, 0))
        assert info and info.source == "profile", info
        assert _in_standing_quiet_hours(u, now=_utc(9, 0), session=s) is False
    finally:
        s.close()


def test_one_sample_is_not_a_pattern(db, model_on):
    from wake_model import resolve_wake
    user = _alexey(db)
    _history(db, user.id, (-1, [(11, 20)]))
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(9, 0))
        assert info and info.source == "profile"
    finally:
        s.close()


def test_a_small_shift_is_ignored_and_an_earlier_pattern_never_shortens_the_morning(db, model_on):
    from heartbeat import _in_standing_quiet_hours
    from wake_model import resolve_wake
    late = _alexey(db, wake_time="11:00")
    _history(db, late.id, (-2, [(11, 10)]), (-1, [(11, 30)]))          # earliest 11:10: 10 min later → noise
    early = _alexey(db, wake_time="11:00")
    _history(db, early.id, (-2, [(8, 0)]), (-1, [(8, 10)]))            # usually up at 8 → profile still governs
    for user in (late, early):
        s, u = _fresh(db, user)
        try:
            info = resolve_wake(u, s, now=_utc(7, 0))
            assert info and info.source == "profile", (user.wake_time, info)
            assert _in_standing_quiet_hours(u, now=_utc(10, 0), session=s) is True     # 11:15 end, unchanged
        finally:
            s.close()


def test_only_the_last_window_days_count(db, model_on):
    from wake_model import resolve_wake
    user = _alexey(db)
    _history(db, user.id, (-9, [(11, 0)]), (-8, [(11, 30)]))              # outside the 7-day window
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(9, 0))
        assert info and info.source == "profile"
    finally:
        s.close()


def test_the_watch_outranks_the_phone_pattern(db, model_on):
    """A measured typical wake (layer 3) stays ahead of the observed one (layer 3b)."""
    from models import WearableDay
    from tests.tier1.test_wake_model import _connect_health
    from wake_model import resolve_wake
    user = _alexey_with_history(db)
    _connect_health(db, user.id)
    for i in range(0, 4):
        wake, bed = _local(9, 0, day_offset=-i), _local(1, 0, day_offset=-i)
        db.add(WearableDay(user_id=user.id, provider="google_health", day=wake.date().isoformat(),
                           sleep_minutes=480, sleep_start=_naive(bed), sleep_end=_naive(wake),
                           synced_at=_naive(wake) + timedelta(hours=20)))
    db.commit()
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(8, 0))
        assert info and info.source == "measured_typical" and info.local_hm == (9, 0)
    finally:
        s.close()


def test_flag_off_is_byte_identical_to_today(db, model_on, monkeypatch):
    from heartbeat import _in_standing_quiet_hours
    from wake_model import resolve_wake
    monkeypatch.setattr(config, "ACTIVITY_TYPICAL_WAKE_ENABLED", False)
    user = _alexey_with_history(db)
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(9, 0))
        assert info and info.source == "profile"
        assert _in_standing_quiet_hours(u, now=_utc(9, 0), session=s) is False
    finally:
        s.close()


# ─── the briefing ─────────────────────────────────────────────────────────────

def test_the_morning_open_waits_for_their_usual_wake(db, model_on):
    """7:29 (the live tick): no MORNING OPEN — quiet. 11:30: the window is open, anchored
    at 11:05."""
    from heartbeat import _morning_anchor_hhmm, _morning_open_signal, _in_standing_quiet_hours
    user = _alexey_with_history(db)
    s, u = _fresh(db, user)
    try:
        assert _in_standing_quiet_hours(u, now=_utc(7, 29), session=s) is True
        assert _morning_open_signal(u, s, now=_utc(7, 29)) is None
        assert _morning_anchor_hhmm(u, s, _utc(11, 30).astimezone(PT), now=_utc(11, 30)) == (11, 5)
        sig = _morning_open_signal(u, s, now=_utc(11, 30))
        assert sig and "11:05am wake" in sig, sig
    finally:
        s.close()


def test_quiet_end_instant(db, model_on):
    from heartbeat import quiet_end_utc
    user = _alexey_with_history(db)
    s, u = _fresh(db, user)
    try:
        assert quiet_end_utc(u, s, now=_naive(_utc(9, 0))) == _naive(_local(11, 20))
        assert quiet_end_utc(u, s, now=_naive(_utc(23, 45))) == _naive(_local(11, 20, day_offset=1))
        assert quiet_end_utc(u, s, now=_naive(_utc(12, 0))) is None          # not quiet now
    finally:
        s.close()


# ─── water pings ──────────────────────────────────────────────────────────────

def _water_row(db, user, fire_h):
    from models import Reminder
    row = Reminder(user_id=user.id, text="drink water", local_time="07:00", every_hours=2,
                   fire_at=_naive(_utc(fire_h, 0)), active=True, source="model")
    db.add(row)
    db.commit()
    return row


def test_water_slots_while_asleep_are_held_and_the_first_one_lands_when_quiet_ends(db, model_on, gates_on,
                                                                                     monkeypatch, caplog):
    """09:00: gated → the next grid slot (11:00) comes before quiet ends, so re-arm there.
    11:00: gated → quiet ends 11:20 → catch-up 11:25 (not 13:00). 11:25: sent."""
    import reminders
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append(body) or True)
    user = _alexey_with_history(db)
    row = _water_row(db, user, 9)
    with caplog.at_level(logging.INFO):
        assert reminders._fire_one(row.id, _naive(_utc(9, 0))) is False
    assert "REMINDER_GATED" in caplog.text and "reason=quiet_hours" in caplog.text
    db.refresh(row)
    assert row.fire_at == _naive(_utc(11, 0)) and not sent
    assert reminders._fire_one(row.id, _naive(_utc(11, 0))) is False
    db.refresh(row)
    assert row.fire_at == _naive(_local(11, 25)), row.fire_at
    assert reminders._fire_one(row.id, _naive(_local(11, 25))) is True
    db.refresh(row)
    assert sent and row.sent_count == 1 and row.fire_at == _naive(_utc(13, 0))


def test_quiet_catchup_flag_off_keeps_the_grid(db, model_on, gates_on, monkeypatch):
    import reminders
    monkeypatch.setattr(config, "INTERVAL_QUIET_CATCHUP_ENABLED", False)
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: True)
    user = _alexey_with_history(db)
    row = _water_row(db, user, 11)
    assert reminders._fire_one(row.id, _naive(_utc(11, 0))) is False
    db.refresh(row)
    assert row.fire_at == _naive(_utc(13, 0))


# ─── #284, ported: the gate sees TODAY's observed wake ────────────────────────

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
        assert _interval_gate_reason(u, _naive(_utc(3, 0)), session=s) == "quiet_hours"
    finally:
        s.close()


def test_fire_one_sends_the_wake_minute_slot_after_an_early_text(db, model_on, gates_on, monkeypatch, caplog):
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


# ─── the program's "gym" reminder ─────────────────────────────────────────────

def _gym_row(db, user, *, local_time="08:00", source="program"):
    from models import Reminder
    row = Reminder(user_id=user.id, text="gym", local_time=local_time, recur_days="mon,tue,wed,thu,fri,sat",
                   fire_at=_naive(_local(int(local_time[:2]), int(local_time[3:]))), active=True, source=source)
    db.add(row)
    db.commit()
    return row


def test_gym_ping_while_asleep_is_skipped_when_quiet_ends_much_later(db, model_on, gates_on, monkeypatch, caplog):
    """08:00 "gym time" with quiet till 11:20: 3.5h late is no reminder at all — skip to the
    next day's slot, say so in the log."""
    import reminders
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append(body) or True)
    user = _alexey_with_history(db)
    row = _gym_row(db, user)
    with caplog.at_level(logging.INFO):
        assert reminders._fire_one(row.id, _naive(_local(8, 0))) is False
    assert not sent and "REMINDER_GATED" in caplog.text and "reason=quiet_hours" in caplog.text
    db.refresh(row)
    assert row.fire_at == _naive(_local(8, 0, day_offset=1)) and row.sent_count == 0 and row.active


def test_gym_ping_catches_up_when_quiet_ends_soon_after(db, model_on, gates_on, monkeypatch):
    """Up by 08:55 lately (texts at 08:55 / 09:05): the 08:00 gym ping lands at 09:15 instead."""
    import reminders
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append(body) or True)
    user = _alexey(db)
    _history(db, user.id, (-2, [(8, 55)]), (-1, [(9, 5)]))
    row = _gym_row(db, user)
    assert reminders._fire_one(row.id, _naive(_local(8, 0))) is False
    db.refresh(row)
    assert row.fire_at == _naive(_local(9, 15)), row.fire_at           # 08:55 + 15 quiet + 5 catch-up
    assert reminders._fire_one(row.id, _naive(_local(9, 15))) is True
    db.refresh(row)
    assert len(sent) == 1 and row.sent_count == 1
    assert row.fire_at == _naive(_local(8, 0, day_offset=1))


def test_a_user_asked_reminder_is_never_gated(db, model_on, gates_on, monkeypatch):
    """'remind me at 8' is theirs: it fires at 8 whatever the model thinks of their morning."""
    import reminders
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append(body) or True)
    user = _alexey_with_history(db)
    row = _gym_row(db, user, source="model")
    assert reminders._fire_one(row.id, _naive(_local(8, 0))) is True
    assert sent and len(sent) == 1


def test_gym_gate_flag_off_sends_as_today(db, model_on, gates_on, monkeypatch):
    import reminders
    monkeypatch.setattr(config, "PROGRAM_REMINDER_QUIET_GATE_ENABLED", False)
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append(body) or True)
    user = _alexey_with_history(db)
    row = _gym_row(db, user)
    assert reminders._fire_one(row.id, _naive(_local(8, 0))) is True and sent


def test_gym_ping_outside_quiet_is_untouched(db, model_on, gates_on, monkeypatch):
    import reminders
    sent = []
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: sent.append(body) or True)
    user = _alexey_with_history(db)
    row = _gym_row(db, user, local_time="18:00")
    assert reminders._fire_one(row.id, _naive(_local(18, 0))) is True and sent


def test_a_student_who_texts_between_classes_is_held_to_his_earliest(db, model_on):
    """Live user 40: up 06:30 for school, first texts 12:29 / 10:47 / 15:35 / 12:31 / 07:32.
    The earliest (07:32) is the only sample safe to hold a morning on; a median (10:47)
    would have gated his water pings through four hours of school."""
    from heartbeat import _in_standing_quiet_hours
    from wake_model import resolve_wake
    user = _alexey(db, wake_time="06:30", sleep_time="23:00")
    _history(db, user.id, (-5, [(12, 29)]), (-4, [(10, 47)]), (-3, [(15, 35)]), (-2, [(12, 31)]), (-1, [(7, 32)]))
    s, u = _fresh(db, user)
    try:
        info = resolve_wake(u, s, now=_utc(7, 0))
        assert info and info.source == "activity_typical" and info.local_hm == (7, 32), info
        assert _in_standing_quiet_hours(u, now=_utc(7, 40), session=s) is True      # 07:47 end
        assert _in_standing_quiet_hours(u, now=_utc(8, 0), session=s) is False
    finally:
        s.close()
