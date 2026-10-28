"""
Live 2026-10-27, user 77: asked for water pings every 2h at 11:47. His Tuesday: Phys 111A lab
12:00–19:00, INDENG 142A 12:30–14:00, a 15:00–15:50 meeting, Phys 112 DIS 19:00–20:00. The v1
gate (the heartbeat's: a block ongoing OR starting within 90 min) deferred every 2h slot to the
next grid slot — 1pm, 3pm, 5pm, 7pm all slid; first ping would have been 9pm.

v2: ongoing blocks only; a block ≥ 2h doesn't gate; an exam title always gates; a gated ping
re-arms to block end + 5 min; a ping deferred 4h fires through a short block. His day becomes
1pm → 2:05 (the INDENG lecture overlapping the lab), 3pm → 3:55 (the meeting), 5pm ✓ (lab only),
7pm → 8:05 (the discussion), 9pm ✓ — four pings instead of none.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import config
from tests.factories import make_user

# Tue 2026-10-27, PT = UTC-7
def PT(h, m=0):
    return datetime(2026, 10, 27, h + 7, m) if h + 7 < 24 else datetime(2026, 10, 28, h + 7 - 24, m)


DAY = [("Phys 111A", PT(12), PT(19)), ("INDENG 142A", PT(12, 30), PT(14)),
       ("Alexey Krivega and Julia Bautista-Corro", PT(15), PT(15, 50)), ("Phys 112 DIS", PT(19), PT(20))]


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(config, "WATER_REMINDER_GATES_ENABLED", True)
    monkeypatch.setattr(config, "INTERVAL_GATE_V2_ENABLED", True)
    monkeypatch.setattr(config, "WATER_GIF_ENABLED", False)
    # quiet hours are the heartbeat's (activity-aware) business — pin a plain 23:30→07:00 PT window here
    import heartbeat
    from zoneinfo import ZoneInfo
    def _quiet(user, *, now=None, session=None):
        loc = now.replace(tzinfo=__import__("datetime").timezone.utc).astimezone(ZoneInfo("America/Los_Angeles"))
        return (loc.hour, loc.minute) >= (23, 30) or loc.hour < 7
    monkeypatch.setattr(heartbeat, "_in_standing_quiet_hours", _quiet)


def _alexey(db, **kw):
    base = dict(name="Alexey", wake_time="07:00", sleep_time="00:00", user_timezone="America/Los_Angeles")
    base.update(kw)
    return make_user(db, **base)


def _seed(db, user, events=DAY, all_day=()):
    from models import get_session, Event
    s = get_session()
    try:
        for title, start, end in events:
            s.add(Event(user_id=user.id, event_type="scheduled", source="gcal", title=title, raw_text=title,
                        occurred_at=start, ends_at=end, all_day=False))
        for title in all_day:
            s.add(Event(user_id=user.id, event_type="scheduled", source="bcourses", title=title, raw_text=title,
                        occurred_at=PT(0), ends_at=None, all_day=True))
        s.commit()
    finally:
        s.close()


def _row(db, uid):
    from models import User
    db.expire_all()
    return db.get(User, uid)


def _water(db, user, fire_at):
    from models import get_session, Reminder
    from reminders import create_reminder
    rec = create_reminder(user.id, "drink water", None, every_hours=2, source="offer")
    s = get_session()
    try:
        r = s.get(Reminder, rec["id"]); r.fire_at = fire_at; s.commit(); return r.id
    finally:
        s.close()


# ─── the ongoing-block reader ────────────────────────────────────────────────

def test_current_blocks_is_ongoing_only_and_skips_all_day(db):
    from events import current_blocks, calendar_block_soon
    u = _alexey(db); _seed(db, u, all_day=("due: Lab 7: Op Amps 1",))
    assert current_blocks(u.id, now=PT(11)) == []                       # lab starts at noon: NOT a block yet
    assert calendar_block_soon(u.id, now=PT(11)) is True                 # …but v1's 90-min lookahead said so
    titles = [b["title"] for b in current_blocks(u.id, now=PT(13))]
    assert titles == ["INDENG 142A", "Phys 111A"]                        # newest start first
    assert current_blocks(u.id, now=PT(13))[1]["minutes"] == 420
    assert [b["title"] for b in current_blocks(u.id, now=PT(16))] == ["Phys 111A"]
    assert current_blocks(u.id, now=PT(20, 30)) == []


# ─── the gate ────────────────────────────────────────────────────────────────

def test_alexeys_tuesday_under_v2(db):
    from reminders import _interval_gate
    u = _alexey(db); _seed(db, u); row = _row(db, u.id)
    # 1pm: the 7h lab would let it through, but INDENG 142A (12:30–14:00, 90 min) overlaps → wait for it
    assert _interval_gate(row, PT(13)) == {"reason": "calendar_block", "until": PT(14)}
    assert _interval_gate(row, PT(14, 30))["reason"] is None             # lab only now
    g = _interval_gate(row, PT(15, 20)); assert g == {"reason": "calendar_block", "until": PT(15, 50)}
    assert _interval_gate(row, PT(17))["reason"] is None                 # only the long lab
    g = _interval_gate(row, PT(19, 30)); assert g == {"reason": "calendar_block", "until": PT(20)}
    assert _interval_gate(row, PT(21))["reason"] is None
    assert _interval_gate(row, PT(23, 45))["reason"] == "quiet_hours"    # sleep 00:00 − 30 min


def test_a_short_lecture_still_gates_and_a_long_block_threshold_is_configurable(db, monkeypatch):
    from reminders import _interval_gate
    u = _alexey(db); _seed(db, u, events=[("CS 70", PT(14), PT(15, 30))]); row = _row(db, u.id)
    assert _interval_gate(row, PT(14, 30)) == {"reason": "calendar_block", "until": PT(15, 30)}
    monkeypatch.setattr(config, "INTERVAL_GATE_LONG_BLOCK_MIN", 60)
    assert _interval_gate(row, PT(14, 30))["reason"] is None


def test_an_exam_always_gates_whatever_its_length(db):
    from reminders import _interval_gate
    u = _alexey(db); _seed(db, u, events=[("PHYSICS 112 Midterm 2", PT(14), PT(17))]); row = _row(db, u.id)
    assert _interval_gate(row, PT(15)) == {"reason": "exam", "until": PT(17)}


def test_max_defer_fires_through_a_short_block_but_never_quiet_hours_or_an_exam(db):
    from reminders import _interval_gate
    from types import SimpleNamespace as NS
    u = _alexey(db); _seed(db, u, events=[("CS 70", PT(14), PT(15, 30))]); row = _row(db, u.id)
    slid = NS(id=1, deferred_since=PT(10))                                # sliding since 10am
    assert _interval_gate(row, PT(14, 30), slid)["reason"] is None       # 4h30 deferred → through the lecture
    fresh = NS(id=1, deferred_since=PT(13))
    assert _interval_gate(row, PT(14, 30), fresh)["reason"] == "calendar_block"
    v = _alexey(db, phone="+15550009901"); _seed(db, v, events=[("CS 70 Midterm", PT(14), PT(17))]); vr = _row(db, v.id)
    assert _interval_gate(vr, PT(15), slid)["reason"] == "exam"
    assert _interval_gate(row, PT(23, 45), slid)["reason"] == "quiet_hours"


def test_v1_flag_restores_the_lookahead_gate(db, monkeypatch):
    monkeypatch.setattr(config, "INTERVAL_GATE_V2_ENABLED", False)
    from reminders import _interval_gate
    u = _alexey(db); _seed(db, u); row = _row(db, u.id)
    assert _interval_gate(row, PT(11))["reason"] == "calendar_block"     # 90-min lookahead
    assert _interval_gate(row, PT(13))["reason"] == "calendar_block"     # the long lab gated too


# ─── firing: catch-up at the block's end, not the next grid slot ─────────────

def test_fire_due_replays_his_day_with_catchup(db, sms_capture, anthropic_stub):
    from models import Reminder
    from reminders import fire_due
    n = {"i": 0}
    def _line(kw):                       # distinct bodies: identical texts trip the outbound dedup guard
        n["i"] += 1; return f"water check {n['i']}"
    anthropic_stub.reply_with(_line)
    u = _alexey(db); _seed(db, u)
    rid = _water(db, u, PT(13))

    def r():
        db.expire_all(); return db.get(Reminder, rid)
    assert fire_due(now=PT(13) + timedelta(seconds=10)) == 0             # 1pm: INDENG lecture → wait for 2pm
    assert r().fire_at == PT(14, 5) and r().deferred_since == PT(13) + timedelta(seconds=10)
    assert fire_due(now=PT(14, 5) + timedelta(seconds=10)) == 1          # 2:05 catch-up, inside the lab only
    assert r().sent_count == 1 and r().deferred_since is None and r().fire_at == PT(15)   # back on the grid
    assert fire_due(now=PT(15) + timedelta(seconds=10)) == 0             # 3pm: the meeting → 3:55, not 5pm
    assert r().fire_at == PT(15, 55)
    assert fire_due(now=PT(15, 55) + timedelta(seconds=10)) == 1
    assert r().sent_count == 2 and r().fire_at == PT(17)
    assert fire_due(now=PT(17) + timedelta(seconds=10)) == 1             # 5pm: lab only → sent
    assert r().fire_at == PT(19)
    assert fire_due(now=PT(19) + timedelta(seconds=10)) == 0             # 7pm: discussion → 8:05
    assert r().fire_at == PT(20, 5)
    assert fire_due(now=PT(20, 5) + timedelta(seconds=10)) == 1
    assert r().sent_count == 4 and r().fire_at == PT(21)
    # four fires in one real second trip the outbound burst guard in this harness, so the row
    # counters (sent_count 4, four re-arms) are the evidence; the first composed line did land
    assert sms_capture and sms_capture[0][1] == "water check 1"


def test_catchup_past_the_window_or_into_quiet_hours_falls_back_to_the_grid(db, sms_capture, anthropic_stub):
    from models import Reminder
    from reminders import fire_due
    anthropic_stub.reply_with(lambda kw: "water check")
    u = _alexey(db); _seed(db, u, events=[("late section", PT(22, 30), PT(23, 28))])
    rid = _water(db, u, PT(23))
    assert fire_due(now=PT(23) + timedelta(seconds=10)) == 0
    db.expire_all(); r = db.get(Reminder, rid)
    assert r.fire_at >= PT(23, 59) and r.fire_at.date() == datetime(2026, 10, 28).date()   # 23:33 is quiet → the grid (tomorrow 7am)


def test_gates_off_means_no_deferral_at_all(db, sms_capture, anthropic_stub, monkeypatch):
    monkeypatch.setattr(config, "WATER_REMINDER_GATES_ENABLED", False)
    from reminders import fire_due
    anthropic_stub.reply_with(lambda kw: "water check")
    u = _alexey(db); _seed(db, u)
    _water(db, u, PT(15))
    assert fire_due(now=PT(15) + timedelta(seconds=10)) == 1
