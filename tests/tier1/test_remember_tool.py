"""
Phase 3 tool 1 — remember. Code-mediated memory writes wrapping the Phase-1
primitives. The model requests; code validates the category and writes.
"""

from __future__ import annotations


def _goals(user_id):
    from models import get_session, User
    s = get_session()
    try:
        u = s.query(User).get(user_id)
        return (u.user_profile_memory or {}).get("goals", [])
    finally:
        s.close()


def test_handle_remember_add_persists(db):
    from tests.factories import make_user
    from agent_tools import handle_remember

    user = make_user(db)
    out = handle_remember(user.id, {
        "action": "add", "category": "goals", "text": "wants to run a half marathon",
    })
    assert out.startswith("ok"), out
    assert any("half marathon" in e["text"] for e in _goals(user.id))


def test_handle_remember_rejects_unknown_category(db):
    from tests.factories import make_user
    from agent_tools import handle_remember

    user = make_user(db)
    out = handle_remember(user.id, {"action": "add", "category": "bogus", "text": "x"})
    assert "error" in out and "category" in out


def test_handle_remember_invalidate_moves_to_history(db):
    from tests.factories import make_user
    from agent_tools import handle_remember
    from memory import HISTORY_KEY
    from models import get_session, User

    user = make_user(db)
    handle_remember(user.id, {"action": "add", "category": "goals", "text": "wants to cut to 12%"})
    entry_id = _goals(user.id)[0]["id"]

    out = handle_remember(user.id, {"action": "invalidate", "entry_id": entry_id})
    assert out.startswith("ok"), out

    s = get_session()
    try:
        prof = s.query(User).get(user.id).user_profile_memory or {}
    finally:
        s.close()
    assert not prof.get("goals")
    assert len(prof.get(HISTORY_KEY, [])) == 1


def test_remember_tool_via_loop_writes_memory(db, driver, monkeypatch, anthropic_stub):
    """End-to-end: the model requests remember → code writes → a reply is sent.
    The fake routes by the loop's `tools` kwarg so it doesn't collide with the
    webhook classifier / extraction calls (which carry no tools)."""
    import config
    from tests._fake_anthropic import ToolUse
    from tests.factories import make_user

    monkeypatch.setattr(config, "SINGLE_AGENT_LOOP_ENABLED", True)
    monkeypatch.setattr(config, "REMEMBER_TOOL_ENABLED", True)

    loop_calls = []

    def handler(kw):
        if not kw.get("tools"):
            return "freeform"  # classifier / extraction default (no tools)
        loop_calls.append(1)
        if len(loop_calls) == 1:
            return ToolUse("remember", {
                "action": "add", "category": "goals",
                "text": "training for a half marathon in the spring",
            })
        return "got it, noted."

    anthropic_stub.reply_with(handler)

    user = make_user(db)
    replies = driver.send(user, "btw i'm training for a half marathon")

    assert any("half marathon" in e["text"] for e in _goals(user.id)), \
        "remember tool call did not persist the fact"
    assert any("got it" in r.lower() for r in replies)
