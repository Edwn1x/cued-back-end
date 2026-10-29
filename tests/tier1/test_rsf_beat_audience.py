"""
The RSF "gym's quiet, quick X?" beat only goes to people who could act on it.

Live 2026-10-28 20:05: user 80 (home gym — dumbbells; 26, desk job, not a student; a
week-long chest cough, "ill tell you once I feel better") and user 42 (silent since
09-22) both got "gym's quiet right now. quick push?". gym_beats.propose gated only on
open hours, occupancy, once a day, an active session and planned_today — never on who
the user is or whether they've been around.

Four gates, all code: (1) a campus gym is for college students (connect_offers.is_student);
(2) home_gym / bodyweight equipment doesn't lift at the RSF; (3) a user quiet for more than
RSF_BEAT_MAX_QUIET_DAYS gets no beat (the heartbeat already backs off for them);
(4) a training pause the coach set (pause_training: "sick", "travelling", "exams") holds
beats, the program's gym ping and the TRAINING GAP nudge until it lapses.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import config
from tests.factories import make_user
from tests.tier1.test_rsf_meter_line import _reading, _now_local, rsf_on  # noqa: F401

TZ = ZoneInfo("America/Los_Angeles")
STUDENT = dict(name="Alexey", onboarding_step=3, current_split="ppl", split_pointer_day="pull",
               confirmed_training_days="mon,tue,wed,thu,fri,sat,sun", workout_time="17:00",
               user_timezone="America/Los_Angeles", occupation="physics student", equipment="full_gym")


def _fresh(db, user):
    from models import get_session, User, Message
    s = get_session()
    return s, s.get(User, user.id)


def _texted(db, user, days_ago: float):
    from models import Message
    db.add(Message(user_id=user.id, direction="in", body="hey",
                   created_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)))
    db.commit()


def test_knob_defaults():
    assert config.RSF_BEAT_AUDIENCE_GATE_ENABLED is True
    assert config.RSF_BEAT_MAX_QUIET_DAYS == 7
    assert config.PAUSE_TRAINING_TOOL_ENABLED is True


def test_a_student_with_a_full_gym_who_texted_recently_still_gets_the_beat(db, rsf_on):
    import gym_beats
    user = make_user(db, **STUDENT)
    _texted(db, user, 0.5)
    _reading(db, 18)
    s, u = _fresh(db, user)
    try:
        b = gym_beats.propose(u, s, _now_local(15))
        assert b and b.kind == "dead", b
    finally:
        s.close()


def test_andrew_home_gym_desk_job_gets_no_beat(db, rsf_on, caplog):
    """Live u80: equipment home_gym, occupation 'desk job' — both gates say no."""
    import logging, gym_beats
    user = make_user(db, **dict(STUDENT, equipment="home_gym", occupation="desk job"))
    _texted(db, user, 0.3)
    _reading(db, 18)
    s, u = _fresh(db, user)
    try:
        with caplog.at_level(logging.INFO):
            assert gym_beats.propose(u, s, _now_local(15)) is None
        assert "GYM_BEAT_SKIPPED" in caplog.text
    finally:
        s.close()


@pytest.mark.parametrize("equipment", ["home_gym", "bodyweight"])
def test_equipment_that_is_not_a_gym_gets_no_beat(db, rsf_on, equipment):
    import gym_beats
    user = make_user(db, **dict(STUDENT, equipment=equipment))
    _texted(db, user, 0.3)
    _reading(db, 18)
    s, u = _fresh(db, user)
    try:
        assert gym_beats.propose(u, s, _now_local(15)) is None
    finally:
        s.close()


def test_a_non_student_gets_no_campus_gym_beat(db, rsf_on):
    import gym_beats
    user = make_user(db, **dict(STUDENT, occupation="desk job"))
    _texted(db, user, 0.3)
    _reading(db, 18)
    s, u = _fresh(db, user)
    try:
        assert gym_beats.propose(u, s, _now_local(15)) is None
    finally:
        s.close()


def test_a_user_silent_for_weeks_gets_no_beat(db, rsf_on):
    """Live u42: silent since 09-22 (36 days) and still beaten. 8 days quiet → no; 6 → yes."""
    import gym_beats
    quiet = make_user(db, **STUDENT)
    _texted(db, quiet, 8)
    recent = make_user(db, **dict(STUDENT, phone="+15550009999"))
    _texted(db, recent, 6)
    _reading(db, 18)
    s, _ = _fresh(db, quiet)
    try:
        from models import User
        assert gym_beats.propose(s.get(User, quiet.id), s, _now_local(15)) is None
        assert gym_beats.propose(s.get(User, recent.id), s, _now_local(15)) is not None
    finally:
        s.close()


def test_audience_gate_flag_off_is_the_old_behaviour(db, rsf_on, monkeypatch):
    import gym_beats
    monkeypatch.setattr(config, "RSF_BEAT_AUDIENCE_GATE_ENABLED", False)
    user = make_user(db, **dict(STUDENT, equipment="home_gym", occupation="desk job"))
    _texted(db, user, 20)
    _reading(db, 18)
    s, u = _fresh(db, user)
    try:
        assert gym_beats.propose(u, s, _now_local(15)) is not None
    finally:
        s.close()


# ─── the training pause ────────────────────────────────────────────────────────

def test_pause_training_tool_sets_the_column_and_resume_clears_it(db):
    from agent_tools import handle_pause_training, dispatch_tool, PAUSE_TRAINING_TOOL
    from models import get_session, User
    assert PAUSE_TRAINING_TOOL["name"] == "pause_training"
    user = make_user(db, **STUDENT)
    out = handle_pause_training(user.id, {"days": 5, "reason": "chest cough"})
    assert out.startswith("ok") and "5 days" in out and "chest cough" in out, out
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert u.training_paused_until and u.training_pause_reason == "chest cough"
        assert (u.training_paused_until - datetime.now(timezone.utc).replace(tzinfo=None)) > timedelta(days=4, hours=23)
    finally:
        s.close()
    out = dispatch_tool("pause_training", {"days": 0}, user.id)
    assert "resumed" in out, out
    s = get_session()
    try:
        assert s.get(User, user.id).training_paused_until is None
    finally:
        s.close()
    assert dispatch_tool("pause_training", {"days": 400}, user.id).startswith("error")


def test_a_paused_user_gets_no_beat_until_the_pause_lapses(db, rsf_on):
    import gym_beats
    from agent_tools import handle_pause_training
    from models import get_session, User
    user = make_user(db, **STUDENT)
    _texted(db, user, 0.2)
    _reading(db, 18)
    handle_pause_training(user.id, {"days": 3, "reason": "sick"})
    s = get_session()
    try:
        assert gym_beats.propose(s.get(User, user.id), s, _now_local(15)) is None
        u = s.get(User, user.id)
        u.training_paused_until = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)   # lapsed
        s.commit()
        assert gym_beats.propose(s.get(User, user.id), s, _now_local(15)) is not None
    finally:
        s.close()


def test_a_paused_user_gets_no_program_gym_ping_and_no_training_gap_nudge(db, monkeypatch, caplog):
    import logging, reminders
    from agent_tools import handle_pause_training
    from heartbeat import _training_gap_signal
    from models import get_session, User, Reminder
    monkeypatch.setattr("sms.send_sms", lambda phone, body, **kw: True)
    user = make_user(db, **dict(STUDENT, confirmed_training_days="mon,tue,wed,thu,fri,sat"))
    handle_pause_training(user.id, {"days": 4, "reason": "sick"})
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    row = Reminder(user_id=user.id, text="gym", local_time="17:00", recur_days="mon,tue,wed,thu,fri,sat",
                   fire_at=now, active=True, source="program")
    db.add(row)
    db.commit()
    with caplog.at_level(logging.INFO):
        assert reminders._fire_one(row.id, now) is False
    assert "reason=training_paused" in caplog.text
    db.refresh(row)
    assert row.active and row.sent_count == 0 and row.fire_at > now
    s = get_session()
    try:
        assert _training_gap_signal(s.get(User, user.id), s) is None
    finally:
        s.close()


def test_the_loop_context_names_the_pause(db):
    from agent_tools import handle_pause_training
    from agent_loop import build_loop_context
    from models import get_session, User
    user = make_user(db, **STUDENT)
    handle_pause_training(user.id, {"days": 3, "reason": "chest cough"})
    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "## TRAINING PAUSED" in ctx and "chest cough" in ctx, ctx


def test_pause_tool_is_registered_and_flag_gated(monkeypatch):
    from capabilities import CAPABILITIES
    from agent_tools import _HANDLERS
    caps = {c.id: c for c in CAPABILITIES}
    assert "training_pause" in caps and caps["training_pause"].tools == ("pause_training",)
    assert "pause_training" in _HANDLERS
    from types import SimpleNamespace as NS
    assert caps["training_pause"].enabled(NS()) is True
    monkeypatch.setattr(config, "PAUSE_TRAINING_TOOL_ENABLED", False)
    assert caps["training_pause"].enabled(NS()) is False


def test_an_unknown_occupation_is_not_treated_as_a_non_student(db, rsf_on):
    """Only a KNOWN non-student is gated: the sign-up form doesn't always capture it."""
    import gym_beats
    user = make_user(db, **dict(STUDENT, occupation=None))
    _texted(db, user, 0.3)
    _reading(db, 18)
    s, u = _fresh(db, user)
    try:
        assert gym_beats.propose(u, s, _now_local(15)) is not None
    finally:
        s.close()
