"""
Tier-2 (live) anchors for the re-engagement reply (2026-11-04, user 33 "Krla"). Binary,
parametrized x3 per the founder rule.

  1. Her exact turn — 26 days dark under water gifs, the ping check-in, then "👍" — gets a
     TEXT back (not a bare tapback): the pings stay, a meal-logging offer, a study-room
     offer, no training-gap talk, and the water reminder is still armed.
  2. Control: an engaged user's "ok cool" is still a reaction-only turn — the carve-out
     must not turn every acknowledgment into a paragraph.
Run: pytest tests/tier2/test_reengagement_reply_live.py --run-tier2 -s
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta

import pytest

from tests.factories import make_user
from tests.tier1.test_workout_phases_3_4_5 import imessage_on, sidecar_ok  # noqa: F401 — fixtures

pytestmark = pytest.mark.tier2

KRLA = dict(name="Krla", onboarding_step=3, preferred_channel="imessage", equipment="full_gym",
            workout_time="19:00-20:00", wake_time="08:30", sleep_time="23:30", height_ft=5, height_in=4,
            weight_lbs=150, age=20, gender="female", goal="fat_loss", experience="beginner",
            calorie_target=1550, protein_target=128, targets_source="computed")
CHECKIN = "been a min. still want the water pings? 👍 and they stay, say stop and they're gone"


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _flags(monkeypatch):
    """Prod-like: flags on, and tapbacks LAND (the sidecar is a test host here — a failed
    react makes the model fall back to a text, which is not what prod does)."""
    import config, sms
    for f in ("SINGLE_AGENT_LOOP_ENABLED", "REMINDERS_ENABLED", "CAMPUS_LIBRARIES_ENABLED", "REENGAGE_REPLY_ENABLED"):
        monkeypatch.setattr(config, f, True)
    monkeypatch.setattr(sms, "react_to_message", lambda user_id, provider_sid, emoji: True)


def _old_account():
    # past NEW_USER_ORIENTATION_HOURS, so the NEW HERE capability list isn't in the context
    return _now() - timedelta(days=50)


def _msg(db, uid, direction, body, *, at, mtype="freeform", sid=None):
    from models import Message
    db.add(Message(user_id=uid, direction=direction, body=body, message_type=mtype, created_at=at,
                   channel="imessage", provider_sid=sid))
    db.commit()


def _run(user_id, text):
    from agent_loop import run_agent_loop
    from models import get_session, User
    s = get_session()
    try:
        return run_agent_loop(s.get(User, user_id), text, "freeform") or ""
    finally:
        s.close()


@pytest.mark.parametrize("run", [1, 2, 3])
def test_thumbs_up_after_26_days_gets_the_offers_bubble(db, monkeypatch, imessage_on, sidecar_ok, run):
    from agent_tools import is_reaction_only_text
    from models import Reminder
    _flags(monkeypatch)
    user = make_user(db, **KRLA, created_at=_old_account())
    now = _now()
    db.add(Reminder(user_id=user.id, text="drink water", local_time="11:30", every_hours=3,
                    window_start="11:30", window_end="20:30", source="offer", active=True,
                    fire_at=now + timedelta(hours=3), last_sent_at=now - timedelta(minutes=1), sent_count=179))
    db.commit()
    _msg(db, user.id, "in", "[image attached]", at=now - timedelta(days=26, minutes=6))
    _msg(db, user.id, "out", "logged the skinnypop, ~100 cal. did u eat it or saving it", at=now - timedelta(days=26, minutes=5))
    _msg(db, user.id, "in", "I ate it", at=now - timedelta(days=26))
    for d in (3, 2, 1):
        for h in (0, 3, 6):
            _msg(db, user.id, "out", "[water-break gif]", at=now - timedelta(days=d, hours=h), mtype="water_reminder")
    _msg(db, user.id, "out", CHECKIN, at=now - timedelta(minutes=1), mtype="reminder_checkin")
    _msg(db, user.id, "in", "👍", at=now - timedelta(seconds=5), sid="spc-msg-krla-1")

    reply = _run(user.id, "👍")
    print(f"\n[run {run}] reply: {reply!r}")
    assert reply.strip() and not is_reaction_only_text(reply, True), f"bare tapback / silence: {reply!r}"
    low = reply.lower()
    assert "stay" in low, f"never said the pings stay: {reply!r}"
    assert re.search(r"\b(log|pic|meal|meals|eat|ate|eaten|food)\b", low), f"no meal-logging offer: {reply!r}"
    assert re.search(r"\b(study|room|moffitt|library)\b", low), f"no study-room offer: {reply!r}"
    assert not re.search(r"\b(gym|workout|workouts|train|trained|training|lift|lifting|session)\b", low), \
        f"brought up training on the way back in: {reply!r}"
    assert len(reply) <= 420, f"that's a paragraph, not a bubble ({len(reply)} chars): {reply!r}"
    db.expire_all()
    assert db.query(Reminder).filter(Reminder.user_id == user.id, Reminder.active.is_(True)).count() == 1, \
        "a 👍 cancelled the water pings"


@pytest.mark.parametrize("run", [1, 2, 3])
def test_engaged_users_ok_cool_is_still_reaction_only(db, monkeypatch, imessage_on, sidecar_ok, run):
    from agent_tools import is_reaction_only_text
    _flags(monkeypatch)
    user = make_user(db, **dict(KRLA, name="Maya"), created_at=_old_account())
    now = _now()
    _msg(db, user.id, "in", "had chicken and rice for lunch", at=now - timedelta(minutes=3))
    _msg(db, user.id, "out", "logged, ~520 cal 42g protein. ur at 78g, dinner needs to carry the rest",
         at=now - timedelta(minutes=2))
    _msg(db, user.id, "in", "ok cool", at=now - timedelta(seconds=5), sid="spc-msg-maya-1")
    reply = _run(user.id, "ok cool")
    print(f"\n[run {run}] control reply: {reply!r}")
    assert (not reply.strip()) or is_reaction_only_text(reply, True), f"paragraph back to 'ok cool': {reply!r}"
