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


# ─── 2026-09-12 live: cardio, same-day append, pointer rollback ──────────────
# A 2-mile run was logged as split_day=full_body (reps=2) and moved the pointer off
# push; deleting the row left the pointer wrong; "i went" → "bench 135x3x8" was
# create+delete twice in one evening.


def _workouts(user_id):
    from models import get_session, Workout
    s = get_session()
    try:
        return [(w.id, w.workout_type, list(w.exercises or []), w.deleted_at is not None)
                for w in s.query(Workout).filter(Workout.user_id == user_id).order_by(Workout.id).all()]
    finally:
        s.close()


def test_cardio_never_touches_the_pointer(db):
    from tests.factories import make_user
    from agent_tools import handle_log_workout
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="push", split_pointer_source="confirmed")
    out = handle_log_workout(user.id, {
        "cardio": True,
        "split_day": "full_body",  # the model's live mistake — must be ignored
        "exercises": [{"name": "run", "distance_miles": 2, "duration_min": 21}],
        "notes": "grinnell loop, 10:40 pace",
    })
    assert out.startswith("ok") and "cardio" in out and "untouched" in out, out
    ws = _workouts(user.id)
    assert len(ws) == 1 and ws[0][1] == "cardio"
    assert ws[0][2] == [{"name": "run", "distance_miles": 2, "duration_min": 21}]
    p = get_split_pointer(user.id)
    assert p["day"] == "push" and p["source"] == "confirmed"


def test_same_day_relog_appends_to_todays_session(db):
    from tests.factories import make_user
    from agent_tools import handle_log_workout
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="pull")
    first = handle_log_workout(user.id, {"split_day": "push"})        # "i went"
    assert first.startswith("ok: logged"), first
    second = handle_log_workout(user.id, {                            # "135 x 3 x 8"
        "split_day": "push",
        "exercises": [{"name": "bench press", "sets": 3, "reps": 8, "weight": 135}],
        "notes": "quick one with friends",
    })
    assert second.startswith("ok: added 1 exercises to today's push"), second
    ws = _workouts(user.id)
    assert len(ws) == 1, ws                       # ONE row, nothing deleted
    assert ws[0][2][0]["weight"] == 135
    assert get_split_pointer(user.id)["day"] == "push"


def test_same_day_unnamed_then_named_relabels_and_confirms(db):
    """'went to the gym' (inferred) then 'it was push' → the same row gets the name
    and the pointer becomes a confirmed push."""
    from tests.factories import make_user
    from agent_tools import handle_log_workout
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="legs")
    handle_log_workout(user.id, {"exercises": [{"name": "bench"}]})   # inferred → push
    assert get_split_pointer(user.id)["source"] == "inferred"
    out = handle_log_workout(user.id, {"split_day": "push", "exercises": [{"name": "ohp"}]})
    assert "today's push" in out, out
    ws = _workouts(user.id)
    assert len(ws) == 1 and ws[0][1] == "push" and [e["name"] for e in ws[0][2]] == ["bench", "ohp"]
    p = get_split_pointer(user.id)
    assert p["day"] == "push" and p["source"] == "confirmed"


def test_cardio_and_a_lift_on_the_same_day_are_separate_rows(db):
    from tests.factories import make_user
    from agent_tools import handle_log_workout

    user = make_user(db, current_split="ppl", split_pointer_day="pull")
    handle_log_workout(user.id, {"split_day": "push", "exercises": [{"name": "bench"}]})
    handle_log_workout(user.id, {"cardio": True, "exercises": [{"name": "run", "distance_miles": 2}]})
    ws = _workouts(user.id)
    assert [w[1] for w in ws] == ["push", "cardio"]


def test_deleting_a_logged_session_rolls_the_pointer_back(db):
    from tests.factories import make_user
    from agent_tools import handle_log_workout, handle_manage_log
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="push", split_pointer_source="confirmed")
    before = get_split_pointer(user.id)
    out = handle_log_workout(user.id, {"split_day": "full_body"})    # the live mistake
    wid = int(out.split("id=")[1].split(",")[0].rstrip(")"))
    assert get_split_pointer(user.id)["day"] == "full_body"

    res = handle_manage_log(user.id, {"action": "delete", "entity": "workout", "id": wid})
    assert res.startswith("ok: deleted workout") and "rolled back to push" in res, res
    after = get_split_pointer(user.id)
    assert after["day"] == "push" and after["source"] == "confirmed"
    assert after["at"] == before["at"]


def test_delete_leaves_pointer_alone_if_a_later_log_moved_it(db):
    from tests.factories import make_user
    from agent_tools import handle_log_workout, handle_manage_log
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl", split_pointer_day="push")
    out = handle_log_workout(user.id, {"split_day": "pull", "date": "yesterday"})
    wid = int(out.split("id=")[1].split(",")[0].rstrip(")"))
    handle_log_workout(user.id, {"split_day": "legs"})              # later log owns the pointer
    res = handle_manage_log(user.id, {"action": "delete", "entity": "workout", "id": wid})
    assert res == f"ok: deleted workout id={wid}", res
    assert get_split_pointer(user.id)["day"] == "legs"


def test_delete_of_first_ever_log_clears_the_pointer(db):
    from tests.factories import make_user
    from agent_tools import handle_log_workout, handle_manage_log
    from split_pointer import get_split_pointer

    user = make_user(db, current_split="ppl")
    assert get_split_pointer(user.id) is None
    out = handle_log_workout(user.id, {"split_day": "legs"})
    wid = int(out.split("id=")[1].split(",")[0].rstrip(")"))
    res = handle_manage_log(user.id, {"action": "delete", "entity": "workout", "id": wid})
    assert "cleared" in res, res
    assert get_split_pointer(user.id) is None


def test_cardio_via_loop_keeps_pointer(db, driver, monkeypatch, anthropic_stub):
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user
    from split_pointer import get_split_pointer

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "LOG_WORKOUT_TOOL_ENABLED", True)
    calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"
        calls.append(1)
        if len(calls) == 1:
            return ToolUse("log_workout", {"cardio": True,
                                           "exercises": [{"name": "run", "distance_miles": 2}]})
        return "logged it, 2 miles as cardio"

    anthropic_stub.reply_with(handler)
    user = make_user(db, current_split="ppl", split_pointer_day="push", split_pointer_source="confirmed")
    driver.send(user, "yeah that's me lol")
    ws = _workouts(user.id)
    assert [w[1] for w in ws] == ["cardio"]
    assert get_split_pointer(user.id)["day"] == "push"


def test_today_log_is_the_users_local_day_not_utc(db):
    """CI (UTC) failed test_log_workout_dates_a_past_session… after 00:00Z: the daily
    log was looked up by the row's UTC calendar date but the user's LOCAL date, so
    confirm and check hit different rows every evening. At any instant at least one
    of these two zones is on a different calendar date from UTC."""
    from tests.factories import make_user
    from models import get_session, DailyLog, confirm_workout_today, is_workout_confirmed_today
    for tz in ("Pacific/Kiritimati", "Pacific/Pago_Pago", "America/Los_Angeles"):
        user = make_user(db, user_timezone=tz)
        assert not is_workout_confirmed_today(user.id)
        confirm_workout_today(user.id)
        assert is_workout_confirmed_today(user.id), tz
        s = get_session()
        try:
            n = s.query(DailyLog).filter(DailyLog.user_id == user.id).count()
        finally:
            s.close()
        assert n == 1, f"{tz}: {n} daily_log rows for one local day"
