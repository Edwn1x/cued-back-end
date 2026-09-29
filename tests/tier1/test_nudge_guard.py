"""Anti-nagging / nudge-repetition guard (nudge_guard.py + build_loop_context +
heartbeat wiring).

Live 2026-09-28 (founder, user 31): the coach re-issued the SAME standing nudge
("eat some protein") ~5× in one day across separate turns AND the morning brief,
with no awareness it had already said it. The guard scans recent OUTBOUND messages,
classifies them into coarse nudge topics, and surfaces an ALREADY NUDGED TODAY line
so the coach varies the angle or lets it rest. Advisory, flag-gated, fail-open.

These are deterministic (no model call) — we prove the machinery: recent nudges of
a topic surface the signal, a fresh topic is not suppressed, and it's inert with the
flag off / no recent nudges. The reactive path (build_loop_context) and the proactive
path (heartbeat._proactive_context, which begins with build_loop_context) both carry it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _seed_out(user_id, body, *, minutes_ago=0, message_type="freeform"):
    """Seed one OUTBOUND Message (naive-UTC, prod's convention). minutes_ago=0 is
    'right now' so the row is always inside today's window regardless of wall clock."""
    from models import get_session, Message
    when = _utcnow_naive() - timedelta(minutes=minutes_ago)
    s = get_session()
    try:
        s.add(Message(user_id=user_id, direction="out", body=body,
                      message_type=message_type, created_at=when))
        s.commit()
    finally:
        s.close()


# ---- unit: classifier is precision-biased + word-boundary anchored ----------

def test_classify_matches_protein():
    from nudge_guard import classify_nudge_topics
    assert "protein" in classify_nudge_topics("you gotta get some protein in today")


def test_classify_multi_topic():
    from nudge_guard import classify_nudge_topics
    topics = classify_nudge_topics("get some sleep, and eat something before bed")
    assert "sleep" in topics
    assert "eat" in topics


def test_classify_word_boundary_no_false_positive():
    from nudge_guard import classify_nudge_topics
    # "work" inside "network"/"framework" must NOT read as workout; a plain compliment
    # must not classify (the legacy-substring bug: "egg whites" ⊃ "hit").
    assert classify_nudge_topics("nice work on that framework, homework's done") == set()
    assert classify_nudge_topics("those egg whites look great") == set()


def test_classify_empty():
    from nudge_guard import classify_nudge_topics
    assert classify_nudge_topics("") == set()
    assert classify_nudge_topics(None) == set()


# ---- integration: build_loop_context surfaces the ALREADY NUDGED signal ------

def test_repeated_protein_surfaces_signal(db, monkeypatch):
    import config
    from agent_loop import build_loop_context
    from tests.factories import make_user

    monkeypatch.setattr(config, "NUDGE_REPETITION_GUARD_ENABLED", True)
    user = make_user(db)
    _seed_out(user.id, "eat some protein, you've got beef and eggs")
    _seed_out(user.id, "still low on protein for the day")
    _seed_out(user.id, "get that protein in before bed")

    db.expire_all()
    ctx = build_loop_context(user, db)
    assert "ALREADY NUDGED TODAY" in ctx
    assert "protein (3x)" in ctx


def test_fresh_topic_not_suppressed(db, monkeypatch):
    """A topic that was NOT recently nudged must not appear in the block — the guard
    only names what was actually raised, so genuinely new topics stay open."""
    import config
    from agent_loop import build_loop_context
    from tests.factories import make_user

    monkeypatch.setattr(config, "NUDGE_REPETITION_GUARD_ENABLED", True)
    user = make_user(db)
    _seed_out(user.id, "get some protein in")
    _seed_out(user.id, "more protein today")

    db.expire_all()
    ctx = build_loop_context(user, db)
    assert "protein" in ctx.split("ALREADY NUDGED TODAY")[1]
    # water was never nudged → must not be listed as already-nudged
    block = ctx.split("ALREADY NUDGED TODAY")[1].split("## NOW")[0]
    assert "water" not in block


def test_inert_when_flag_off(db, monkeypatch):
    import config
    from agent_loop import build_loop_context
    from tests.factories import make_user

    monkeypatch.setattr(config, "NUDGE_REPETITION_GUARD_ENABLED", False)
    user = make_user(db)
    _seed_out(user.id, "get some protein in")
    _seed_out(user.id, "more protein today")

    db.expire_all()
    ctx = build_loop_context(user, db)
    assert "ALREADY NUDGED TODAY" not in ctx


def test_inert_when_no_recent_nudges(db, monkeypatch):
    import config
    from agent_loop import build_loop_context
    from tests.factories import make_user

    monkeypatch.setattr(config, "NUDGE_REPETITION_GUARD_ENABLED", True)
    user = make_user(db)
    # a non-nudge outbound shouldn't trigger the block
    _seed_out(user.id, "haha yeah that game was wild")

    db.expire_all()
    ctx = build_loop_context(user, db)
    assert "ALREADY NUDGED TODAY" not in ctx


def test_reactions_excluded(db, monkeypatch):
    """A tapback is closure, not a nudge — a reaction row must never count even if its
    body happens to contain a topic keyword."""
    import config
    from nudge_guard import recent_nudge_topics
    from tests.factories import make_user

    monkeypatch.setattr(config, "NUDGE_REPETITION_GUARD_ENABLED", True)
    user = make_user(db)
    _seed_out(user.id, "protein", message_type="reaction")

    db.expire_all()
    assert recent_nudge_topics(user, db) == {}


def test_lookback_window_excludes_yesterday(db, monkeypatch):
    """A nudge from ~25h ago (before today's local midnight) is outside the window."""
    import config
    from nudge_guard import recent_nudge_topics
    from tests.factories import make_user

    monkeypatch.setattr(config, "NUDGE_REPETITION_GUARD_ENABLED", True)
    user = make_user(db)
    _seed_out(user.id, "get some protein in", minutes_ago=25 * 60)

    db.expire_all()
    assert recent_nudge_topics(user, db) == {}


# ---- heartbeat carries the same signal (proactive path) ----------------------

def test_heartbeat_context_carries_signal(db, monkeypatch):
    """_proactive_context begins with build_loop_context, so the heartbeat decision
    sees the same ALREADY NUDGED block and won't re-pick a just-nudged topic."""
    import config
    import heartbeat
    from tests.factories import make_user

    monkeypatch.setattr(config, "NUDGE_REPETITION_GUARD_ENABLED", True)
    user = make_user(db)
    _seed_out(user.id, "eat some protein")
    _seed_out(user.id, "still short on protein")

    db.expire_all()
    ctx = heartbeat._proactive_context(user, db)
    assert "ALREADY NUDGED TODAY" in ctx
    assert "protein (2x)" in ctx
