"""A correction / callout never gets silence (live 2026-10-09 20:31 PT, user 48: "You should
know about the project I have due today / I'm still working on it / It's the third time i
tell you atp" → the coach saved a lesson and replied [silent] — nothing sent)."""
from __future__ import annotations

import pytest

from tests.factories import make_user

BURST = "I'm not\nAnd I can't\nYou should know about the project I have due today\nI'm still working on it\nIt's the third time i tell you atp"


@pytest.mark.parametrize("text, is_corr", [
    (BURST, True),
    ("I literally told you earlier i have to finish my cs project", True),
    ("Left on read?", True),
    ("What are you talking about?", True),
    ("Then why are you telling me to go to sleep?", True),
    ("u forgot the pull ups", True),
    ("ate a burger", False),
    ("Alr", False),
    ("ok yeah this one works", False),
])
def test_correction_regex(text, is_corr):
    from agent_loop import _is_callout
    assert _is_callout(text) is is_corr


def test_silent_reply_to_a_correction_is_nudged_into_one_owning_line(db, anthropic_stub, caplog):
    import agent_loop, logging
    u = make_user(db)
    seen = []

    def _h(kw):
        seen.append(kw["messages"][-1]["content"])
        return "[silent]" if len(seen) == 1 else "my bad, project 2b due today, it's on my radar now"
    anthropic_stub.reply_with(_h)
    with caplog.at_level(logging.WARNING, logger="cued.agent_loop"):
        out = agent_loop.run_agent_loop(u, BURST, "freeform")
    assert out == "my bad, project 2b due today, it's on my radar now"
    assert len(seen) == 2 and "answered with silence" in str(seen[1]) and "owns it plainly" in str(seen[1])
    assert "AGENT_LOOP_CORRECTION_SILENCE_NUDGE" in caplog.text


def test_silent_twice_on_a_correction_still_ends_silent_not_a_loop(db, anthropic_stub):
    import agent_loop
    u = make_user(db)
    calls = []
    anthropic_stub.reply_with(lambda kw: calls.append(1) or "[silent]")
    assert agent_loop.run_agent_loop(u, "I literally told you earlier", "freeform") == ""
    assert len(calls) == 2                                    # one nudge, then the swallow


def test_silent_reply_to_a_normal_message_is_still_silent(db, anthropic_stub, monkeypatch):
    import agent_loop, config
    u = make_user(db)
    calls = []
    anthropic_stub.reply_with(lambda kw: calls.append(1) or "[silent]")
    assert agent_loop.run_agent_loop(u, "Alr", "freeform") == "" and len(calls) == 1
    monkeypatch.setattr(config, "CORRECTION_SILENCE_NUDGE_ENABLED", False)
    calls.clear()
    assert agent_loop.run_agent_loop(u, BURST, "freeform") == "" and len(calls) == 1


def test_voice_rule_present():
    from agent_loop import _VOICE_PATH
    raw = open(_VOICE_PATH, encoding="utf-8").read()
    assert "Never answer a correction with silence or a bare tapback" in raw
