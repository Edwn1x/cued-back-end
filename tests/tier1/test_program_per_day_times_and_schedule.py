"""
Live 2026-11-03 22:09 (user 86): a class-schedule photo + "i work 6-10 tue/thu, fri 11:30-3:30,
ping me when i'm free". The model rebuilt the program (tue/thu/fri/sat/sun) but adjust_program
takes ONE time, so the standing 'gym' row stayed at 14:00 (mid-class Tue/Thu) and the model
stacked set_reminder rows at 17:00 beside it; nothing persisted the schedule (0 events);
the photo was typed food_photo because "ate" sits inside "late".
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.factories import make_user

TZ = ZoneInfo("America/Los_Angeles")
JAMES = dict(user_timezone="America/Los_Angeles", experience="intermediate", goal="muscle_building",
             equipment="full_gym", current_split="ppl", workout_time="14:00", confirmed_workout_time="14:00",
             confirmed_training_days="tue,thu,fri,sat,sun", workout_days="tue,thu,fri,sat,sun")


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _program_rows(db, uid):
    from models import Reminder
    db.expire_all()
    return sorted((r.recur_days, r.local_time) for r in db.query(Reminder)
                  .filter(Reminder.user_id == uid, Reminder.source == "program", Reminder.active.is_(True)).all())


@pytest.fixture
def program_on(monkeypatch):
    import config
    monkeypatch.setattr(config, "PROGRAM_BUILDER_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "PROGRAM_REMINDER_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "GYM_REMINDER_HOURS_GATE_ENABLED", False)


# ─── 1. adjust_program: a time per day, and a single time that actually sticks ─────────

def test_adjust_program_times_per_day_arms_one_ping_row_per_time(db, program_on):
    from agent_tools import handle_adjust_program
    from workouts.program import apply_program, _confirm, describe_week
    from models import User
    u = make_user(db, **JAMES)
    _confirm(u.id, apply_program(u.id, weekdays=["tue", "thu", "fri", "sat", "sun"], source="test"))
    assert _program_rows(db, u.id) == [("tue,thu,fri,sat,sun", "14:00")]
    out = handle_adjust_program(u.id, {"times": {"tue": "17:00", "thu": "17:00", "fri": "17:05", "sat": "14:00", "sun": "14:00"}})
    assert out.startswith("ok:") and "tue push 5pm" in out and "sat push 2pm" in out, out
    db.expire_all()
    user = db.get(User, u.id)
    assert user.workout_times == {"by_day": {"tue": "17:00", "thu": "17:00", "fri": "17:05", "sat": "14:00", "sun": "14:00"}}
    assert user.program["times"] == user.workout_times["by_day"]
    assert _program_rows(db, u.id) == [("fri", "17:05"), ("sat,sun", "14:00"), ("tue,thu", "17:00")]
    # idempotent: a no-op adjust keeps the rows (no cancel + re-create churn)
    from models import Reminder
    ids_before = sorted(r.id for r in db.query(Reminder).filter(Reminder.user_id == u.id, Reminder.source == "program", Reminder.active.is_(True)))
    handle_adjust_program(u.id, {})
    db.expire_all()
    ids_after = sorted(r.id for r in db.query(Reminder).filter(Reminder.user_id == u.id, Reminder.source == "program", Reminder.active.is_(True)))
    assert ids_before == ids_after
    # back to one time → one row again, the per-day rows gone
    handle_adjust_program(u.id, {"time": "18:00"})
    assert _program_rows(db, u.id) == [("tue,thu,fri,sat,sun", "18:00")]


def test_a_single_new_time_lands_in_confirmed_workout_time_too(db, program_on):
    """slot_for prefers confirmed_workout_time (copied at onboarding) — the old 14:00 used to win."""
    from agent_tools import handle_adjust_program
    from workouts.program import apply_program, _confirm
    from training_time import slot_for
    from models import User
    u = make_user(db, **JAMES)
    _confirm(u.id, apply_program(u.id, weekdays=["tue", "thu"], source="test"))
    handle_adjust_program(u.id, {"time": "17:00"})
    db.expire_all()
    user = db.get(User, u.id)
    assert user.confirmed_workout_time == "17:00" and slot_for(user, "tue") == "17:00"
    assert user.program["times"] == {"tue": "17:00", "thu": "17:00"}
    assert _program_rows(db, u.id) == [("tue,thu", "17:00")]


def test_bad_times_are_errors(db, program_on):
    from agent_tools import handle_adjust_program
    u = make_user(db, **JAMES)
    assert handle_adjust_program(u.id, {"times": {"tue": "5pm-ish"}}).startswith("error:")
    assert handle_adjust_program(u.id, {"times": {"someday": "17:00"}}).startswith("error:")


def test_ping_line_and_week_line_speak_per_day_times():
    from workouts.program import _ping_line, describe_week, program_ping_groups
    prog = {"days": {"tue": "push", "thu": "pull", "fri": "legs", "sat": "push", "sun": "pull"},
            "times": {"tue": "17:00", "thu": "17:00", "fri": "17:05", "sat": "14:00", "sun": "14:00"}}
    assert program_ping_groups(prog) == [(["tue", "thu"], "17:00"), (["fri"], "17:05"), (["sat", "sun"], "14:00")]
    assert _ping_line(prog) == "i'll ping u tue/thu 5pm, fri 5:05pm, sat/sun 2pm. say 'stop the gym reminders' if u don't want that"
    assert describe_week(prog) == "tue push 5pm · thu pull 5pm · fri legs 5:05pm · sat push 2pm · sun pull 2pm"
    one = {"days": {"mon": "upper", "wed": "lower"}, "times": {"mon": "18:00", "wed": "18:00"}}
    assert describe_week(one) == "mon upper · wed lower at 6pm"


# ─── 2. the program ping still goes out in class, phrased for after ───────────

def _class_block(db, uid, start_utc, minutes, title="CE 93 lecture"):
    from events import record_event
    return record_event(uid, "scheduled", source="model", raw_text=title, title=title,
                        occurred_at=start_utc, ends_at=start_utc + timedelta(minutes=minutes))


def test_program_ping_inside_a_class_block_still_sends_as_a_heads_up(db, program_on, monkeypatch, caplog):
    """Founder 2026-11-04: "even when im in class, its nice to get reminders" — the ping goes,
    the wording points at after class, and nothing else about it changes."""
    import logging, config
    from reminders import create_reminder, _fire_one, _program_block_note
    from models import Reminder, get_session, User
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(config, "PROGRAM_REMINDER_YIELDS_TO_BRIEFING_ENABLED", False)
    monkeypatch.setattr(config, "PROGRAM_REMINDER_QUIET_GATE_ENABLED", False)
    sent = []
    import sms, reminders
    monkeypatch.setattr(sms, "send_sms", lambda phone, text, **k: sent.append(text) or {"ok": True})
    import llm_client
    monkeypatch.setattr(llm_client, "make_client", lambda: (_ for _ in ()).throw(RuntimeError("no model")))   # plain-line path
    u = make_user(db, **JAMES)
    now = _now()
    rid = create_reminder(u.id, "gym", "14:00", days="mon,tue,wed,thu,fri,sat,sun", source="program")["id"]
    db.get(Reminder, rid).fire_at = now - timedelta(minutes=1)
    db.commit()
    _class_block(db, u.id, now - timedelta(minutes=30), 70)            # class till now+40
    s = get_session()
    try:
        note = _program_block_note(s.get(User, u.id), db.get(Reminder, rid))
    finally:
        s.close()
    assert note and note[0] == "CE 93 lecture"
    assert _fire_one(rid, now) is True and len(sent) == 1
    assert sent[0].startswith("reminder: gym") and "after CE 93 lecture, ur out at" in sent[0]
    assert "REMINDER_SENT" in caplog.text and "calendar_block" not in caplog.text
    # a block that IS the gym adds nothing; no block adds nothing
    w = make_user(db, **JAMES, phone="+15550006002")
    wid = create_reminder(w.id, "gym", "14:00", days="mon,tue,wed,thu,fri,sat,sun", source="program")["id"]
    _class_block(db, w.id, now - timedelta(minutes=5), 90, title="gym — push")
    s = get_session()
    try:
        assert _program_block_note(s.get(User, w.id), db.get(Reminder, wid)) is None
    finally:
        s.close()


# ─── 3. the schedule is saved: log_event repeat_days ─────────────────────────

def test_log_event_repeat_days_writes_the_weeks_out_and_the_readers_see_it(db, caplog):
    import logging
    from agent_tools import handle_log_event
    from models import Event, get_session
    from schedule import timed_schedule
    caplog.set_level(logging.INFO)
    u = make_user(db, **JAMES)
    out = handle_log_event(u.id, {"description": "work (dining hall)", "repeat_days": ["tue", "thu"],
                                  "starts_at": "18:00", "ends_at": "22:00", "repeat_weeks": 4})
    assert out.startswith("ok: saved weekly 'work (dining hall)' tue/thu 18:00–22:00 for 4 weeks"), out
    db.expire_all()
    rows = db.query(Event).filter(Event.user_id == u.id, Event.source == "model").order_by(Event.occurred_at).all()
    assert 7 <= len(rows) <= 8 and len({r.series_id for r in rows}) == 1 and rows[0].series_id.startswith("local:")
    assert all(r.title == "work (dining hall)" and (r.ends_at - r.occurred_at) == timedelta(hours=4) for r in rows)
    assert {r.occurred_at.replace(tzinfo=timezone.utc).astimezone(TZ).strftime("%a") for r in rows} == {"Tue", "Thu"}
    assert all(r.occurred_at.replace(tzinfo=timezone.utc).astimezone(TZ).hour == 18 for r in rows)
    s = get_session()
    try:
        sched = timed_schedule(u.id, s, horizon_hours=24 * 8)
    finally:
        s.close()
    assert len(sched) >= 2                                            # the free-window readers see it
    # batch form + validation
    out = handle_log_event(u.id, {"events": [{"description": "E7", "repeat_days": ["fri"], "starts_at": "15:30", "ends_at": "17:00"},
                                             {"description": "orgo exam", "date": "tomorrow", "starts_at": "09:00"}]})
    assert "noted 'orgo exam'" in out and "saved weekly 'E7' fri" in out, out
    assert handle_log_event(u.id, {"description": "x", "repeat_days": ["tue"]}).startswith("error:")
    assert handle_log_event(u.id, {"description": "x", "repeat_days": ["someday"], "starts_at": "10:00"}).startswith("error:")


# ─── 4. the image classifier reads whole words ───────────────────────────────

@pytest.mark.parametrize("caption,expected", [
    ("This my class schedule and I work 6-10pm on Tuesday and have fraternity activities on the weekends late", "image"),
    ("ate this", "food_photo"), ("late lunch", "food_photo"), ("plated it", "image"), ("my meal", "food_photo"),
])
def test_image_caption_keywords_are_whole_words(caption, expected, monkeypatch):
    import config
    from app import classify_message
    monkeypatch.setattr(config, "NEUTRAL_IMAGE_TYPE_ENABLED", True)
    assert classify_message(caption, has_image=True) == expected


# ─── 5. the quiet-gym capability exists for the coach to cite ────────────────

def test_gym_quiet_capability_is_listed_when_beats_are_on(monkeypatch):
    import config, capabilities
    monkeypatch.setattr(config, "RSF_METER_ENABLED", True)
    monkeypatch.setattr(config, "RSF_BEATS_ENABLED", True)
    cap = next(c for c in capabilities.CAPABILITIES if c.id == "gym_quiet")
    assert cap.enabled(None) and "quiet" in cap.what
    monkeypatch.setattr(config, "RSF_BEATS_ENABLED", False)
    assert not cap.enabled(None)
