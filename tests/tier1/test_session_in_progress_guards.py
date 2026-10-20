"""
Founder, 2026-10-19 00:00 PT: "my worry is that the coach is going to start nagging him to go
to sleep while he's clearly just gotten to the gym. he probably got busy and fell behind on
his schedule, it happens." Live: user 40's card was built at 18:38, he got to the gym at
23:55; the abandon sweep (6h from START) would have closed it mid-workout at 00:38.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import FOUNDER, imessage_on, sidecar_ok, card_ok, sync_threads  # noqa: F401


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _open(db, user):
    from workouts.start import start_workout_session
    return start_workout_session(user.id, "push")["session_id"]


def _tap(sid, minutes_ago):
    from models import get_session, SetLog
    s = get_session()
    try:
        r = s.query(SetLog).filter(SetLog.session_id == sid, SetLog.done.is_(False)).order_by(SetLog.id).first()
        r.done, r.actual_weight, r.actual_reps, r.source = True, r.planned_weight, r.planned_reps, "card"
        r.done_at = _now() - timedelta(minutes=minutes_ago)
        s.commit()
    finally:
        s.close()


def test_abandon_sweep_clock_runs_from_the_last_set_not_the_start(db, imessage_on, sidecar_ok, card_ok, anthropic_stub):
    from workouts.session_ops import abandon_stale, session_in_progress
    from models import get_session, WorkoutSession
    anthropic_stub.reply_with(lambda kw: "nope")
    u = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _open(db, u)
    s = get_session()
    try:
        s.get(WorkoutSession, sid).started_at = _now() - timedelta(hours=5, minutes=30)      # built at 18:38
        s.commit()
    finally:
        s.close()
    _tap(sid, 6)                                                                            # a set 6 min ago (he just got there)
    assert abandon_stale(now=_now() + timedelta(hours=1)) == 0                              # 00:38 on the old clock: stays open
    assert session_in_progress(u.id) == (sid, 6)
    assert session_in_progress(u.id, within_min=5) is None
    assert abandon_stale(now=_now() + timedelta(hours=7)) == 1                              # 6h after the last set: swept
    assert session_in_progress(u.id) is None


def test_heartbeat_hard_gates_a_session_in_progress(db, monkeypatch, sms_capture, anthropic_stub, imessage_on, sidecar_ok, card_ok):
    import heartbeat, config
    from tests.tier1.test_heartbeat import _allow, _ticks
    anthropic_stub.reply_with(lambda kw: "nope")
    u = make_user(db, preferred_channel="imessage", **FOUNDER)
    _allow(monkeypatch, u)
    sid = _open(db, u)
    _tap(sid, 10)
    heartbeat.heartbeat_tick(u.id)
    assert _ticks(u.id)[-1].reason == "guardrail:workout_in_progress"
    monkeypatch.setattr(config, "HEARTBEAT_SESSION_GUARD_ENABLED", False)
    heartbeat.heartbeat_tick(u.id)
    assert _ticks(u.id)[-1].reason != "guardrail:workout_in_progress"


def test_the_loop_is_told_they_are_mid_workout_and_the_voice_bans_sleep_talk(db, anthropic_stub, imessage_on, sidecar_ok, card_ok):
    import agent_loop
    from agent_loop import _voice_prompt
    anthropic_stub.reply_with(lambda kw: "ok")
    u = make_user(db, preferred_channel="imessage", **FOUNDER)
    sid = _open(db, u)
    _tap(sid, 3)
    anthropic_stub.calls.clear()
    agent_loop.run_agent_loop(u, "just did the stairmaster, starting the lifts now", "freeform")
    texts = []
    for c in anthropic_stub.calls:
        for b in c.get("system", []) or []:
            texts.append(b.get("text", "") if isinstance(b, dict) else str(b))
        for m in c.get("messages", []) or []:
            texts.append(str(m.get("content")))
    seen = " ".join(texts)
    assert "## SESSION IN PROGRESS" in seen and "no 'go to sleep'" in seen and f"card #{sid} open" in seen
    v = " ".join(_voice_prompt().split())
    assert "Mid-workout (SESSION IN PROGRESS)" in v and 'never "go to sleep"' in v
