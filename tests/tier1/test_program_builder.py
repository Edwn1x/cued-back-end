"""
workouts/program.py — the code-built, tailored program and the coach's brief of it
(founder, 2026-10-18: "a schedule, a training program that is actually good, that fits each
user, tailored, and explains it to the user like a real coach would").
Live before: user 40 (17, never trained, 4 nights at 10:30pm, machines, wrist) got the same
five-machine full-body day every session and a briefing that could only guess "rest day".
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

import config
from tests.factories import make_user

JAYDEN = dict(name="Jayden", experience="none", goal="fat_loss", equipment="machines", workout_days="4",
              workout_time="22:30", workout_times=None, confirmed_training_days=None, current_split="none",
              split_days=None, custom_templates=None, user_timezone="America/Los_Angeles",
              injuries="right wrist — surgery for broken distal radius a few months ago",
              user_profile_memory={"training_preferences": [{"id": "stairs1", "text": "likes incorporating stairmaster cardio into workouts",
                                                             "ts": "2026-10-17T23:13:20+00:00", "uses": 1}]})


def _u(**over):
    base = dict(JAYDEN); base.update(over); return NS(**base)


def test_live_user_40_gets_upper_lower_on_machines_with_his_stairmaster():
    from workouts.program import build_program
    p = build_program(_u())
    assert p["split"] == "upper_lower" and p["cycle"] == ["upper", "lower"]
    assert p["days"] == {"mon": "upper", "tue": "lower", "thu": "upper", "fri": "lower"} and p["days_source"] == "default"
    assert set(p["times"].values()) == {"22:30"}
    assert p["exercises"]["upper"] == ["chest press machine", "seated cable row", "shoulder press machine", "lat pulldown", "dumbbell curl", "tricep pushdown"]
    assert p["exercises"]["lower"] == ["leg press", "leg extension", "leg curl", "calf raise"]
    assert p["cardio"] == {"what": "stairmaster", "minutes": 15, "on": ["lower"]}
    assert any("wrist" in c for c in p["constraints"]) and p["scheme"]["sets"] == 3 and p["scheme"]["reps"] == "10-12"
    assert "every muscle gets hit twice a week" in p["rationale"][0]
    assert any("nothing on the wrist" in r for r in p["rationale"])


@pytest.mark.parametrize("days,experience,split,cycle", [
    ("2", "none", "full_body", ["full_body"]), ("3", "beginner", "full_body", ["full_body"]),
    ("3", "intermediate", "ppl", ["push", "pull", "legs"]), ("4", "advanced", "upper_lower", ["upper", "lower"]),
    ("5", "intermediate", "rotation", ["push", "pull", "legs", "upper", "lower"]), ("6", "advanced", "ppl", ["push", "pull", "legs"]),
])
def test_split_follows_days_and_level(days, experience, split, cycle):
    from workouts.program import build_program
    p = build_program(_u(workout_days=days, experience=experience, equipment="full_gym", injuries=None, user_profile_memory=None, goal="muscle_building"))
    assert (p["split"], p["cycle"]) == (split, cycle) and p["days_per_week"] == int(days)
    assert len(p["days"]) == int(days)


def test_named_days_and_a_stated_split_win():
    from workouts.program import build_program
    p = build_program(_u(workout_days="mon,wed,fri", experience="intermediate", current_split="ppl", equipment="full_gym", injuries=None))
    assert p["days"] == {"mon": "push", "wed": "pull", "fri": "legs"} and p["days_source"] == "stated" and p["split"] == "ppl"
    assert "ur own split" not in p["rationale"][0] and "push / pull / legs" in p["rationale"][0]
    q = build_program(_u(workout_days="4", confirmed_training_days="sun,tue,thu,sat"))
    assert list(q["days"]) == ["tue", "thu", "sat", "sun"] and q["days"]["tue"] == "upper"
    r = build_program(_u(), weekdays=["mon", "wed", "fri", "sat"])          # their swap in the brief
    assert list(r["days"]) == ["mon", "wed", "fri", "sat"] and r["days_source"] == "stated"


def test_bodyweight_and_full_gym_exercises_and_cardio_rules():
    from workouts.program import build_program
    bw = build_program(_u(equipment="bodyweight", injuries=None, user_profile_memory=None, goal="general_fitness"))
    assert all("press machine" not in e for e in bw["exercises"]["upper"]) and "pushup" in bw["exercises"]["upper"]
    assert bw["cardio"] is None and any("bodyweight" in r for r in bw["rationale"])
    gym = build_program(_u(equipment="full_gym", injuries=None, user_profile_memory=None))
    assert "bench press" in gym["exercises"]["upper"] and gym["cardio"] == {"what": "incline walk", "minutes": 15, "on": ["lower"]}   # fat loss → a finisher
    by_day = build_program(_u(workout_times={"by_day": {"mon": "07:00", "tue": "18:00", "thu": "07:00", "fri": "18:00"}}))
    assert by_day["times"] == {"mon": "07:00", "tue": "18:00", "thu": "07:00", "fri": "18:00"}


def test_the_brief_reads_like_a_coach_and_ends_on_the_days_question():
    from workouts.program import build_program, program_brief, describe_week
    p = build_program(_u())
    assert describe_week(p) == "mon upper · tue lower · thu upper · fri lower at 10:30pm"
    b = program_brief(_u(), p)
    assert len(b) == 3
    assert b[0].startswith("ok jayden, here's ur program. mon upper · tue lower · thu upper · fri lower at 10:30pm.")
    assert "upper = chest press machine" in b[1] and "lower = leg press" in b[1] and "stairmaster" in b[1] and "wrist" in b[1]
    assert b[2].startswith("how it moves: 3x10-12.") and "weight goes up next time" in b[2] and b[2].endswith("those days work, or wanna swap any?")
    for bubble in b:
        assert bubble == bubble.lower() or "jayden" in bubble     # voice: lowercase
        assert len(bubble) < 420


def test_program_context_names_today_and_the_next_session():
    from workouts.program import build_program, program_context
    p = build_program(_u())
    sun = datetime(2026, 10, 18, 19, 0, tzinfo=timezone.utc)            # Sunday noon PT
    ctx = program_context(_u(), p, now=sun)
    assert "- today (sun): rest day; next: mon upper" in ctx and "- week: mon upper · tue lower" in ctx
    mon = datetime(2026, 10, 19, 19, 0, tzinfo=timezone.utc)
    assert "- today (mon): upper day; next: tue lower" in program_context(_u(), p, now=mon)
    assert "machines only" in ctx and "weight goes up next time" in ctx
    assert program_context(NS(program=None, user_timezone="America/Los_Angeles")) == ""


def test_apply_program_stores_it_and_mirrors_the_columns(db):
    from workouts.program import apply_program
    from workouts.start import infer_template
    from models import get_session, User
    u = make_user(db, **{k: v for k, v in JAYDEN.items() if k != "user_profile_memory"},
                  user_profile_memory=JAYDEN["user_profile_memory"])
    p = apply_program(u.id)
    db.expire_all(); row = db.get(User, u.id)
    assert row.program["split"] == "upper_lower" and row.program["version"] == 1
    assert row.current_split == "upper_lower" and row.confirmed_training_split == "upper_lower"
    assert row.confirmed_training_days == "mon,tue,thu,fri" and row.workout_days == "4"       # a count stays a count until they confirm
    assert infer_template(row) == "upper"                                                     # the next card is a real day
    # their swap: the days become theirs
    apply_program(u.id, weekdays=["mon", "wed", "fri", "sat"], source="brief")
    db.expire_all(); row = db.get(User, u.id)
    assert row.confirmed_training_days == "mon,wed,fri,sat" and row.workout_days == "mon,wed,fri,sat"
    assert row.program["days"] == {"mon": "upper", "wed": "lower", "fri": "upper", "sat": "lower"}


def test_apply_program_rotation_uses_split_days_and_the_flag_is_a_noop(db, monkeypatch):
    from workouts.program import apply_program
    from split_pointer import cycle_for
    from models import User
    u = make_user(db, experience="intermediate", goal="muscle_building", equipment="full_gym", workout_days="5", workout_time="18:00", current_split=None)
    apply_program(u.id)
    db.expire_all(); row = db.get(User, u.id)
    assert row.split_days == ["push", "pull", "legs", "upper", "lower"] and cycle_for(row) == row.split_days
    monkeypatch.setattr(config, "PROGRAM_BUILDER_ENABLED", False)
    v = make_user(db, workout_days="3", phone="+15550008881")
    assert apply_program(v.id) is None


# ── the brief, the days answer, the standing reminder ────────────────────────

def _brief_user(db, **over):
    kw = {k: v for k, v in JAYDEN.items() if k != "user_profile_memory"}
    kw.update(dict(preferred_channel="sms", user_profile_memory=JAYDEN["user_profile_memory"]))
    kw.update(over)
    return make_user(db, **kw)


def _reminders(db, uid, source=None):
    from models import Reminder
    db.expire_all()
    q = db.query(Reminder).filter(Reminder.user_id == uid, Reminder.active.is_(True))
    if source:
        q = q.filter(Reminder.source == source)
    return q.order_by(Reminder.id).all()


def test_brief_goes_out_as_one_step_and_a_yes_locks_the_days_and_arms_the_reminder(db, sms_capture, driver, anthropic_stub):
    from workouts.program import send_program_brief, handle_days_reply
    from models import User
    u = _brief_user(db)
    assert send_program_brief(u.id) is True
    bodies = [b for _p, b in sms_capture]
    assert any(b.startswith("ok jayden, here's ur program.") for b in bodies), bodies
    assert any(b.endswith("those days work, or wanna swap any?") for b in bodies)
    db.expire_all(); assert not db.get(User, u.id).program.get("days_confirmed")
    sms_capture.clear()
    replies = driver.send(u, "yes that works")
    assert replies == ["locked in. i'll ping u those days at 10:30pm. say 'stop the gym reminders' if u don't want that"], replies
    assert anthropic_stub.calls == [], "the yes must not reach the model"
    db.expire_all(); row = db.get(User, u.id)
    assert row.program["days_confirmed"] is True and row.confirmed_training_days == "mon,tue,thu,fri"
    rem = _reminders(db, u.id, source="program")
    assert len(rem) == 1 and rem[0].recur_days == "mon,tue,thu,fri" and rem[0].local_time == "22:30" and rem[0].text == "gym"
    # answered: a second "yes" is a normal turn now
    assert handle_days_reply(u.id, "yes") is None


@pytest.mark.parametrize("text,expected_days", [
    ("mon wed fri sat", ["mon", "wed", "fri", "sat"]),
    ("swap thu for fri", ["mon", "tue", "fri"]),                 # fri already there → 3 distinct days
    ("wednesday instead of tuesday", ["mon", "wed", "thu", "fri"]),
    ("can we do tuesday thursday saturday and sunday", ["tue", "thu", "sat", "sun"]),
])
def test_a_list_or_a_swap_rebuilds_the_week_on_their_days(db, sms_capture, text, expected_days):
    from workouts.program import send_program_brief, handle_days_reply
    from models import User
    u = _brief_user(db)
    send_program_brief(u.id)
    line = handle_days_reply(u.id, text)
    assert line and line.startswith("locked: "), line
    db.expire_all(); row = db.get(User, u.id)
    assert list(row.program["days"]) == expected_days and row.program["days_confirmed"] is True
    assert row.confirmed_training_days == ",".join(expected_days) and row.workout_days == ",".join(expected_days)
    assert _reminders(db, u.id, source="program")[0].recur_days == ",".join(expected_days)


def test_no_asks_for_days_and_anything_else_is_a_normal_turn(db, sms_capture):
    from workouts.program import send_program_brief, handle_days_reply
    u = _brief_user(db)
    send_program_brief(u.id)
    assert handle_days_reply(u.id, "nah") == "which days then? just list em"
    assert handle_days_reply(u.id, "why upper lower and not push pull legs?") is None
    assert _reminders(db, u.id, source="program") == []
    v = _brief_user(db, phone="+15550008882")
    assert handle_days_reply(v.id, "yes") is None                                 # no brief pending


def test_completion_sends_the_brief_after_the_summary_and_defers_the_first_setup_step(db, sms_capture, anthropic_stub, monkeypatch, caplog):
    import logging
    import onboarding_agent as oa
    from tests.tier1.test_card_setup import _u as _row
    monkeypatch.setattr(config, "SETUP_SEQUENCE_ENABLED", True)
    monkeypatch.setattr(config, "SETUP_STEPS_DIRECTIVE_ENABLED", True)
    monkeypatch.setattr(config, "CARD_SETUP_ENABLED", True)
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_DELAY_S", 0)
    anthropic_stub.reply_with(lambda kw: "locked in")
    u = _brief_user(db, onboarding_step=2, calorie_target=None, protein_target=None, height_ft=5, height_in=4,
                    weight_lbs=200, age=17, gender="male", workout_days="4", current_split="none", lift_anchors=None)
    caplog.set_level(logging.INFO)
    oa._complete_onboarding(_row(u.id), "nothing")
    bodies = [b for _p, b in sms_capture]
    i_sum = next(i for i, b in enumerate(bodies) if b.startswith("ok so far this is what i have"))
    i_brief = next(i for i, b in enumerate(bodies) if b.startswith("ok jayden, here's ur program"))
    assert i_brief > i_sum
    assert "ONBOARDING_SETUP_KICKOFF_DEFERRED" in caplog.text and "CONNECT_LINK_SENT" not in caplog.text
    assert not any(b.startswith("first thing:") for b in bodies)


def test_adjust_program_tool_rebuilds_and_moves_the_reminder(db, sms_capture):
    from agent_tools import handle_adjust_program, ADJUST_PROGRAM_TOOL
    from workouts.program import send_program_brief, handle_days_reply
    from models import User
    assert ADJUST_PROGRAM_TOOL["input_schema"]["required"] == []
    u = _brief_user(db)
    send_program_brief(u.id); handle_days_reply(u.id, "yes")
    r = handle_adjust_program(u.id, {"days": ["mon", "wed", "fri"], "time": "07:00"})
    assert r.startswith("ok: program is now mon full body · wed full body · fri full body at 7am (full/body)"), r
    db.expire_all(); row = db.get(User, u.id)
    assert row.program["split"] == "full_body" and row.workout_time == "07:00" and row.confirmed_training_days == "mon,wed,fri"
    assert row.program["split_source"] == "builder" and row.program["days_confirmed"] is True
    rem = _reminders(db, u.id, source="program")
    assert len(rem) == 1 and rem[0].recur_days == "mon,wed,fri" and rem[0].local_time == "07:00"
    r2 = handle_adjust_program(u.id, {"split": "ppl"})
    assert "(ppl)" in r2 and "push" in r2
    db.expire_all(); assert db.get(User, u.id).program["split_source"] == "user"
    r3 = handle_adjust_program(u.id, {"days": ["mon", "tue", "thu", "fri"]})       # their stated ppl survives a day change
    assert "(ppl)" in r3 and "mon push · tue pull · thu legs · fri push" in r3
    assert handle_adjust_program(u.id, {"days": ["funday"]}).startswith("error: unknown days")
    assert handle_adjust_program(u.id, {"time": "late"}).startswith("error: time")


def test_the_loop_and_heartbeat_contexts_carry_the_program_block(db, anthropic_stub, monkeypatch):
    import agent_loop, heartbeat
    from workouts.program import apply_program
    from models import User
    u = _brief_user(db)
    apply_program(u.id)
    db.expire_all(); row = db.get(User, u.id)
    ctx = agent_loop._build_context(row) if hasattr(agent_loop, "_build_context") else None
    if ctx is None:
        anthropic_stub.reply_with(lambda kw: "ok")
        agent_loop.run_agent_loop(row, "what's today", "freeform")
        seen = " ".join(str(c) for c in anthropic_stub.calls)
        assert "## PROGRAM (upper/lower" in seen and "- week: mon upper · tue lower" in seen
    else:
        assert "## PROGRAM (upper/lower" in ctx
    from models import get_session
    s = get_session()
    try:
        blk = heartbeat._proactive_context(s.get(User, u.id), s)
    finally:
        s.close()
    assert "## PROGRAM (upper/lower" in blk and "next:" in blk
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "Their program (the PROGRAM block" in v and "adjust_program" in v
