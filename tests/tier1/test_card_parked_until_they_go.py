"""
Founder, 2026-10-22: "at 2:59pm when he was talking about his workout, the coach shouldn't
have sent the workout card unless he's actually at the gym ready to work out. it's annoying
and comes off as nagging if he just gets it when he's just letting us know."

Live: user 40 (trains ~10:30pm) answered "Gym. Tryouts got canceled" at 2:58pm → the full
card + two intro bubbles landed at 2:59pm, 7½ hours early. The card now goes out when
they're going (an arrival / ask line), not when they mention going.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import FOUNDER, imessage_on, sidecar_ok, card_ok, sync_threads  # noqa: F401

NIGHT_LIFTER = dict(FOUNDER, workout_time="22:30", user_timezone="America/Los_Angeles")
# 2026-10-22 14:59 PT == 21:59 UTC (the live moment)
AFTERNOON_UTC = datetime(2026, 10, 22, 21, 59)
LATE_UTC = datetime(2026, 10, 23, 5, 5)           # 22:05 PT, 25 min before his usual


def _inbound(db, user, text):
    from models import Message
    m = Message(user_id=user.id, direction="in", body=text, message_type="freeform")
    db.add(m); db.commit()
    return m.id


def _sessions(db, user):
    from models import WorkoutSession
    return db.query(WorkoutSession).filter(WorkoutSession.user_id == user.id).count()


def _intros(db, user):
    from models import Message
    return db.query(Message).filter(Message.user_id == user.id, Message.direction == "out",
                                    Message.message_type.in_(("workout_intro", "workout_card"))).count()


@pytest.fixture
def at(monkeypatch):
    def _set(when):
        from workouts import park
        monkeypatch.setattr(park, "_now_utc", lambda: when)
    return _set


def test_a_statement_hours_before_their_usual_start_parks_the_card(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, at):
    from agent_tools import handle_start_workout_session
    from workouts import park
    anthropic_stub.reply_with(lambda kw: "nope")
    at(AFTERNOON_UTC)
    u = make_user(db, preferred_channel="imessage", **NIGHT_LIFTER)
    mid = _inbound(db, u, "Gym. Tryouts got canceled")
    r = handle_start_workout_session(u.id, {}, message_id=mid)
    assert r.startswith("NOT sent") and "10:30pm" in r and "2:59pm" in r and "parked" in r, r
    assert "no exercise list" in r and "Don't call this again" in r
    assert _sessions(db, u) == 0 and _intros(db, u) == 0
    entry = park.peek(u.id)
    assert entry and entry["key"] is None and not entry["exercises"]
    # "going" + a future marker is a plan, not an arrival
    for i, text in enumerate(["im going to the gym tonight", "going to the gym later", "ready for tonight", "ill probably lift at 10"]):
        v = make_user(db, preferred_channel="imessage", phone=f"+1555021{i:04d}", **NIGHT_LIFTER)
        r = handle_start_workout_session(v.id, {}, message_id=_inbound(db, v, text))
        assert r.startswith("NOT sent"), (text, r)
        assert _sessions(db, v) == 0


def test_an_ask_or_an_arrival_sends_whatever_the_clock_says(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, at):
    from agent_tools import handle_start_workout_session
    anthropic_stub.reply_with(lambda kw: "nope")
    at(AFTERNOON_UTC)
    for i, text in enumerate(["what's my workout today", "send me my card", "omw to the gym", "at the gym, ate a sandwich on the way",
                              "bouta lift", "im here", "what should i do today"]):
        u = make_user(db, preferred_channel="imessage", phone=f"+1555022{i:04d}", **NIGHT_LIFTER)
        r = handle_start_workout_session(u.id, {}, message_id=_inbound(db, u, text))
        assert r.startswith("ok:"), (text, r)
        assert _sessions(db, u) == 1, text


def test_a_statement_near_their_usual_start_or_with_no_time_on_file_still_sends(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, at):
    from agent_tools import handle_start_workout_session
    anthropic_stub.reply_with(lambda kw: "nope")
    at(LATE_UTC)
    u = make_user(db, preferred_channel="imessage", **NIGHT_LIFTER)
    assert handle_start_workout_session(u.id, {}, message_id=_inbound(db, u, "gym")).startswith("ok:")
    at(AFTERNOON_UTC)
    v = make_user(db, preferred_channel="imessage", phone="+15550230001", **dict(FOUNDER, workout_time=None, workout_times=None, confirmed_workout_time=None))
    assert handle_start_workout_session(v.id, {}, message_id=_inbound(db, v, "gym")).startswith("ok:")
    # no inbound behind the call (a proactive / admin path) → never gated
    w = make_user(db, preferred_channel="imessage", phone="+15550230002", **NIGHT_LIFTER)
    assert handle_start_workout_session(w.id, {}).startswith("ok:")


def test_a_phrase_time_and_a_per_day_slot_both_count(db, at):
    from datetime import time
    from workouts import park
    u = make_user(db, **dict(FOUNDER, workout_time="evenings usually", user_timezone="America/Los_Angeles"))
    local = datetime(2026, 10, 22, 14, 59)
    assert park.usual_start(u, local) == time(18, 0)
    assert park.hold_reason(u, "gym tonight", now_utc=AFTERNOON_UTC)                      # 3h out → hold
    u.workout_times = {"by_day": {"thu": "15:30"}}
    assert park.usual_start(u, local) == time(15, 30)
    assert park.hold_reason(u, "gym tonight", now_utc=AFTERNOON_UTC) is None              # 31 min out → send


def test_the_arrival_line_releases_the_parked_card_in_code(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, at):
    from agent_tools import handle_start_workout_session
    from workouts import park
    anthropic_stub.reply_with(lambda kw: "nope")
    at(AFTERNOON_UTC)
    u = make_user(db, preferred_channel="imessage", **NIGHT_LIFTER)
    handle_start_workout_session(u.id, {"template_key": "pull"}, message_id=_inbound(db, u, "gym later"))
    assert park.peek(u.id)["key"] == "pull"
    # chatter doesn't release it
    assert park.release_parked_card(u.id, "ate a sandwich") is None
    assert park.release_parked_card(u.id, "omw to the gym, also had a bagel") is None     # mixed → the model's turn
    assert park.peek(u.id) and _sessions(db, u) == 0
    at(LATE_UTC)
    r = park.release_parked_card(u.id, "Omw to the gym")
    assert r and r["template_key"] == "pull" and _sessions(db, u) == 1
    assert park.peek(u.id) is None
    assert park.release_parked_card(u.id, "here") is None                                 # nothing parked now


def test_a_one_off_list_parks_and_releases_as_that_list(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, at):
    from agent_tools import handle_start_workout_session
    from workouts import park
    from models import SetLog, WorkoutSession
    anthropic_stub.reply_with(lambda kw: "nope")
    at(AFTERNOON_UTC)
    u = make_user(db, preferred_channel="imessage", **NIGHT_LIFTER)
    ex = [{"name": "pushup", "sets": 3, "reps": 12, "bodyweight": True}, {"name": "air squat", "sets": 3, "reps": 15, "bodyweight": True}]
    r = handle_start_workout_session(u.id, {"exercises": ex}, message_id=_inbound(db, u, "ill do the home one tonight"))
    assert r.startswith("NOT sent")
    assert park.peek(u.id)["exercises"] == ex
    at(LATE_UTC)
    rel = park.release_parked_card(u.id, "starting now")
    assert rel and rel["one_off"]
    sid = db.query(WorkoutSession).filter_by(user_id=u.id).one().id
    assert {s.exercise for s in db.query(SetLog).filter_by(session_id=sid)} == {"pushup", "air_squat"}


def test_the_coach_sees_the_parked_block_and_the_flag_turns_it_all_off(db, imessage_on, sidecar_ok, card_ok, sync_threads, anthropic_stub, at, monkeypatch):
    import config
    import agent_loop
    from agent_tools import handle_start_workout_session
    from workouts import park
    anthropic_stub.reply_with(lambda kw: "bet")
    at(AFTERNOON_UTC)
    u = make_user(db, preferred_channel="imessage", **NIGHT_LIFTER)
    handle_start_workout_session(u.id, {}, message_id=_inbound(db, u, "gym tonight"))
    assert park.context_line(u) and "CARD PARKED" in park.context_line(u)
    agent_loop.run_agent_loop(u, "thinking about going tonight", "freeform")
    seen = " ".join(str(b.get("text", "")) if isinstance(b, dict) else str(b)
                    for c in anthropic_stub.calls for b in (c.get("system", []) or []))
    assert "CARD PARKED" in seen and "sends itself" in seen
    monkeypatch.setattr(config, "CARD_EARLY_SEND_GATE_ENABLED", False)
    assert park.context_line(u) is None
    assert park.release_parked_card(u.id, "omw") is None
    v = make_user(db, preferred_channel="imessage", phone="+15550240001", **NIGHT_LIFTER)
    assert handle_start_workout_session(v.id, {}, message_id=_inbound(db, v, "gym tonight")).startswith("ok:")
