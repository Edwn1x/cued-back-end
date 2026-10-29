"""
Live 2026-10-28 23:35, user 77, mid home pull session: the coach told him to use the swap button,
he answered "I dont see those options", and the coach guessed "might be an older card". The card
has them (founder's screenshot): small grey "swap" and "remove" on the right of each exercise's
name, above its sets. voice.md said the buttons exist but never WHERE, so the coach couldn't point.
"""
import re


def _voice():
    from agent_loop import _voice_prompt
    return re.sub(r"\s+", " ", _voice_prompt())


def test_voice_says_where_swap_and_remove_are():
    v = _voice()
    assert 'small grey "swap" and "remove" on the right of each exercise\'s name, above its sets' in v


def test_voice_bans_the_older_card_guess_and_keeps_a_text_fallback():
    v = _voice()
    assert "never guess it's an older card" in v
    assert "text u the swap" in v or "text me the swap" in v
