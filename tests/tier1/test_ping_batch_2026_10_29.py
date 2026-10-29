"""
Three ping fixes from the 2026-10-28 thread watch.

1. "sip before bed" at 5pm. water_gif.pick_gif shuffled all four loops time-blind; #270
   captioned loop 4 (water-04-sleepy-stretch.gif) "sip before bed" and it went to user 51 at
   17:05 and user 77 at 17:01. The bed loop is now eligible only in the last
   WATER_GIF_BED_WINDOW_H hours before the user's sleep_time (after 21:00 local when the
   profile has no clock); the other loops rotate the rest of the day.

2. The exam gate fired on "cs70 Discussion (friday = quiz)" on a WEDNESDAY (user 51, 16:30
   water ping gated reason=exam, caught up 17:05). reminders._EXAM_TITLE_RE read the
   parenthetical note; schedule._is_deadline already knows a class-type title with a
   weekday-conditional annotation is an exam only on that weekday (#152 / #169).
   events.current_blocks now carries is_exam from that rule and the gate reads it.

3. Photon's ambiguous 502 ("dispatch may have completed") on /send-attachment: the GIF
   usually DID land, and the text fallback then doubled it (user 53, 08:01). That specific
   error now counts as sent — the GIF row is written, no text follows, WATER_GIF_AMBIGUOUS
   is logged. Any other failure still falls back to the composed line.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_water_gif import (gif_on, imessage_on, sidecar_text_ok, attachment_ok,  # noqa: F401
                                        _water_row as _gif_water_row, _out)
from tests.tier1.test_water_gate_long_blocks import _flags, _alexey, _seed, _row, PT  # noqa: F401

PTZ = ZoneInfo("America/Los_Angeles")


def test_knob_defaults():
    assert config.WATER_GIF_BED_WINDOW_ENABLED is True
    assert config.WATER_GIF_BED_WINDOW_H == 3
    assert config.EXAM_GATE_TITLE_AWARE_ENABLED is True
    assert config.WATER_GIF_AMBIGUOUS_AS_SENT_ENABLED is True


# ─── 1. the bed loop keeps to the evening ───────────────────────────────────────

def _at(h, mi=0):
    return datetime(2026, 10, 28, h, mi, tzinfo=PTZ).astimezone(timezone.utc).replace(tzinfo=None)


def _picks(user, now, counts=range(0, 24)):
    from types import SimpleNamespace as NS
    from water_gif import pick_gif
    return [pick_gif(NS(sent_count=c, user_id=user.id, source="offer", text="drink water"), user.id,
                     user=user, now=now) for c in counts]


def test_the_bed_loop_never_shows_in_the_afternoon(db):
    from water_gif import BED_GIF_HINT
    user = make_user(db, sleep_time="23:00")
    picks = _picks(user, _at(17, 5))
    assert picks and not any(BED_GIF_HINT in p for p in picks), picks
    assert len(set(picks)) == 3, "the other three loops still rotate"
    assert all(a != b for a, b in zip(picks, picks[1:])), "never the same file twice in a row"


def test_the_bed_loop_shows_in_the_last_hours_before_bed(db):
    from water_gif import BED_GIF_HINT
    user = make_user(db, sleep_time="23:00")
    assert any(BED_GIF_HINT in p for p in _picks(user, _at(21, 30)))
    assert any(BED_GIF_HINT in p for p in _picks(user, _at(20, 0)))           # 3h window opens at 20:00
    assert not any(BED_GIF_HINT in p for p in _picks(user, _at(19, 45)))


def test_no_parseable_bedtime_means_after_9pm(db):
    from water_gif import BED_GIF_HINT
    user = make_user(db, sleep_time="late")
    assert not any(BED_GIF_HINT in p for p in _picks(user, _at(20, 30)))
    assert any(BED_GIF_HINT in p for p in _picks(user, _at(21, 30)))


def test_after_midnight_bedtime_counts_as_the_same_evening(db):
    from water_gif import BED_GIF_HINT
    user = make_user(db, sleep_time="01:00")
    assert any(BED_GIF_HINT in p for p in _picks(user, _at(23, 30)))
    assert not any(BED_GIF_HINT in p for p in _picks(user, _at(17, 0)))


def test_without_a_user_or_with_the_flag_off_every_loop_is_eligible(db, monkeypatch):
    from types import SimpleNamespace as NS
    from water_gif import pick_gif, BED_GIF_HINT
    rows = [NS(sent_count=c, user_id=7, source="offer", text="drink water") for c in range(0, 24)]
    assert any(BED_GIF_HINT in pick_gif(r, 7) for r in rows)
    monkeypatch.setattr(config, "WATER_GIF_BED_WINDOW_ENABLED", False)
    user = make_user(db, sleep_time="23:00")
    assert any(BED_GIF_HINT in p for p in _picks(user, _at(17, 0)))


def test_the_reminder_path_passes_the_user(db, gif_on, imessage_on, attachment_ok, anthropic_stub, monkeypatch):
    """send_water_gif hands pick_gif the user, so the evening rule applies on a real ping."""
    import water_gif
    seen = {}
    real = water_gif.pick_gif
    def spy(row, user_id=None, **kw):
        seen.update(kw)
        return real(row, user_id, **kw)
    monkeypatch.setattr(water_gif, "pick_gif", spy)
    u = make_user(db, preferred_channel="imessage", sleep_time="23:00")
    row = _gif_water_row(db, u)
    assert water_gif.send_water_gif(u, row) is True
    assert seen.get("user") is not None


# ─── 2. the exam gate reads the title the way the schedule does ──────────────────

def test_a_discussion_with_a_friday_quiz_note_is_not_an_exam_on_tuesday(db):
    from reminders import _interval_gate
    u = _alexey(db)
    _seed(db, u, events=[("cs70 Discussion (friday = quiz)", PT(14), PT(15))])
    g = _interval_gate(_row(db, u.id), PT(14, 30))
    assert g["reason"] == "calendar_block", g                 # a short class still gates as a class, never as an exam


def test_a_long_lab_with_a_friday_quiz_note_does_not_gate_on_tuesday(db):
    from reminders import _interval_gate
    u = _alexey(db)
    _seed(db, u, events=[("Physics Lab (friday = quiz)", PT(12), PT(15))])
    assert _interval_gate(_row(db, u.id), PT(13)) == {"reason": None, "until": None}


def test_the_same_note_is_an_exam_on_friday(db):
    from reminders import _interval_gate
    fri = lambda h, m=0: datetime(2026, 10, 30, h + 7, m)       # Fri 2026-10-30 PT
    u = _alexey(db)
    _seed(db, u, events=[("Physics Lab (friday = quiz)", fri(12), fri(15))])
    assert _interval_gate(_row(db, u.id), fri(13)) == {"reason": "exam", "until": fri(15)}


def test_a_real_midterm_title_still_gates_and_current_blocks_carries_is_exam(db):
    from events import current_blocks
    from reminders import _interval_gate
    u = _alexey(db)
    _seed(db, u, events=[("PHYSICS 112 Midterm 2", PT(14), PT(17)), ("cs70 Discussion (friday = quiz)", PT(14), PT(15))])
    blocks = {b["title"]: b for b in current_blocks(u.id, now=PT(14, 30))}
    assert blocks["PHYSICS 112 Midterm 2"]["is_exam"] is True
    assert blocks["cs70 Discussion (friday = quiz)"]["is_exam"] is False
    assert _interval_gate(_row(db, u.id), PT(14, 30))["reason"] == "exam"


def test_a_stubbed_block_without_is_exam_keeps_the_regex(db, monkeypatch):
    """Older callers / stubs hand dicts without the key: the title regex still decides."""
    from reminders import _interval_gate
    u = _alexey(db)
    monkeypatch.setattr("events.current_blocks", lambda uid, now=None: [{"start": PT(14), "end": PT(15), "title": "CS 70 quiz", "minutes": 60}])
    assert _interval_gate(_row(db, u.id), PT(14, 30))["reason"] == "exam"


def test_title_aware_flag_off_is_the_old_regex(db, monkeypatch):
    from reminders import _interval_gate
    monkeypatch.setattr(config, "EXAM_GATE_TITLE_AWARE_ENABLED", False)
    u = _alexey(db)
    _seed(db, u, events=[("Physics Lab (friday = quiz)", PT(12), PT(15))])
    assert _interval_gate(_row(db, u.id), PT(13))["reason"] == "exam"


# ─── 3. the ambiguous 502 counts as sent ───────────────────────────────────────

AMBIGUOUS = RuntimeError('sidecar /send-attachment 502: {"error":"dispatch may have completed; status unknown"}')


def test_ambiguous_dispatch_error_writes_the_gif_row_and_sends_no_text(db, gif_on, imessage_on, monkeypatch,
                                                                       sidecar_text_ok, anthropic_stub, caplog):
    import sms
    from reminders import fire_due
    from water_gif import MARKER
    monkeypatch.setattr(sms, "_send_imessage_attachment", lambda *a, **k: (_ for _ in ()).throw(AMBIGUOUS))
    anthropic_stub.reply_with(lambda kw: "water break, go")
    u = make_user(db, preferred_channel="imessage")
    row = _gif_water_row(db, u)
    with caplog.at_level(logging.INFO):
        assert fire_due(now=row.fire_at) == 1
    assert sidecar_text_ok == []
    rows = _out(db, u.id)
    assert [(r.message_type, r.body) for r in rows] == [("water_reminder", MARKER)]
    assert "WATER_GIF_AMBIGUOUS" in caplog.text and "WATER_GIF_FAILED" not in caplog.text


def test_a_plain_502_still_falls_back_to_the_line(db, gif_on, imessage_on, monkeypatch, sidecar_text_ok,
                                                   anthropic_stub, caplog):
    import sms
    from reminders import fire_due
    monkeypatch.setattr(sms, "_send_imessage_attachment",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sidecar /send-attachment 502: nope")))
    anthropic_stub.reply_with(lambda kw: "water break, go")
    u = make_user(db, preferred_channel="imessage")
    row = _gif_water_row(db, u)
    with caplog.at_level(logging.INFO):
        assert fire_due(now=row.fire_at) == 1
    assert sidecar_text_ok == [(u.phone, "water break, go")] and "WATER_GIF_FAILED" in caplog.text


def test_ambiguous_flag_off_falls_back_as_before(db, gif_on, imessage_on, monkeypatch, sidecar_text_ok, anthropic_stub):
    import sms
    from reminders import fire_due
    monkeypatch.setattr(config, "WATER_GIF_AMBIGUOUS_AS_SENT_ENABLED", False)
    monkeypatch.setattr(sms, "_send_imessage_attachment", lambda *a, **k: (_ for _ in ()).throw(AMBIGUOUS))
    anthropic_stub.reply_with(lambda kw: "water break, go")
    u = make_user(db, preferred_channel="imessage")
    row = _gif_water_row(db, u)
    assert fire_due(now=row.fire_at) == 1
    assert sidecar_text_ok == [(u.phone, "water break, go")]
