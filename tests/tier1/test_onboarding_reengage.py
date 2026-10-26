"""Live 2026-10-15/16: 15 sign-ups in one night, 1 finished setup, 11 stalled mid-intake and
nothing contacted them again for four days (the heartbeat serves step-3 users only). The sweep
sends at most two nudges (~20h, ~72h), inside 10:00–21:00 local, each ending in the next ask."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from tests.factories import make_user

PT_NOON_UTC = datetime(2026, 10, 20, 19, 0)      # 12:00 PT, a Tuesday
PT_2AM_UTC = datetime(2026, 10, 20, 9, 0)        # 02:00 PT


def _msg(uid, direction, body, ago_h, mtype="onboarding"):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=uid, direction=direction, body=body, channel="imessage", message_type=mtype,
                      created_at=PT_NOON_UTC - timedelta(hours=ago_h)))
        s.commit()
    finally:
        s.close()


def _stalled(db, *, replied=True, ago_h=24, **kw):
    """A step-2 user whose hook went out and (optionally) who answered once, `ago_h` hours ago."""
    base = dict(onboarding_step=2, preferred_channel="imessage", user_timezone="America/Los_Angeles",
                occupation=None, activity_level=None, avg_steps=None, cooking_situation=None, injuries=None,
                wake_time=None, sleep_time=None, workout_days="3", current_split="none")
    base.update(kw)
    u = make_user(db, **base)
    _msg(u.id, "out", "hey, it's cued. how's ur day going", ago_h + 1)
    if replied:
        _msg(u.id, "in", "good!", ago_h + 0.5)
        _msg(u.id, "out", "nice. im assuming ur a berkeley student? what major", ago_h)
    return u


def _sent(sms_capture):
    return [b for _p, b in sms_capture]


def _pin_nudge(uid, at):
    """The nudge the sweep just sent is stamped with the REAL clock (send_sms), while every
    other timestamp here is relative to the fake `now`. Pin it to the fake clock so the
    72h gap is measured from the sweep's `now`, whatever day the suite runs on (it went red
    on main on 2026-10-22, two days after the fixed date)."""
    from models import get_session, Message
    s = get_session()
    try:
        for m in s.query(Message).filter(Message.user_id == uid, Message.direction == "out",
                                         Message.created_at > PT_NOON_UTC).all():
            m.created_at = at
        s.commit()
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_FIRST_HOURS", 20)
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_SECOND_HOURS", 72)
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_MODEL_COPY_ENABLED", False)   # the template path; model copy has its own file


def test_first_nudge_after_20h_quiet_names_what_is_left_and_asks(db, sms_capture):
    import onboarding_reengage as r
    u = _stalled(db, ago_h=24)
    assert r.sweep(now=PT_NOON_UTC) == 1
    out = _sent(sms_capture)
    # founder's structure: why they signed up → starting is the hard part, i'm here → the ONE ask: today's food
    assert len(out) == 1 and out[0].startswith("hey Test, u signed up to"), out[0]
    assert "getting started is always the hard part" in out[0] and "lock in" in out[0]
    assert "eaten so far today?" in out[0]                        # 12:00 PT → the food ask
    assert "height" not in out[0] and "quick things left" not in out[0]   # no setup questions, no field count


def test_never_replied_user_gets_the_open_door_variant(db, sms_capture):
    import onboarding_reengage as r
    u = _stalled(db, replied=False, ago_h=30)
    assert r.sweep(now=PT_NOON_UTC) == 1
    out = _sent(sms_capture)[0]
    assert out.startswith("hey Test, it's cued") and "u signed up to" in out and "?" in out
    assert "eaten" in out and "height" not in out


def test_too_soon_sends_nothing(db, sms_capture):
    import onboarding_reengage as r
    _stalled(db, ago_h=6)
    assert r.sweep(now=PT_NOON_UTC) == 0 and sms_capture == []


def test_quiet_hours_defer_and_do_not_consume_the_nudge(db, sms_capture):
    import onboarding_reengage as r
    _stalled(db, ago_h=24)
    assert r.sweep(now=PT_2AM_UTC) == 0 and sms_capture == []
    assert r.sweep(now=PT_NOON_UTC) == 1


def test_second_nudge_only_72h_after_the_first_and_never_a_third(db, sms_capture):
    import onboarding_reengage as r
    u = _stalled(db, ago_h=24)
    assert r.sweep(now=PT_NOON_UTC) == 1
    _pin_nudge(u.id, PT_NOON_UTC)
    assert r.sweep(now=PT_NOON_UTC + timedelta(hours=24)) == 0          # too soon for #2
    assert r.sweep(now=PT_NOON_UTC + timedelta(hours=73)) == 1          # #2
    assert "last one from me" in _sent(sms_capture)[-1]
    assert r.sweep(now=PT_NOON_UTC + timedelta(hours=73 + 200)) == 0    # never a third
    assert len(_sent(sms_capture)) == 2


def test_a_reply_after_the_first_nudge_stops_the_second(db, sms_capture):
    import onboarding_reengage as r
    u = _stalled(db, ago_h=24)
    assert r.sweep(now=PT_NOON_UTC) == 1
    _pin_nudge(u.id, PT_NOON_UTC)
    _msg(u.id, "in", "yeah lets do it", ago_h=-1)       # one hour after the nudge
    assert r.sweep(now=PT_NOON_UTC + timedelta(hours=100)) == 0


@pytest.mark.parametrize("kw", [dict(onboarding_step=3), dict(opted_out=True), dict(waitlist_status="pending")])
def test_onboarded_opted_out_and_waitlisters_are_never_nudged(db, sms_capture, kw):
    import onboarding_reengage as r
    _stalled(db, ago_h=48, **kw)
    assert r.sweep(now=PT_NOON_UTC) == 0 and sms_capture == []


def test_archived_users_are_skipped(db, sms_capture):
    import onboarding_reengage as r
    _stalled(db, ago_h=48, archived_at=PT_NOON_UTC - timedelta(days=1))
    assert r.sweep(now=PT_NOON_UTC) == 0


def test_the_nudge_is_typed_so_it_never_counts_as_a_coach_turn(db, sms_capture):
    import onboarding_reengage as r
    from models import get_session, Message
    u = _stalled(db, ago_h=24)
    r.sweep(now=PT_NOON_UTC)
    s = get_session()
    try:
        row = s.query(Message).filter(Message.user_id == u.id, Message.direction == "out").order_by(Message.id.desc()).first()
        assert row.message_type == r.MESSAGE_TYPE
    finally:
        s.close()


def test_flag_off(db, sms_capture, monkeypatch):
    import onboarding_reengage as r
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_ENABLED", False)
    _stalled(db, ago_h=48)
    assert r.sweep(now=PT_NOON_UTC) == 0


def test_start_at_holds_the_sweep_until_the_instant_and_then_sends(db, sms_capture, monkeypatch):
    """Founder 2026-10-25: 'have the first nudges go out tmr morning, like 10am' — flag on,
    sweep armed, nothing before the instant; the first tick at/after it sends."""
    import onboarding_reengage as r
    _stalled(db, ago_h=24)
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_START_AT", "2026-10-26T17:00:00Z")      # 10:00 PT
    assert r.sweep(now=datetime(2026, 10, 26, 16, 30)) == 0 and sms_capture == []
    assert r.sweep(now=datetime(2026, 10, 26, 17, 0)) == 1
    monkeypatch.setattr(config, "ONBOARDING_REENGAGE_START_AT", "garbage")
    assert r.start_at() is None                                                              # unreadable → no gate
