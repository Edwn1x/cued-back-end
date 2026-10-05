"""
Tier-2 (live) — coaching lessons end to end with the real model.

1. A correction turn → the coach fixes the data AND a lesson lands in `coaching_lessons`
   (either writer: the reactive remember call, or the background extractor run
   synchronously here the way app.py would spawn it).
2. With a lesson in context ("don't restate the protein nudge…"), a turn that invites
   exactly that nudge obeys the lesson.
3. A one-off number fix with no pattern does NOT become a lesson (extractor precision).

Run: pytest --run-tier2 -s tests/tier2/test_coaching_lessons_live.py
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.tier2


def _flags(monkeypatch):
    import config
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "LESSONS_ENABLED", "LESSONS_EXTRACT_ENABLED",
              "REMEMBER_TOOL_ENABLED", "LOG_MEAL_TOOL_ENABLED", "MANAGE_LOG_TOOL_ENABLED"):
        monkeypatch.setattr(config, f, True)


def _lessons(user_id):
    from models import get_session, User
    s = get_session()
    try:
        return [e["text"] for e in ((s.get(User, user_id).user_profile_memory or {}).get("coaching_lessons") or [])]
    finally:
        s.close()


def _persist(user_id, direction, body):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user_id, direction=direction, body=body, message_type="freeform"))
        s.commit()
    finally:
        s.close()


def test_correction_produces_a_lesson(db, monkeypatch):
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from agent_tools import handle_log_meal
    from lessons import extract_and_store_lesson_task
    _flags(monkeypatch)
    user = make_user(db, name="Sam")

    # Turn 1: the coach's prior claim, mirrored into the Messages table like prod does.
    handle_log_meal(user.id, {"items": [{"name": "grilled chicken breast", "grams": 170,
                                        "calories": 280, "protein_g": 52, "carbs_g": 0, "fat_g": 6}],
                              "meal_type": "dinner"})
    _persist(user.id, "in", "[photo] dinner")
    _persist(user.id, "out", "logged ~6oz grilled chicken breast, 280 cal / 52g protein")

    # Turn 2: the correction.
    msg = "no that was pork chops not chicken, you keep calling everything chicken from photos"
    _persist(user.id, "in", msg)
    reply = run_agent_loop(user, msg, "freeform")
    _persist(user.id, "out", reply)
    print(f"\n[LESSON] reply: {reply}")
    print(f"[LESSON] lessons after reactive turn: {_lessons(user.id)}")

    # The background learner, run synchronously (app.py spawns it as a daemon thread).
    extract_and_store_lesson_task(user.id, msg, reply)
    lessons = _lessons(user.id)
    print(f"[LESSON] lessons after extractor: {lessons}")

    assert lessons, "a clear, generalizable correction produced no lesson from either writer"
    joined = " ".join(lessons).lower()
    assert any(k in joined for k in ("photo", "meat", "protein source", "chicken", "verify", "ask")), joined
    assert "sam" not in joined and " he " not in f" {joined} " and " his " not in f" {joined} "
    assert len(lessons) == 1, "the background writer must yield to a fresh reactive lesson (one lesson, not a paraphrase pair)"


def test_lesson_in_context_changes_behavior(db, monkeypatch):
    from tests.factories import make_user
    from agent_loop import run_agent_loop
    from models import User
    from memory import _new_entry
    from sqlalchemy.orm.attributes import flag_modified
    _flags(monkeypatch)
    user = make_user(db, name="Sam", goal="muscle_building", protein_target=160)
    u = db.get(User, user.id)
    prof = dict(u.user_profile_memory or {})
    prof["coaching_lessons"] = [_new_entry(
        "Don't bring up the protein gap unprompted; they've asked you to stop nagging about protein — "
        "answer what they asked and leave it.")]
    u.user_profile_memory = prof
    flag_modified(u, "user_profile_memory")
    db.commit()
    db.expire_all()
    user = db.get(User, user.id)

    reply = run_agent_loop(user, "just had a bowl of cereal, heading to class", "freeform")
    print(f"\n[LESSON-OBEY] reply: {reply}")
    low = reply.lower()
    assert "protein" not in low, "restated the protein nudge despite the lesson"


def test_one_off_number_fix_is_not_a_lesson(db, monkeypatch):
    from tests.factories import make_user
    from lessons import extract_and_store_lesson_task, looks_like_correction
    _flags(monkeypatch)
    user = make_user(db, name="Sam")
    _persist(user.id, "out", "logged 3 eggs scrambled, ~210 cal")
    msg = "it was 2 eggs not 3"
    assert looks_like_correction(msg)          # the gate fires — precision is the model's job here
    extract_and_store_lesson_task(user.id, msg, "fixed, 2 eggs")
    lessons = _lessons(user.id)
    print(f"\n[LESSON-ONEOFF] lessons: {lessons}")
    assert not lessons, f"a one-off count fix became a lesson: {lessons}"
