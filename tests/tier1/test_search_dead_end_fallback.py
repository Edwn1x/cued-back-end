"""Live 2026-10-16 14:11 PT (user 61): "How many calories in GARA white latte" → two web
searches, then "couldn't pin GARA's exact number, their menu doesn't post macros" and nothing
else. No estimate, no question. The ladder in voice.md now says what to do when the tool comes
up empty; the code backstop appends the one next step when a reply still dead-ends."""
from __future__ import annotations

import logging

import pytest

from tests.factories import make_user

LIVE = "couldn't pin GARA's exact number, their menu doesn't post macros"


def test_voice_prompt_carries_the_empty_tool_rule():
    from agent_loop import _voice_prompt
    v = _voice_prompt()
    assert "When the tool comes up empty" in v
    assert "A reply with no number AND no next step is a failure" in v


def test_live_dead_end_gets_the_next_step_appended(db, caplog):
    import agent_loop
    user = make_user(db)
    with caplog.at_level(logging.INFO):
        out = agent_loop._dead_end_fallback(LIVE, user.id)
    assert out == LIVE + " — give me the size or send a pic and i'll log a solid estimate"
    assert "AGENT_LOOP_DEAD_END_BACKSTOP" in caplog.text


@pytest.mark.parametrize("text", [
    "couldn't find GARA's numbers but a 12oz white latte w/ whole milk is usually ~200 cal, 7g protein — logged as an estimate",
    "couldn't pin GARA's exact number — what size was it?",
    "logged it, 210 cal",
    "their menu doesn't post macros, so i went with ~200 cal for a 12oz",
])
def test_a_number_or_a_question_means_the_ladder_was_followed(db, text):
    import agent_loop
    user = make_user(db)
    assert agent_loop._dead_end_fallback(text, user.id) == text


def test_other_phrasings_are_caught(db):
    import agent_loop
    user = make_user(db)
    for t in ["wasn't able to track down the label for that", "they don't list calories anywhere"]:
        assert agent_loop._dead_end_fallback(t, user.id).endswith("i'll log a solid estimate")


def test_flag_off_is_a_noop(db, monkeypatch):
    import config, agent_loop
    monkeypatch.setattr(config, "SEARCH_DEAD_END_BACKSTOP_ENABLED", False)
    user = make_user(db)
    assert agent_loop._dead_end_fallback(LIVE, user.id) == LIVE
