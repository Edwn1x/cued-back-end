"""Live 2026-10-17 16:48 PT: the coach 👎-tapbacked the founder's "I had to get here to find out
bruh". Meant as "that sucks"; on iMessage a thumbs-down on someone's complaint reads as
disagreeing with them. The react tool now refuses dislike and asks for a line of text."""
from __future__ import annotations

import pytest

from tests.factories import make_user
from tests.tier1.test_reactions_and_replies import _inbound, imessage_on, sidecar  # noqa: F401


@pytest.mark.parametrize("emoji", ["dislike", "👎", "👎🏽"])
def test_dislike_is_refused_with_a_text_instruction(db, imessage_on, sidecar, emoji):
    from agent_tools import handle_react_to_message, begin_turn
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    mid = _inbound(db, user, "I had to get here to find out bruh", "spc-msg-bruh")
    begin_turn(user.id)
    out = handle_react_to_message(user.id, {"message_ref": f"m{mid}", "emoji": emoji})
    assert out.startswith("error:") and "disagreeing" in out
    assert sidecar == []


def test_other_tapbacks_still_work(db, imessage_on, sidecar):
    from agent_tools import handle_react_to_message, begin_turn
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    mid = _inbound(db, user, "gym was brutal today", "spc-msg-ok")
    begin_turn(user.id)
    out = handle_react_to_message(user.id, {"message_ref": f"m{mid}", "emoji": "love"})
    assert out.startswith("ok:")
    assert [r for r, _ in sidecar] == ["/react"] or any("react" in r for r, _ in sidecar)


def test_flag_off_allows_dislike(db, imessage_on, sidecar, monkeypatch):
    import config
    from agent_tools import handle_react_to_message, begin_turn
    monkeypatch.setattr(config, "REACTION_DISLIKE_BLOCKED", False)
    user = make_user(db, preferred_channel="imessage", onboarding_step=3)
    mid = _inbound(db, user, "gym was brutal today", "spc-msg-ok2")
    begin_turn(user.id)
    assert handle_react_to_message(user.id, {"message_ref": f"m{mid}", "emoji": "dislike"}).startswith("ok:")


def test_voice_carries_the_rule():
    from agent_loop import _voice_prompt
    v = " ".join(_voice_prompt().split())
    assert "Never 👎 their message." in v and "no situation where 👎 on THEIR text is the move" in v
