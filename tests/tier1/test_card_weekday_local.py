"""
The card's and the summary's weekday is the USER's weekday. ws.date is naive UTC, so a card
built Sunday 6:31pm PT read "full body · mon" (live 2026-10-18, user 40, msg 6951).
"""
from __future__ import annotations

from datetime import datetime

from tests.factories import make_user


def test_sunday_evening_pacific_is_sunday_on_the_card_and_in_the_summary(db):
    from models import get_session, WorkoutSession, SetLog
    from card_page import build_state
    from workouts.summary import summarize
    u = make_user(db, user_timezone="America/Los_Angeles")
    s = get_session()
    try:
        ws = WorkoutSession(user_id=u.id, date=datetime(2026, 10, 19, 1, 31), template_key="full_body", status="active")  # Mon 01:31 UTC = Sun 18:31 PT
        s.add(ws); s.flush()
        s.add(SetLog(session_id=ws.id, exercise="leg_press", exercise_label="leg press", set_index=0, planned_weight=130, planned_reps=10, done=False))
        s.commit()
        assert build_state(s, ws)["session"]["weekday"] == "sun"
        assert summarize(s, ws)["weekday"] == "sun"
        ws.date = datetime(2026, 10, 19, 17, 0)           # Mon 10am PT
        s.commit()
        assert build_state(s, ws)["session"]["weekday"] == "mon"
    finally:
        s.close()
