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


def test_a_weekday_name_as_the_reminder_date_means_that_weekday_never_today():
    """Live 2026-10-18 18:37 PT (user 40): date_str="wednesday" fell through to TODAY and
    armed a 10pm "gym" ping for that night (reminder 9, cancelled by hand)."""
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone
    from reminders import next_fire_at
    tz = ZoneInfo("America/Los_Angeles")
    sun_evening = datetime(2026, 10, 19, 1, 37, tzinfo=timezone.utc).replace(tzinfo=None)      # Sun 18:37 PT
    wed = next_fire_at(tz, "22:00", None, after=sun_evening, date_str="wednesday")
    assert wed == datetime(2026, 10, 22, 5, 0)                                                   # Wed 22:00 PT
    assert next_fire_at(tz, "22:00", None, after=sun_evening, date_str="next wed") == wed
    assert next_fire_at(tz, "22:00", None, after=sun_evening, date_str="Sunday") == datetime(2026, 10, 19, 5, 0)   # still ahead today
    assert next_fire_at(tz, "17:00", None, after=sun_evening, date_str="sunday") == datetime(2026, 10, 26, 0, 0)   # passed → next sunday
    assert next_fire_at(tz, "22:00", None, after=sun_evening, date_str="someday") is None                          # unreadable → error, not today
    assert next_fire_at(tz, "22:00", None, after=sun_evening, date_str="2026-10-21") == wed
