"""
Phase 1 — Event detection floor (precision-biased) + local-day reader.
"""

from __future__ import annotations

import pytest


# ── detection: precision (matches) ───────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "just got back from the gym",
    "already went",
    "just finished my workout",
    "already hit the gym",
    "done with my workout",
    "just lifted",
    "got my workout in",
])
def test_gym_completed_phrasings_match(msg):
    from events import detect_event_signals
    types = [s["event_type"] for s in detect_event_signals(msg)]
    assert "went_to_gym" in types, msg


# ── detection: precision (does NOT match futures/hypotheticals/prior-day) ─────

@pytest.mark.parametrize("msg", [
    "gonna hit the gym later",
    "might go to the gym",
    "thinking about working out",
    "went to the gym yesterday",
    "crushed it at the gym last night",
    "should i go to the gym today?",
])
def test_gym_non_completed_phrasings_do_not_match(msg):
    from events import detect_event_signals
    types = [s["event_type"] for s in detect_event_signals(msg)]
    assert "went_to_gym" not in types, msg


def test_in_class_with_end_time_parses():
    from events import detect_event_signals
    sig = [s for s in detect_event_signals("in class till 2") if s["event_type"] == "in_class"]
    assert sig and sig[0]["end"] == (2, 0, None)


def test_in_class_without_end_time():
    from events import detect_event_signals
    sig = [s for s in detect_event_signals("ugh stuck in class") if s["event_type"] == "in_class"]
    assert sig and sig[0]["end"] is None


def test_have_class_later_is_not_in_class_now():
    from events import detect_event_signals
    types = [s["event_type"] for s in detect_event_signals("i have class at 3")]
    assert "in_class" not in types


# ── reader: local-day windowing (flag #1 — the timezone bug) ─────────────────

def test_event_at_11pm_local_lands_in_local_today_not_utc_tomorrow(db):
    """A 11pm-Pacific event is ~6-7am-UTC tomorrow; a naive UTC window would
    mis-bucket it. The local-day reader must still count it as today."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from tests.factories import make_user
    from events import record_event, went_to_gym_today

    user = make_user(db, user_timezone="America/Los_Angeles")
    tz = ZoneInfo("America/Los_Angeles")
    eleven_pm_local = datetime.now(tz).replace(hour=23, minute=0, second=0, microsecond=0)
    occurred_utc = eleven_pm_local.astimezone(timezone.utc).replace(tzinfo=None)

    record_event(user.id, "went_to_gym", occurred_at=occurred_utc)
    assert went_to_gym_today(user.id), "11pm-local event was lost to a UTC-day window"


def test_in_class_now_respects_end_time(db):
    from datetime import datetime, timezone, timedelta
    from tests.factories import make_user
    from events import record_event, in_class_now

    user = make_user(db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    record_event(user.id, "in_class", ends_at=now + timedelta(hours=1))
    assert in_class_now(user.id) is True

    from models import get_session, Event
    s = get_session()
    try:
        s.query(Event).filter_by(user_id=user.id).delete()
        s.commit()
    finally:
        s.close()
    record_event(user.id, "in_class", ends_at=now - timedelta(hours=1))
    assert in_class_now(user.id) is False


def test_inbound_message_creates_event_end_to_end(db, driver):
    """The webhook floor writes the event synchronously from a real inbound."""
    from tests.factories import make_user
    from events import went_to_gym_today

    user = make_user(db)
    driver.send(user, "just got back from the gym, chest day done")
    assert went_to_gym_today(user.id), "sync inbound detector did not record the event"
