"""
Live 2026-10-30 17:18 PT (user 51): "Hitting the gym now, just doing upper" → "want me to send a
card" → "Nah I got it". The inbound path confirmed the day and set at_gym; nothing wrote a
Workout row; the next three briefings read the workouts table and said "pull's been 8 days".
Now the at_gym state carries what they said, and a sweep writes the session once it has
clearly happened (no card, no row, SAID_SESSION_SETTLE_MIN later).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user

PPL = dict(current_split="ppl", split_pointer_day="push", user_timezone="America/Los_Angeles",
           confirmed_training_days="mon,tue,wed,thu,fri,sat")
LIVE = "Hitting the gym now, just doing upper"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _said(uid, text, minutes_ago=100):
    from models import get_session, User
    s = get_session()
    try:
        u = s.get(User, uid)
        u.session_state = {"status": "at_gym", "said": text,
                           "started_at": (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()}
        s.commit()
    finally:
        s.close()


def _rows(db, uid):
    from models import Workout
    db.expire_all()
    return db.query(Workout).filter(Workout.user_id == uid, Workout.deleted_at.is_(None)).order_by(Workout.id).all()


def _state(db, uid):
    from models import User
    db.expire_all()
    return db.get(User, uid).session_state


# ─── 1. the inbound path keeps what they said ────────────────────────────────

def test_the_freeform_confirmation_stores_what_they_said(db, driver, anthropic_stub, monkeypatch):
    import config
    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", False)
    user = make_user(db, **PPL)
    anthropic_stub.reply_with(lambda kw: "go get it")
    driver.send(user, LIVE)
    st = _state(db, user.id)
    assert st and st["status"] == "at_gym" and st["said"] == LIVE


# ─── 2. the sweep ────────────────────────────────────────────────────────────

def test_live_case_settles_into_a_row_and_leaves_an_off_cycle_pointer_alone(db, caplog):
    import logging
    from workouts.said_sessions import settle_said_sessions
    from split_pointer import get_split_pointer
    caplog.set_level(logging.INFO)
    u = make_user(db, **PPL)
    _said(u.id, LIVE)
    assert settle_said_sessions(_now()) == 1
    rows = _rows(db, u.id)
    assert len(rows) == 1 and rows[0].workout_type == "upper" and rows[0].completed
    assert "said they were at the gym" in rows[0].user_notes and rows[0].exercises == []
    assert (_now() - rows[0].date) > timedelta(minutes=90)        # dated when they said it, not when settled
    assert get_split_pointer(u.id)["day"] == "push"               # "upper" isn't a PPL day: no guess
    assert "SAID_SESSION_SETTLED" in caplog.text and "in_cycle=False" in caplog.text
    assert _state(db, u.id) is None
    assert settle_said_sessions(_now()) == 0 and len(_rows(db, u.id)) == 1   # once


def test_a_named_day_in_their_cycle_moves_the_pointer_with_an_audit(db):
    from workouts.said_sessions import settle_said_sessions
    from split_pointer import get_split_pointer
    u = make_user(db, **PPL)
    _said(u.id, "hitting legs rn")
    assert settle_said_sessions(_now()) == 1
    rows = _rows(db, u.id)
    assert rows[0].workout_type == "legs"
    p = get_split_pointer(u.id)
    assert p["day"] == "legs" and p["source"] == "confirmed"
    assert rows[0].edits and rows[0].edits[-1]["field"] == "split_pointer" and rows[0].edits[-1]["new"]["day"] == "legs"


def test_the_briefings_training_gap_now_counts_it(db):
    """What the live case broke: _training_gap_signal reads workouts rows."""
    from workouts.said_sessions import settle_said_sessions
    from heartbeat import _training_gap_signal
    from models import get_session, User
    u = make_user(db, **PPL)
    _said(u.id, LIVE, minutes_ago=120)
    settle_said_sessions(_now())
    s = get_session()
    try:
        gap = _training_gap_signal(s.get(User, u.id), s)
    finally:
        s.close()
    assert not gap or "0 days ago" in gap


def test_too_soon_and_no_said_do_nothing(db):
    from workouts.said_sessions import settle_said_sessions
    from models import set_session_state
    u = make_user(db, **PPL)
    _said(u.id, LIVE, minutes_ago=30)
    assert settle_said_sessions(_now()) == 0 and _rows(db, u.id) == []
    v = make_user(db, **PPL, phone="+15550004001")
    set_session_state(v.id, "at_gym")                              # the legacy shape: nothing to settle
    assert settle_said_sessions(_now()) == 0 and _rows(db, v.id) == []


@pytest.mark.parametrize("text", [
    "hitting the gym at 6", "gym today after data", "going to the gym later", "hitting legs tonight",
    "heading to the gym around 7pm", "gym in 30 min",
])
def test_a_plan_for_later_never_settles(db, text, caplog):
    import logging
    from workouts.said_sessions import settle_said_sessions, said_for_later
    caplog.set_level(logging.INFO)
    assert said_for_later(text), text
    u = make_user(db, **PPL)
    _said(u.id, text)
    assert settle_said_sessions(_now()) == 0 and _rows(db, u.id) == []
    assert _state(db, u.id)["settled"] == "later" and "said_for_later" in caplog.text
    assert settle_said_sessions(_now()) == 0                       # marked: not re-examined


@pytest.mark.parametrize("text", [LIVE, "hitting legs", "at the gym, starting push", "gym rn"])
def test_present_tense_is_not_a_plan(text):
    from workouts.said_sessions import said_for_later
    assert not said_for_later(text), text


def test_a_card_session_or_a_logged_row_that_day_wins(db):
    from workouts.said_sessions import settle_said_sessions
    from workouts.start import start_workout_session
    from agent_tools import handle_log_workout
    carded = make_user(db, **PPL)
    _said(carded.id, LIVE)
    start_workout_session(carded.id, "push", no_anchors=True)
    assert settle_said_sessions(_now()) == 0 and _rows(db, carded.id) == []
    assert _state(db, carded.id)["settled"] == "card"
    logged = make_user(db, **PPL, phone="+15550004002")
    _said(logged.id, LIVE)
    handle_log_workout(logged.id, {"split_day": "push", "exercises": [{"name": "bench", "sets": 3, "reps": 8, "weight": 135}]})
    assert settle_said_sessions(_now()) == 0 and len(_rows(db, logged.id)) == 1
    assert _state(db, logged.id)["settled"] == "already_logged"


def test_the_flag_off_writes_nothing(db, monkeypatch):
    import config
    from workouts.said_sessions import settle_said_sessions
    monkeypatch.setattr(config, "SAID_SESSION_SETTLE_ENABLED", False)
    u = make_user(db, **PPL)
    _said(u.id, LIVE)
    assert settle_said_sessions(_now()) == 0 and _rows(db, u.id) == []
