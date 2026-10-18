"""
Jayden's first night (user 40, 2026-10-17 22:00 → 2026-10-18 00:58 PT) — the follow-ups.

  1. 22:42 "Ughhh I don't want toooo" to the gym reminder he asked for → "lol ok don't
     then / it's late anyway, go tomorrow (it's on the books)". Nothing was on the books.
     → voice rule for in-the-moment bailing + a code re-arm of a bailed one-off gym reminder.
  2. 00:54 "I also did abdominal crunch 100x10 • 100x10" one minute after closing the card
     → legacy row, session untouched, summary 2,000 lb short.
     → a straggler lands in the session they just closed.
  3. 00:58 a walk fact evicted "gets a coffee every morning" from `schedule`.
     → food habits reclassify from schedule too; nightly sweep closes typed-column dups.
  4. The summary said 118 min for a ~55-min workout; "shoulder press" stored as the barbell
     overhead press on a machines user; no line followed a first-ever workout.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import FOUNDER, imessage_on, sidecar_ok, card_ok, sync_threads, _sets  # noqa: F401
from tests.tier1.test_food_preferences_and_typed_dups import JAYDEN_COLS


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


# ── 1. bailing on a self-scheduled session ───────────────────────────────────

def test_voice_carries_the_in_the_moment_rule():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    for phrase in ("The moment they try to bail on a session THEY set up", 'does NOT mean "ok don\'t then"',
                   "ONE warm push, then the smallest version", "Never \"it's late anyway\" when it's their normal time",
                   "set_reminder for the same time tomorrow and SAY you did", "Never claim it's \"on the books\""):
        assert phrase in v, phrase


def _fired_one_off(db, user, text="go to the gym", hours_ago=3.5):
    """A one-off reminder that fired `hours_ago` and is now inactive (as fire_due leaves it)."""
    from models import Reminder
    from reminders import create_reminder
    rec = create_reminder(user.id, text, "22:00")
    db.expire_all()
    row = db.get(Reminder, rec["id"])
    row.active = False
    row.last_sent_at = _now() - timedelta(hours=hours_ago)
    row.sent_count = 1
    db.commit()
    return row.id


def _active(db, uid):
    from models import Reminder
    db.expire_all()
    return db.query(Reminder).filter(Reminder.user_id == uid, Reminder.active.is_(True)).order_by(Reminder.id).all()


def test_a_bailed_gym_reminder_rearms_once_for_tomorrow(db, caplog):
    from reminders import rearm_bailed_gym_reminders
    u = make_user(db, user_timezone="America/Los_Angeles")
    rid = _fired_one_off(db, u)
    caplog.set_level(logging.INFO)
    assert rearm_bailed_gym_reminders(_now()) == 1
    rows = _active(db, u.id)
    assert len(rows) == 1 and rows[0].source == "rearm" and rows[0].text == "go to the gym" and rows[0].local_time == "22:00"
    assert rows[0].recur_days is None and rows[0].fire_at > _now()
    assert "REMINDER_GYM_REARMED" in caplog.text
    # once: the re-arm is never re-armed, and a second sweep adds nothing
    assert rearm_bailed_gym_reminders(_now()) == 0 and len(_active(db, u.id)) == 1
    # the coach can now truthfully say it's on the books
    from reminders import context_block
    from models import get_session
    s = get_session()
    try:
        blk = context_block(_u(u.id), s)
    finally:
        s.close()
    assert "pending:" in blk and "go to the gym" in blk


def test_rearm_waits_three_hours_and_skips_when_they_actually_went(db):
    from reminders import rearm_bailed_gym_reminders
    from workouts.start import start_workout_session
    early = make_user(db, user_timezone="America/Los_Angeles")
    _fired_one_off(db, early, hours_ago=1)                       # still inside the window — they may yet go
    assert rearm_bailed_gym_reminders(_now()) == 0
    went = make_user(db, user_timezone="America/Los_Angeles", **FOUNDER, phone="+15550007777")
    _fired_one_off(db, went, hours_ago=4)
    start_workout_session(went.id, "push")                        # started after the reminder fired
    assert rearm_bailed_gym_reminders(_now()) == 0 and _active(db, went.id) == []


def test_rearm_ignores_non_gym_water_and_recurring_and_the_flag(db, monkeypatch):
    from reminders import rearm_bailed_gym_reminders, create_reminder
    from models import Reminder
    u = make_user(db, user_timezone="America/Los_Angeles")
    _fired_one_off(db, u, text="call mom", hours_ago=4)
    assert rearm_bailed_gym_reminders(_now()) == 0
    v = make_user(db, user_timezone="America/Los_Angeles", phone="+15550007778")
    rec = create_reminder(v.id, "go to the gym", "22:00", days="mon,wed")
    db.expire_all(); row = db.get(Reminder, rec["id"]); row.last_sent_at = _now() - timedelta(hours=4); db.commit()
    assert rearm_bailed_gym_reminders(_now()) == 0                # recurring re-arms itself
    w = make_user(db, user_timezone="America/Los_Angeles", phone="+15550007779")
    _fired_one_off(db, w, hours_ago=4)
    monkeypatch.setattr(config, "REMINDER_GYM_REARM_ENABLED", False)
    assert rearm_bailed_gym_reminders(_now()) == 0 and _active(db, w.id) == []


def test_fire_due_runs_the_rearm_sweep(db, monkeypatch):
    from reminders import fire_due
    import reminders
    called = []
    monkeypatch.setattr(reminders, "rearm_bailed_gym_reminders", lambda now=None: called.append(now) or 0)
    fire_due(now=_now())
    assert len(called) == 1


# ── 2. stragglers land in the session they just closed ───────────────────────

def _close(db, user, key="push"):
    from workouts.start import start_workout_session
    from workouts.session_ops import apply_text_update, close_session
    sid = start_workout_session(user.id, key)["session_id"]
    apply_text_update(user.id, "190 x4")
    close_session(sid, via="text", fill_as_planned=True)
    return sid


def _legacy_rows(db, uid):
    from models import Workout
    db.expire_all()
    return db.query(Workout).filter(Workout.user_id == uid, Workout.deleted_at.is_(None)).order_by(Workout.id).all()


def test_live_user_40_crunches_a_minute_after_closing_land_in_that_session(db, imessage_on, sidecar_ok, card_ok, sync_threads, caplog):
    from agent_tools import handle_log_workout
    from models import get_session, WorkoutSession
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _close(db, user)
    before = next(w for w in _legacy_rows(db, user.id) if w.user_notes == f"card session #{sid}")
    n_before = len(_legacy_rows(db, user.id))
    db.expire_all(); vol_before = db.get(WorkoutSession, sid).total_volume_lb
    caplog.set_level(logging.INFO)
    out = handle_log_workout(user.id, {"split_day": "push", "exercises": [{"name": "abdominal crunch", "sets": 2, "reps": 10, "weight": 100}]})
    assert out.startswith("ok: added 2 sets to the session they just closed") and f"#{sid}" in out, out
    assert "LOG_WORKOUT_INTO_CLOSED_SESSION" in caplog.text
    rows = [r for r in _sets(db, sid) if r[0] == "abdominal_crunch"]
    assert len(rows) == 2 and all(r[2] and r[3] == 100 and r[4] == 10 and r[5] == "text" for r in rows)
    db.expire_all()
    ws = db.get(WorkoutSession, sid)
    assert ws.status == "done" and ws.total_volume_lb == vol_before + 2000
    assert len(_legacy_rows(db, user.id)) == n_before                    # no second legacy row
    after = next(w for w in _legacy_rows(db, user.id) if w.id == before.id)
    assert any(e["name"] == "abdominal crunch" and e["detail"] == [[100.0, 10], [100.0, 10]] for e in after.exercises)


def test_outside_the_window_or_with_it_off_the_legacy_append_stands(db, imessage_on, sidecar_ok, card_ok, sync_threads, monkeypatch):
    from agent_tools import handle_log_workout
    from models import get_session, WorkoutSession
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _close(db, user)
    s = get_session()
    try:
        ws = s.get(WorkoutSession, sid); ws.finished_at = _now() - timedelta(minutes=45); s.commit()
    finally:
        s.close()
    out = handle_log_workout(user.id, {"split_day": "push", "exercises": [{"name": "abdominal crunch", "sets": 2, "reps": 10, "weight": 100}]})
    assert "just closed" not in out and [r for r in _sets(db, sid) if r[0] == "abdominal_crunch"] == []
    user2 = make_user(db, preferred_channel="imessage", **FOUNDER, phone="+15550007780")
    sid2 = _close(db, user2)
    monkeypatch.setattr(config, "WORKOUT_STRAGGLER_WINDOW_MIN", 0)
    out = handle_log_workout(user2.id, {"split_day": "push", "exercises": [{"name": "abdominal crunch", "sets": 2, "reps": 10, "weight": 100}]})
    assert "just closed" not in out and [r for r in _sets(db, sid2) if r[0] == "abdominal_crunch"] == []


# ── 3. memory: food habits out of schedule; the nightly typed-dup sweep ──────

def test_food_habits_leave_schedule_but_timed_schedule_lines_stay():
    from memory import apply_facts
    prof = {"schedule": [
        {"id": "a", "text": "Gets a coffee every morning with milk and sugar.", "ts": "t", "uses": 2},
        {"id": "b", "text": "lunch break 12-1 on weekdays", "ts": "t", "uses": 0},
        {"id": "c", "text": "Managing for soccer, roughly 4:00pm–7:30pm each day", "ts": "t", "uses": 0},
        {"id": "d", "text": "eats dinner at 9pm after practice", "ts": "t", "uses": 0},
    ]}
    new, _ = apply_facts(prof, [], user_id=40)
    assert [e["id"] for e in new["food_preferences"]] == ["a"]
    assert [e["id"] for e in new["schedule"]] == ["b", "c", "d"]


def test_nightly_consolidation_closes_typed_column_duplicates(db, caplog):
    from consolidation import consolidate_user
    from tests.tier1.test_consolidation import _entry, _profile, _valid_texts
    profile = {"training_preferences": [
        _entry("averages about 10,000 steps a day"), _entry("eats an equal mix of eating out and home-cooked meals"),
        _entry("uses the Apple Fitness app and an Apple Watch to track activity"),
        _entry("dislikes canned foods, especially canned fish"), _entry("dislikes spicy foods"),
        _entry("likes incorporating stairmaster cardio into workouts"),
        _entry("prefers machine exercises over barbell work because of the wrist"),
    ], "constraints": [_entry("dislikes canned fish — allergic, keep it off every plan", safety=True)],
       "identity": [_entry("is in high school, not a college student"), _entry("is not currently working")],
       "schedule": [_entry("Managing for soccer, roughly 4:00pm–7:30pm each day"), _entry("walks to and from the gym, 1.4 mi round trip")],
       "communication_preferences": [_entry("wants periodic reminders throughout the day to drink water")]}
    user = make_user(db, user_profile_memory=profile, **JAYDEN_COLS)
    caplog.set_level(logging.INFO)
    res = consolidate_user(user.id)
    assert res["status"] == "ok" and res["removed"] == 5, res
    live = _valid_texts(_profile(user.id))
    assert "likes incorporating stairmaster cardio into workouts" in " ".join(live)
    assert "prefers machine exercises over barbell work because of the wrist" in " ".join(live)
    assert "dislikes canned fish — allergic" in " ".join(live)                     # safety never swept
    assert "10,000 steps" not in " ".join(live) and "Apple Watch" not in " ".join(live)
    assert res["summary"].count(", restates ") == 5


def test_consolidation_sweep_flag_off_and_moves_food_facts(db, monkeypatch):
    from consolidation import consolidate_user
    from tests.tier1.test_consolidation import _entry, _profile
    monkeypatch.setattr(config, "MEMORY_TYPED_DUP_SWEEP_ENABLED", False)
    profile = {"training_preferences": [_entry("averages about 10,000 steps a day"),
                                        _entry("hates canned fish, texture thing")]}
    user = make_user(db, user_profile_memory=profile, **JAYDEN_COLS)
    res = consolidate_user(user.id)
    prof = _profile(user.id)
    assert [e["text"] for e in prof["training_preferences"]] == ["averages about 10,000 steps a day"]
    assert [e["text"] for e in prof["food_preferences"]] == ["hates canned fish, texture thing"]
    assert "moved: 1 food fact → food_preferences" in res["summary"]


# ── 4. summary duration, machine-aware names, the first-workout line ─────────

def test_summary_duration_runs_from_the_first_set_not_the_card(db, imessage_on, sidecar_ok, card_ok, sync_threads):
    from workouts.start import start_workout_session
    from workouts.session_ops import apply_text_update, close_session
    from workouts.summary import finish_session
    from models import get_session, WorkoutSession, SetLog
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = start_workout_session(user.id, "push")["session_id"]
    apply_text_update(user.id, "190 x4")
    s = get_session()
    try:
        ws = s.get(WorkoutSession, sid)
        ws.started_at = _now() - timedelta(minutes=118)                        # card built ~2h ago
        for r in s.query(SetLog).filter(SetLog.session_id == sid, SetLog.done.is_(True)).all():
            r.done_at = _now() - timedelta(minutes=50)                         # first tap 50 min ago
        s.commit()
    finally:
        s.close()
    close_session(sid, via="text", fill_as_planned=True)
    summary = sidecar_ok[-1] if "min" in sidecar_ok[-1] else sidecar_ok[-2]
    import re
    m = re.search(r"· (\d+) min", summary)
    assert m and 48 <= int(m.group(1)) <= 52, summary


def test_shoulder_press_on_a_machines_user_is_the_machine():
    from workouts.templates import machine_slug_for
    from card_page import resolve_exercise_name
    assert machine_slug_for("machines", "overhead_press") == "shoulder_press_machine"
    assert machine_slug_for("full_gym", "overhead_press") == "overhead_press"
    assert machine_slug_for("machines", "lat_pulldown") == "lat_pulldown" and machine_slug_for("machines", None) is None
    assert resolve_exercise_name("shoulder press", set(), equipment="machines") == ("shoulder_press_machine", "shoulder press machine")
    assert resolve_exercise_name("shoulder press", set(), equipment="full_gym") == ("overhead_press", "overhead press")
    assert resolve_exercise_name("bench", set(), equipment="machines")[0] == "chest_press_machine"


def test_card_add_exercise_uses_the_users_equipment(db, imessage_on, sidecar_ok, card_ok, client):
    from workouts.start import start_workout_session
    from card_page import card_token
    user = make_user(db, preferred_channel="imessage", equipment="machines", **FOUNDER)
    sid = start_workout_session(user.id, "push")["session_id"]
    tok = card_token(user.id, sid)
    r = client.post("/card/api/exercise", json={"name": "shoulder press", "sets": 3, "reps": 10, "weight": 55}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.get_json()
    assert "shoulder_press_machine" in {x[0] for x in _sets(db, sid)} and "overhead_press" not in {x[0] for x in _sets(db, sid)}


def test_first_ever_completed_session_gets_one_line_after_the_summary(db, imessage_on, sidecar_ok, card_ok, sync_threads, monkeypatch):
    from workouts.close import FIRST_WORKOUT_LINE
    user = make_user(db, preferred_channel="imessage", **FOUNDER)
    _close(db, user)
    assert sidecar_ok[-1] == FIRST_WORKOUT_LINE and "lb total" in sidecar_ok[-2]
    sidecar_ok.clear()
    _close(db, user, key="pull")                                               # second session: no line
    assert FIRST_WORKOUT_LINE not in sidecar_ok and any("lb total" in b for b in sidecar_ok)
    monkeypatch.setattr(config, "FIRST_WORKOUT_LINE_ENABLED", False)
    other = make_user(db, preferred_channel="imessage", **FOUNDER, phone="+15550007781")
    sidecar_ok.clear()
    _close(db, other)
    assert FIRST_WORKOUT_LINE not in sidecar_ok
