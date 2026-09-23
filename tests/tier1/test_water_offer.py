"""
Water-reminder offer (water_offer.py): the one-line, once-only, code-handled invitation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.factories import make_user


@pytest.fixture
def offer_on(monkeypatch):
    import config
    for f in ("WATER_OFFER_ENABLED", "WATER_REMINDERS_ENABLED", "REMINDERS_ENABLED"):
        monkeypatch.setattr(config, f, True)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _u(uid):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, uid)
    finally:
        s.close()


def _reminders(uid):
    from reminders import active_reminders
    return active_reminders(uid)


def _daytime_tz():
    offset = (14 - datetime.now(timezone.utc).hour) % 24
    if offset > 14:
        offset -= 24
    return f"Etc/GMT-{offset}" if offset >= 0 else f"Etc/GMT+{-offset}"


def test_send_offer_once_and_marks_status(db, offer_on, sms_capture):
    from water_offer import send_offer, OFFER_TEXT
    u = make_user(db)
    assert send_offer(u.id, source="test") is True
    assert sms_capture[-1][1] == OFFER_TEXT
    row = _u(u.id)
    assert row.water_offer_status == "offered" and row.water_offered_at
    assert send_offer(u.id) is False and len(sms_capture) == 1     # never twice


def test_send_offer_skips_ineligible(db, offer_on, sms_capture, monkeypatch):
    import config
    from water_offer import send_offer
    from reminders import create_reminder
    assert send_offer(make_user(db, onboarding_step=2).id) is False     # still onboarding
    assert send_offer(make_user(db, active=False).id) is False
    has = make_user(db, wake_time="07:00", sleep_time="23:00")
    create_reminder(has.id, "drink water", None, every_hours=2)
    assert send_offer(has.id) is False and _u(has.id).water_offer_status == "yes"   # already has one
    monkeypatch.setattr(config, "WATER_OFFER_ENABLED", False)
    assert send_offer(make_user(db).id) is False
    assert sms_capture == []


def test_yes_creates_the_interval_reminder_in_code(db, offer_on, driver, anthropic_stub):
    from water_offer import send_offer, YES_REPLY
    u = make_user(db, wake_time="07:00", sleep_time="23:00")
    send_offer(u.id)
    replies = driver.send(u, "yes")
    assert replies == [YES_REPLY.format(n=3)], replies
    assert anthropic_stub.calls == [], "the yes must not reach the model"
    rows = _reminders(u.id)
    assert len(rows) == 1 and rows[0].every_hours == 3 and rows[0].source == "offer" and rows[0].text == "drink water"
    assert _u(u.id).water_offer_status == "yes"


def test_yes_with_an_interval_wins(db, offer_on, driver):
    from water_offer import send_offer
    u = make_user(db, wake_time="07:00", sleep_time="23:00")
    send_offer(u.id)
    replies = driver.send(u, "sure, every 2 hours")
    assert "every ~2h" in replies[0]
    assert _reminders(u.id)[0].every_hours == 2


def test_no_is_remembered_and_never_reasked(db, offer_on, driver, anthropic_stub):
    from water_offer import send_offer, NO_REPLY, sweep
    u = make_user(db, user_timezone=_daytime_tz())
    send_offer(u.id)
    assert driver.send(u, "nah") == [NO_REPLY]
    assert anthropic_stub.calls == [] and _reminders(u.id) == []
    assert _u(u.id).water_offer_status == "no"
    assert sweep() == 0 and send_offer(u.id) is False


def test_other_reply_lapses_the_offer_and_reaches_the_model(db, offer_on, driver, anthropic_stub):
    from water_offer import send_offer
    anthropic_stub.reply_with(lambda kw: "sure, what did u have?")
    u = make_user(db)
    send_offer(u.id)
    replies = driver.send(u, "had a burrito for lunch")
    assert anthropic_stub.calls, "a non-answer must continue as a normal turn"
    assert _u(u.id).water_offer_status == "lapsed" and _reminders(u.id) == []
    # a later bare 'yes' is NOT an answer to the (lapsed) offer
    assert driver.send(u, "yes") != ["bet, every ~3h while ur up. say 'stop the water reminders' anytime"]


def test_offer_must_be_the_last_outbound_for_a_bare_yes(db, offer_on, driver, anthropic_stub):
    from water_offer import send_offer, handle_reply
    from models import get_session, Message
    anthropic_stub.reply_with(lambda kw: "ok")
    u = make_user(db)
    send_offer(u.id)
    s = get_session()
    try:
        s.add(Message(user_id=u.id, direction="out", body="u lifting tonight?", message_type="heartbeat",
                      created_at=_now() + timedelta(seconds=5)))
        s.commit()
    finally:
        s.close()
    assert handle_reply(u.id, "yes") is None      # 'yes' now answers the lift question
    assert _u(u.id).water_offer_status == "offered"


def test_stale_offer_is_not_answerable(db, offer_on):
    from water_offer import send_offer, handle_reply
    from models import get_session, User
    u = make_user(db)
    send_offer(u.id)
    s = get_session()
    try:
        s.get(User, u.id).water_offered_at = _now() - timedelta(hours=60)
        s.commit()
    finally:
        s.close()
    assert handle_reply(u.id, "yes") is None


def test_sweep_offers_existing_users_only_when_guardrails_allow(db, offer_on, sms_capture, monkeypatch):
    import config
    from water_offer import sweep, OFFER_TEXT
    from models import get_session, Message
    monkeypatch.setattr(config, "HEARTBEAT_ALLOWLIST", [])
    monkeypatch.setattr(config, "HEARTBEAT_STANDING_QUIET_ENABLED", True)
    day = make_user(db, user_timezone=_daytime_tz())                 # ~2pm local → allowed
    talking = make_user(db, user_timezone=_daytime_tz())
    s = get_session()
    try:
        s.add(Message(user_id=talking.id, direction="in", body="yo", message_type="freeform", created_at=_now()))
        s.commit()
    finally:
        s.close()
    onboarding = make_user(db, onboarding_step=2, user_timezone=_daytime_tz())
    n = sweep()
    assert n == 1, n
    assert [b for _p, b in sms_capture] == [OFFER_TEXT]
    assert _u(day.id).water_offer_status == "offered"
    assert _u(talking.id).water_offer_status is None      # active conversation: wait
    assert _u(onboarding.id).water_offer_status is None
    assert sweep() == 0                                   # once, ever


def test_kickoff_sends_the_offer_after_the_rundown(db, offer_on, sms_capture, anthropic_stub, monkeypatch):
    """The third bubble after onboarding completes (best-effort, after the rundown)."""
    import config
    import onboarding_agent as oa
    from water_offer import OFFER_TEXT
    monkeypatch.setattr(config, "ONBOARDING_RUNDOWN_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: "locked in. lets go")
    u = make_user(db, onboarding_step=2, calorie_target=None, protein_target=None)
    oa._complete_onboarding(_u(u.id), "yes")
    bodies = [b for _p, b in sms_capture]
    assert bodies and bodies[-1] == OFFER_TEXT, bodies
    assert _u(u.id).water_offer_status == "offered"


def test_migration_and_capability(db):
    from models import User
    assert hasattr(User, "water_offer_status") and hasattr(User, "water_offered_at")
    from capabilities import CAPABILITIES
    cap = {c.id: c for c in CAPABILITIES}["water_reminders"]
    assert cap.relevance(type("U", (), {"water_offer_status": None})()) == 8
    assert cap.relevance(type("U", (), {"water_offer_status": "no"})()) == 3


def test_offer_counts_as_an_unanswered_proactive_for_the_heartbeat(db, offer_on, sms_capture):
    from water_offer import send_offer
    from engagement_tracker import has_unanswered_proactive
    from models import get_session, Message
    u = make_user(db)
    send_offer(u.id)
    # the local test DB stores the aware default in the session's zone; pin it to UTC-now
    s = get_session()
    try:
        m = s.query(Message).filter(Message.user_id == u.id, Message.direction == "out").one()
        m.created_at = _now()
        s.commit()
    finally:
        s.close()
    assert has_unanswered_proactive(u.id, 120) is True


def test_a_fired_reminder_bubble_does_not_take_the_floor_from_the_offer(db, offer_on, sms_capture):
    """Live 2026-09-23 (Alex): offer 00:31, his tue/thu 'go run' reminder fired 02:30,
    'Nahh I drink a lot of water' at 02:31 → pending_offer saw the reminder as the
    last outbound, the 'no' went to the model, status stuck 'offered' for good."""
    from water_offer import send_offer, handle_reply, NO_REPLY
    from models import get_session, Message
    u = make_user(db)
    send_offer(u.id)
    s = get_session()
    try:
        s.add(Message(user_id=u.id, direction="out", body="go run", message_type="reminder",
                      created_at=_now() + timedelta(seconds=5)))
        s.commit()
    finally:
        s.close()
    assert handle_reply(u.id, "Nahh I drink a lot of water") == NO_REPLY
    assert _u(u.id).water_offer_status == "no" and _reminders(u.id) == []


def test_stretched_yes_and_no_spellings_count(db, offer_on):
    from water_offer import YES_RE, NO_RE
    for t in ("Nahh I drink a lot of water", "naw", "nooo", "nope!", "Nah im good"):
        assert NO_RE.match(t) and not YES_RE.match(t), t
    for t in ("yess", "Yeahh do it", "yeh", "yupp", "yaa"):
        assert YES_RE.match(t) and not NO_RE.match(t), t
    assert not NO_RE.match("not really sure what u mean") and not NO_RE.match("nothing today")
    assert not YES_RE.match("yesterday was rough") and not YES_RE.match("yale game sat")
