"""Heartbeat guardrails: exclude waitlisted users + standing overnight quiet hours.

Live 2026-09-21: clearing HEARTBEAT_ALLOWLIST opened proactive sends to every active
user, which (a) swept in waitlisted accounts (active=true, not onboarded) and (b) had
no standing night gate, so nudges landed at 1am/5am. These are the two fixes."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import config


def _at_local(hour, minute=0, tz="America/Los_Angeles"):
    """An aware-UTC instant for the given local wall-clock time (for injecting `now`)."""
    return datetime(2026, 9, 21, hour, minute, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)


@pytest.fixture
def quiet_on(monkeypatch):
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_QUIET_START_HOUR", 21)
    monkeypatch.setattr(config, "HEARTBEAT_QUIET_END_HOUR", 8)
    yield


# ─── waitlist exclusion ───────────────────────────────────────────────────────

def test_guardrail_blocks_waitlisted(db):
    from tests.factories import make_user
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, waitlist_status="pending")
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(14)) == "waitlisted"
    finally:
        s.close()


def test_guardrail_allows_non_waitlisted(db):
    from tests.factories import make_user
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, waitlist_status=None)   # onboarded/active
    s = get_session()
    try:
        # daytime, no other blocker → not gated
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(14)) is None
    finally:
        s.close()


def test_heartbeat_all_query_excludes_pending(db, monkeypatch):
    from tests.factories import make_user
    import heartbeat
    monkeypatch.setattr(config, "HEARTBEAT_ENABLED", True)
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    normal = make_user(db, waitlist_status=None, phone="+15105550001")
    pending = make_user(db, waitlist_status="pending", phone="+15105550002")
    swept = []
    monkeypatch.setattr(heartbeat, "heartbeat_tick", lambda uid: swept.append(uid))
    heartbeat.heartbeat_all()
    assert normal.id in swept
    assert pending.id not in swept, "waitlisted user was swept into the heartbeat"


def test_waitlist_takes_precedence_over_quiet(db, quiet_on):
    """A pending user is blocked as 'waitlisted' even at 2pm (quiet passes)."""
    from tests.factories import make_user
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, waitlist_status="pending")
    s = get_session()
    try:
        assert guardrail_reason(s.get(User, user.id), s, now=_at_local(14)) == "waitlisted"
    finally:
        s.close()


# ─── standing quiet hours ─────────────────────────────────────────────────────

@pytest.mark.parametrize("hour,quiet", [
    (1, True), (5, True), (23, True), (21, True),   # night → quiet
    (7, True), (8, False), (9, False), (14, False), (20, False),  # 8am opens, 9pm not yet
])
def test_in_standing_quiet_hours_default_window(quiet_on, hour, quiet):
    from heartbeat import _in_standing_quiet_hours

    class U:
        user_timezone = "America/Los_Angeles"
        sleep_time = None
        wake_time = None
    assert _in_standing_quiet_hours(U(), now=_at_local(hour)) is quiet


def test_quiet_window_extends_for_late_riser(quiet_on):
    from heartbeat import _in_standing_quiet_hours

    class U:
        user_timezone = "America/Los_Angeles"
        sleep_time = None
        wake_time = "11am"
    assert _in_standing_quiet_hours(U(), now=_at_local(10)) is True    # still asleep at 10
    assert _in_standing_quiet_hours(U(), now=_at_local(11, 30)) is False


def test_quiet_window_extends_for_early_sleeper(quiet_on):
    from heartbeat import _in_standing_quiet_hours

    class U:
        user_timezone = "America/Los_Angeles"
        sleep_time = "8pm"
        wake_time = None
    assert _in_standing_quiet_hours(U(), now=_at_local(20, 30)) is True   # asleep by 8:30
    assert _in_standing_quiet_hours(U(), now=_at_local(19)) is False


def test_quiet_flag_off_never_gates(monkeypatch):
    from heartbeat import _in_standing_quiet_hours
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", False)

    class U:
        user_timezone = "America/Los_Angeles"
        sleep_time = None
        wake_time = None
    assert _in_standing_quiet_hours(U(), now=_at_local(2)) is False   # 2am, flag off


def test_guardrail_blocks_at_night_allows_daytime(db, quiet_on):
    from tests.factories import make_user
    from models import get_session, User
    from heartbeat import guardrail_reason
    user = make_user(db, waitlist_status=None)
    s = get_session()
    try:
        u = s.get(User, user.id)
        assert guardrail_reason(u, s, now=_at_local(1)) == "quiet_hours_standing"   # 1am
        assert guardrail_reason(u, s, now=_at_local(14)) is None                    # 2pm
    finally:
        s.close()
