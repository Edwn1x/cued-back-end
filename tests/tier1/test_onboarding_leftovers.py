"""Two leftovers from the user-48 from-zero grades (2026-10-05) not covered by the onboarding
restructure (#203/#207/#208/#209):
  • "Ok?" was swallowed as a closing ack (👍, no reply) while he was asking what to do next;
  • "So what now" ×3 → "nothing til ur at the gym" — a NEW HERE orientation block for the
    first 48h after setup."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from tests.factories import make_user


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_a_question_mark_ack_is_a_prompt_not_a_closer():
    from app import is_closing_acknowledgment
    assert is_closing_acknowledgment("ok") is True
    assert is_closing_acknowledgment("Ok") is True
    assert is_closing_acknowledgment("Ok?") is False
    assert is_closing_acknowledgment("cool?") is False
    assert is_closing_acknowledgment("got it") is True


def test_new_here_block_for_the_first_hours_only(db):
    from agent_loop import _new_here_block
    import config
    u = make_user(db, onboarding_step=3)
    u.created_at = _utcnow() - timedelta(hours=2)
    blk = _new_here_block(u)
    assert blk and blk.startswith("## NEW HERE (finished setup ~2h ago)") and "what now" in blk
    assert "Never answer \"nothing til ur at the gym\"" in blk
    u.created_at = _utcnow() - timedelta(hours=config.NEW_USER_ORIENTATION_HOURS + 1)
    assert _new_here_block(u) is None
    u.created_at = _utcnow() - timedelta(minutes=10)
    u.onboarding_step = 2
    assert _new_here_block(u) is None


def test_new_here_block_is_in_the_loop_context(db):
    from agent_loop import build_loop_context
    from models import get_session, User
    u = make_user(db, onboarding_step=3)
    s = get_session()
    try:
        row = s.get(User, u.id)
        row.created_at = _utcnow() - timedelta(hours=1)
        s.commit()
        assert "## NEW HERE" in build_loop_context(row, s)
        row.created_at = _utcnow() - timedelta(days=5)
        s.commit()
        assert "## NEW HERE" not in build_loop_context(row, s)
    finally:
        s.close()
