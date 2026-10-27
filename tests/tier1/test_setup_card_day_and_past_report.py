"""
Live 2026-10-27, user 77 (Alexey), first card at setup:
  11:21 the brief: "mon push · tue pull · wed legs · thu push · fri pull · sat legs at 8am"
  11:41 "All time PRs: Bench: 185, squat: 3 plates. This morning I did 3x10 of 135 on bench…"
  11:42 the card: "here's ur first card, push day … tap it now so ur set for 8am"  (a Tuesday)
  11:42 the model's log_workout → LOG_WORKOUT_INTO_SESSION: his morning sets landed INSIDE the
        planned setup card, which became an active session; "next is pull" contradicted the brief
  11:44 he tapped a row at home → bench 165×5 "done", source card — a set he never did

Three fixes: (1) with no split pointer the card is the PROGRAM's day for the next session —
today's mapped day unless they've trained today, else the next mapped weekday; (2) a report of a
session already done never lands in a planned card — it logs on its own, the pointer moves, and
the waiting card is re-planned in place to the next day with one line; (3) the setup copy says
"just open it — the sets r for when u lift".
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user
from tests.tier1.test_card_setup import setup_on, sidecar_ok, card_ok, sync_threads, _imsg_user, _sessions, ANCHORS  # noqa: F401

PROG = {"version": 1, "level": "beginner", "split": "ppl", "cycle": ["push", "pull", "legs"],
        "days": {"mon": "push", "tue": "pull", "wed": "legs", "thu": "push", "fri": "pull", "sat": "legs"},
        "times": {d: "08:00" for d in ("mon", "tue", "wed", "thu", "fri", "sat")}, "days_source": "default",
        "exercises": {}, "scheme": {"sets": 3, "reps": "10-12", "why": ""}, "progression": {"text": "", "review_weeks": 4},
        "rationale": [""], "constraints": []}

TUE = datetime(2026, 10, 27, 18, 0, tzinfo=timezone.utc)      # Tue 11am PT (Alexey's setup)
SUN = datetime(2026, 10, 25, 18, 0, tzinfo=timezone.utc)
WED = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)

LIVE_TEXT = "All time PRs: Bench: 185, squat: 3 plates. This morning I did 3x10 of 135 on bench and 30 lbs tricep extension."


def _alexey(db, **kw):
    base = dict(onboarding_step=3, current_split="ppl", program=PROG, workout_time="08:00", workout_days="6",
                user_timezone="America/Los_Angeles", lift_anchors=dict(ANCHORS), experience="none",
                split_pointer_day=None, split_pointer_source=None, height_ft=6, height_in=2, weight_lbs=165, age=22)
    base.update(kw)
    return make_user(db, **base)


# ─── 1. the day ──────────────────────────────────────────────────────────────

def test_no_pointer_means_the_programs_day_for_today(db):
    from workouts.start import infer_template, program_next_key
    u = _alexey(db)
    assert program_next_key(u, now=TUE) == "pull" and infer_template(u, now=TUE) == "pull"
    assert infer_template(u, now=WED) == "legs"
    assert infer_template(u, now=SUN) == "push"            # a rest day → the next mapped weekday (mon)


def test_trained_today_rolls_the_first_card_to_the_next_program_day(db):
    from workouts.start import infer_template, trained_today
    from models import get_session, Workout
    u = _alexey(db)
    assert trained_today(u, now=TUE) is False
    s = get_session()
    try:
        s.add(Workout(user_id=u.id, workout_type="logged", exercises=[], completed=True,
                      date=TUE.replace(tzinfo=None) - timedelta(hours=2)))      # this morning
        s.commit()
    finally:
        s.close()
    assert trained_today(u, now=TUE) is True
    assert infer_template(u, now=TUE) == "legs"             # Tuesday is done → Wednesday's day


def test_cardio_today_is_not_a_trained_day(db):
    from workouts.start import trained_today
    from models import get_session, Workout
    u = _alexey(db)
    s = get_session()
    try:
        s.add(Workout(user_id=u.id, workout_type="cardio", exercises=[], completed=True, date=TUE.replace(tzinfo=None)))
        s.commit()
    finally:
        s.close()
    assert trained_today(u, now=TUE) is False


def test_a_pointer_still_wins_over_the_calendar(db):
    from workouts.start import infer_template
    u = _alexey(db, split_pointer_day="push", split_pointer_source="confirmed")
    assert infer_template(u, now=TUE) == "pull" and infer_template(u, now=WED) == "pull"


def test_without_a_program_or_with_the_flag_off_it_is_the_cycles_first_day(db, monkeypatch):
    from workouts.start import infer_template
    assert infer_template(_alexey(db, program=None), now=TUE) == "push"
    import config
    monkeypatch.setattr(config, "SETUP_CARD_PROGRAM_DAY_ENABLED", False)
    assert infer_template(_alexey(db), now=TUE) == "push"


# ─── 2. a report never lands in a planned card ───────────────────────────────

@pytest.mark.parametrize("text,past", [
    (LIVE_TEXT, True),
    ("earlier i did legs, squat 185x5x3", True),
    ("just got back from the gym, bench 135 3x10", True),
    ("yesterday i hit pull", True),
    ("bench 190x4 done", False),
    ("hit 185 on bench today", False),
    ("heading to the gym rn", False),
])
def test_past_report_regex(text, past):
    from agent_tools import _PAST_REPORT_RE
    assert bool(_PAST_REPORT_RE.search(text)) is past, text


def test_a_morning_report_logs_on_its_own_and_replans_the_setup_card(db, setup_on, sidecar_ok, card_ok, sync_threads):
    """Alexey's turn, replayed: push setup card waiting (the old inference) → 'this morning I did
    3x10 of 135 on bench' → a standalone workout row, pointer push, the card re-planned to pull in
    the same bubble with one line; no extension reminder, no second tour, no set logged inside it.
    (A confirmed pointer is seeded so the inferred advance — legs → push → next pull — doesn't
    depend on the weekday the suite runs; the no-pointer program-day path is pinned above.)"""
    from workouts.start import start_workout_session
    from workouts.card_setup import EXTENSION_REMINDER, BREAKDOWN_SETUP
    from agent_tools import handle_log_workout
    from models import get_session, Workout, SetLog, Message
    u = _alexey(db, preferred_channel="imessage", split_pointer_day="legs", split_pointer_source="confirmed")
    r0 = start_workout_session(u.id, "push", setup=True)
    assert r0["surface"] == "card" and r0["setup"] is True
    setup_sid = r0["session_id"]
    assert _sessions(u.id)[0][1] == "planned"
    s = get_session()
    try:
        m = Message(user_id=u.id, direction="in", body=LIVE_TEXT)
        s.add(m); s.commit(); mid = m.id
    finally:
        s.close()
    del sidecar_ok[:]

    out = handle_log_workout(u.id, {"exercises": [{"name": "bench press", "sets": 3, "reps": 10, "weight": 135},
                                                  {"name": "tricep extension", "sets": 1, "reps": 12, "weight": 30}]},
                             message_id=mid)
    assert out.startswith("ok: logged workout"), out
    assert "split pointer now push" in out
    assert "re-planned it to pull day" in out and "don't repeat that" in out
    s = get_session()
    try:
        assert s.query(Workout).filter(Workout.user_id == u.id, Workout.completed.is_(True)).count() == 1
        assert s.query(SetLog).filter(SetLog.session_id == setup_sid, SetLog.done.is_(True)).count() == 0, "nothing landed in the setup card"
    finally:
        s.close()
    rows = _sessions(u.id)
    assert rows[0] == (setup_sid, "abandoned", {"id": "photon-card-1"})
    assert rows[1][1] == "planned" and rows[1][2] == {"id": "photon-card-1"}       # same bubble, still a setup card
    assert len(card_ok["sent"]) == 1 and len(card_ok["updated"]) == 1
    assert sidecar_ok == ["u already got push in today, so ur card's now pull day for 8am. same deal: the sets r for when u lift"], sidecar_ok
    assert EXTENSION_REMINDER not in sidecar_ok and not any(b in BREAKDOWN_SETUP for b in sidecar_ok)


def test_already_done_flag_works_without_past_words(db, setup_on, sidecar_ok, card_ok, sync_threads):
    from workouts.start import start_workout_session
    from agent_tools import handle_log_workout
    from models import get_session, SetLog
    u = _alexey(db, preferred_channel="imessage", split_pointer_day="legs", split_pointer_source="confirmed")
    sid = start_workout_session(u.id, "push", setup=True)["session_id"]
    out = handle_log_workout(u.id, {"exercises": [{"name": "bench press", "sets": 3, "reps": 10, "weight": 135}],
                                    "already_done": True})
    assert out.startswith("ok: logged workout") and "re-planned it to pull day" in out
    s = get_session()
    try:
        assert s.query(SetLog).filter(SetLog.session_id == sid, SetLog.done.is_(True)).count() == 0
    finally:
        s.close()


def test_live_logging_still_lands_in_the_planned_card(db, setup_on, sidecar_ok, card_ok, sync_threads):
    """At the gym, card planned this morning, 'bench 135x10' with no past words: the card absorbs it."""
    from workouts.start import start_workout_session
    from agent_tools import handle_log_workout
    from models import get_session, SetLog, WorkoutSession
    u = _alexey(db, preferred_channel="imessage")
    sid = start_workout_session(u.id, "push", setup=True)["session_id"]
    out = handle_log_workout(u.id, {"exercises": [{"name": "bench press", "sets": 3, "reps": 10, "weight": 135}]})
    assert "re-planned" not in out
    s = get_session()
    try:
        assert s.query(SetLog).filter(SetLog.session_id == sid, SetLog.done.is_(True)).count() == 3
        assert s.get(WorkoutSession, sid).status == "active"
    finally:
        s.close()


def test_flag_off_restores_absorption(db, setup_on, sidecar_ok, card_ok, sync_threads, monkeypatch):
    import config
    monkeypatch.setattr(config, "PAST_REPORT_SKIPS_PLANNED_CARD_ENABLED", False)
    from workouts.start import start_workout_session
    from agent_tools import handle_log_workout
    from models import get_session, SetLog
    u = _alexey(db, preferred_channel="imessage")
    sid = start_workout_session(u.id, "push", setup=True)["session_id"]
    handle_log_workout(u.id, {"exercises": [{"name": "bench press", "sets": 3, "reps": 10, "weight": 135}], "already_done": True})
    s = get_session()
    try:
        assert s.query(SetLog).filter(SetLog.session_id == sid, SetLog.done.is_(True)).count() == 3
    finally:
        s.close()


def test_no_replan_when_the_waiting_card_is_already_the_right_day(db, setup_on, sidecar_ok, card_ok, sync_threads):
    """Card is pull (the program's Tuesday); an unnamed morning report infers push → next is pull:
    the card already says pull, nothing moves, nothing is sent."""
    from workouts.start import start_workout_session
    from agent_tools import handle_log_workout
    u = _alexey(db, preferred_channel="imessage", split_pointer_day="legs", split_pointer_source="confirmed")
    start_workout_session(u.id, "pull", setup=True)
    del sidecar_ok[:]
    out = handle_log_workout(u.id, {"exercises": [{"name": "bench press", "sets": 3, "reps": 10, "weight": 135}], "already_done": True})
    assert out.startswith("ok: logged workout") and "re-planned" not in out
    assert sidecar_ok == [] and len(card_ok["updated"]) == 0
    assert [r[1] for r in _sessions(u.id)] == ["planned"]


def test_tool_schema_and_copy():
    from agent_tools import LOG_WORKOUT_TOOL
    from workouts.start import intro_line
    assert "already_done" in LOG_WORKOUT_TOOL["input_schema"]["properties"]
    assert "never absorbs a report" in LOG_WORKOUT_TOOL["description"]
    ex = [{"label": "bench press", "sets": [{"planned_weight": 135, "planned_reps": 5}]}]
    line = intro_line(ex, "pull", first=True, setup=True, workout_time="08:00")
    assert "tap it now so ur set for 8am (just open it — the sets r for when u lift)" in line, line
    assert intro_line(ex, "pull", setup=True, workout_time="08:00", replan_from="push").startswith("u already got push in today")
