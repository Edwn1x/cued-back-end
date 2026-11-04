"""
First text back after a long gap → the reply context carries a re-engagement cue
(founder 2026-11-04). Live u33: 26 days dark under water gifs, 👍 to the ping check-in
within a minute, coach answered with a bare tapback because nothing told the reply turn
it was her first text in 26 days. The cue is code-dated off the messages table and says:
react AND one bubble of concrete offers; the check-in variant adds "the pings stay".
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _add(user_id, direction, body, *, at, mtype="freeform"):
    from models import get_session, Message
    s = get_session()
    try:
        s.add(Message(user_id=user_id, direction=direction, body=body, message_type=mtype, created_at=at))
        s.commit()
    finally:
        s.close()


def _ctx(user_id):
    from models import get_session, User
    from agent_loop import build_loop_context
    s = get_session()
    try:
        return build_loop_context(s.get(User, user_id), s)
    finally:
        s.close()


def test_long_gap_emits_cue_with_day_count(db):
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "I ate it", at=now - timedelta(days=26, hours=2))
    _add(u.id, "out", "[water-break gif]", at=now - timedelta(days=3), mtype="water_reminder")
    _add(u.id, "in", "👍", at=now - timedelta(seconds=20))
    ctx = _ctx(u.id)
    assert "FIRST TEXT FROM THEM IN 26 DAYS" in ctx, ctx
    assert "react-only rule does NOT apply" in ctx
    assert "OFFER two or three concrete things" in ctx
    assert "log today's meals" in ctx
    # not the check-in variant — the last outbound was a plain water ping
    assert "they stay" not in ctx


def test_checkin_reply_names_the_pings(db):
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "ok", at=now - timedelta(days=20))
    _add(u.id, "out", "been a min. still want the water pings? 👍 and they stay, say stop and they're gone",
         at=now - timedelta(minutes=1), mtype="reminder_checkin")
    _add(u.id, "in", "👍", at=now - timedelta(seconds=10))
    ctx = _ctx(u.id)
    assert "FIRST TEXT FROM THEM IN 20 DAYS" in ctx, ctx
    assert "the pings STAY" in ctx and "cancel_reminder" in ctx


def test_short_gap_no_cue(db):
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "hey", at=now - timedelta(days=2))
    _add(u.id, "in", "ok", at=now - timedelta(seconds=10))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_first_ever_text_no_cue(db):
    from tests.factories import make_user
    u = make_user(db)
    _add(u.id, "in", "hi", at=_now() - timedelta(seconds=5))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_stale_latest_inbound_no_cue(db):
    """Only the live turn for that text gets the cue — not a context built hours later."""
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "hey", at=now - timedelta(days=30))
    _add(u.id, "in", "👍", at=now - timedelta(hours=5))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_flag_off_no_cue(db, monkeypatch):
    import config
    from tests.factories import make_user
    monkeypatch.setattr(config, "REENGAGE_REPLY_ENABLED", False)
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "hey", at=now - timedelta(days=30))
    _add(u.id, "in", "👍", at=now - timedelta(seconds=5))
    assert "FIRST TEXT FROM THEM" not in _ctx(u.id)


def test_study_room_offer_follows_campus_flag(db, monkeypatch):
    import config
    from tests.factories import make_user
    u = make_user(db)
    now = _now()
    _add(u.id, "in", "hey", at=now - timedelta(days=10))
    _add(u.id, "in", "👍", at=now - timedelta(seconds=5))
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", True)
    assert "send_study_room_link" in _ctx(u.id)
    monkeypatch.setattr(config, "CAMPUS_LIBRARIES_ENABLED", False)
    assert "send_study_room_link" not in _ctx(u.id)


def test_voice_prompt_carries_the_exception():
    from pathlib import Path
    voice = Path("prompts/voice.md").read_text()
    assert "FIRST TEXT FROM THEM IN N DAYS" in voice
