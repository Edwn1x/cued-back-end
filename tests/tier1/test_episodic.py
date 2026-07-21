"""
Phase 5 — episodic digest. The sweep fires only when a conversation has gone quiet,
the watermark makes it idempotent (same messages never digested twice), a "NONE"
verdict still advances the watermark but writes no note, and recent notes surface
into the shared context builder (the heartbeat's follow-up material).
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_convo(db, user_id, n=4, minutes_ago=200):
    """n messages; the LAST one `minutes_ago` old (controls the quiet gate)."""
    from models import get_session, Message
    s = get_session()
    try:
        base = _utcnow_naive() - timedelta(minutes=minutes_ago)
        for i in range(n):
            s.add(Message(user_id=user_id, direction="in" if i % 2 == 0 else "out",
                         body=f"line {i}", created_at=base + timedelta(seconds=i)))
        s.commit()
    finally:
        s.close()


def _digests(user_id):
    from models import get_session, EpisodicDigest
    s = get_session()
    try:
        return s.query(EpisodicDigest).filter(EpisodicDigest.user_id == user_id).all()
    finally:
        s.close()


def _watermark(user_id):
    from models import get_session, User
    s = get_session()
    try:
        return s.get(User, user_id).last_episodic_message_id
    finally:
        s.close()


def test_quiet_conversation_is_digested_and_watermark_advances(db, anthropic_stub):
    from tests.factories import make_user
    import episodic

    anthropic_stub.reply_with(lambda kw: "Big orgo midterm tomorrow — pretty stressed.")
    user = make_user(db, name="Priya")
    _seed_convo(db, user.id, n=4, minutes_ago=200)  # quiet (>90 min)

    res = episodic.digest_user(user.id)
    assert res["status"] == "wrote", res
    notes = _digests(user.id)
    assert len(notes) == 1 and "orgo midterm" in notes[0].text
    assert _watermark(user.id) == res["watermark"]

    # idempotent: no new messages beyond the watermark -> nothing to do
    again = episodic.digest_user(user.id)
    assert again["status"] == "too_few"
    assert len(_digests(user.id)) == 1


def test_active_conversation_is_not_digested(db, anthropic_stub):
    from tests.factories import make_user
    import episodic

    called = []
    anthropic_stub.reply_with(lambda kw: called.append(1) or "note")
    user = make_user(db, name="Sam")
    _seed_convo(db, user.id, n=4, minutes_ago=5)  # still active (<90 min)

    res = episodic.digest_user(user.id)
    assert res["status"] == "still_active", res
    assert _digests(user.id) == []
    assert _watermark(user.id) in (None, 0), "watermark must not advance on a live convo"
    assert called == [], "must not call the model for an active conversation"


def test_nothing_notable_advances_watermark_without_writing(db, anthropic_stub):
    from tests.factories import make_user
    import episodic

    anthropic_stub.reply_with(lambda kw: "NONE")
    user = make_user(db, name="Sam")
    _seed_convo(db, user.id, n=5, minutes_ago=200)

    res = episodic.digest_user(user.id)
    assert res["status"] == "nothing_notable", res
    assert _digests(user.id) == []
    assert _watermark(user.id) == res["watermark"], "watermark advances so we don't re-scan"


def test_too_few_messages_skips(db, anthropic_stub):
    from tests.factories import make_user
    import episodic

    user = make_user(db, name="Sam")
    _seed_convo(db, user.id, n=2, minutes_ago=200)  # below EPISODIC_MIN_MESSAGES

    res = episodic.digest_user(user.id)
    assert res["status"] == "too_few", res
    assert _digests(user.id) == []


def test_recent_episodic_surfaces_in_context(db, monkeypatch):
    import config
    from tests.factories import make_user
    from models import get_session, EpisodicDigest
    from agent_loop import build_loop_context

    monkeypatch.setattr(config, "EPISODIC_ENABLED", True)
    user = make_user(db, name="Priya")
    s = get_session()
    try:
        s.add(EpisodicDigest(user_id=user.id, text="Moving apartments this weekend — stressed."))
        s.commit()
    finally:
        s.close()

    s = get_session()
    try:
        u = s.get(type(user), user.id)
        ctx = build_loop_context(u, s)
    finally:
        s.close()
    assert "RECENT LIFE CONTEXT" in ctx and "Moving apartments" in ctx
