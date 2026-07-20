"""
Phase 3 tool 2 — log_workout. Structured workout capture that advances the split
pointer under the Phase-1 policy (named day confirmed; else inferred).
"""

from __future__ import annotations


def test_handle_log_workout_named_day_confirmed(db):
    from tests.factories import make_user
    from agent_tools import handle_log_workout
    from models import get_session, Workout
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="push")
    out = handle_log_workout(user.id, {
        "split_day": "pull",
        "exercises": [{"name": "barbell row", "sets": 4, "reps": 8, "weight": 135}],
    })
    assert out.startswith("ok"), out

    s = get_session()
    try:
        ws = s.query(Workout).filter(Workout.user_id == user.id).all()
    finally:
        s.close()
    assert len(ws) == 1 and ws[0].workout_type == "pull" and ws[0].completed

    p = get_split_pointer(user.id)
    assert p["day"] == "pull" and p["source"] == "confirmed"


def test_handle_log_workout_unnamed_infers_next_day(db):
    from tests.factories import make_user
    from agent_tools import handle_log_workout
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="push")
    handle_log_workout(user.id, {"exercises": [{"name": "bench"}]})
    p = get_split_pointer(user.id)
    assert p["day"] == "pull" and p["source"] == "inferred"


def test_log_workout_via_loop(db, driver, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from models import get_session, Workout
    from split_pointer import get_split_pointer

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_WORKOUT_TOOL_ENABLED", True)

    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        loop_calls.append(1)
        if len(loop_calls) == 1:
            return ToolUse("log_workout", {
                "split_day": "legs",
                "exercises": [{"name": "squat", "sets": 5, "reps": 5, "weight": 275}],
            })
        return "legs done, 275x5 is solid"

    anthropic_stub.reply_with(handler)

    user = make_user(db, current_split="ppl", split_pointer_day="pull")
    replies = driver.send(user, "just did legs, squatted 275x5")

    s = get_session()
    try:
        cnt = s.query(Workout).filter(Workout.user_id == user.id).count()
    finally:
        s.close()
    assert cnt == 1, "log_workout tool did not persist the workout"
    p = get_split_pointer(user.id)
    assert p["day"] == "legs" and p["source"] == "confirmed"
    assert len(replies) >= 1
