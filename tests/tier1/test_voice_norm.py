"""The lowercase friend voice, enforced in code (identity.md: 'Lowercase default').
Live 2026-10-05/06 (user 48): "What", "Nice. what's the hook", "Nice. go film it",
"Yeah, you're at 980 for the day, 35g protein." — all from the normal agent_loop path."""
from __future__ import annotations

import pytest

from voice_norm import lowercase_lead


@pytest.mark.parametrize("before, after", [
    ("What", "what"),
    ("Nice. what's the hook", "nice. what's the hook"),
    ("Nice. go film it", "nice. go film it"),
    ("Yeah, you're at 980 for the day, 35g protein.", "yeah, you're at 980 for the day, 35g protein."),
    ("I'm on it. I'll check", "i'm on it. i'll check"),
    ("ok. RSF is packed rn", "ok. RSF is packed rn"),                      # acronym stays
    ("HW4 is due fri", "HW4 is due fri"),                                  # digits stay
    ("tap this: https://app.cued.fit/c/gcal/AbC", "tap this: https://app.cued.fit/c/gcal/AbC"),
    ("Chipotle's a solid move", "chipotle's a solid move"),                # voice is lowercase everywhere
    ("ur at 2510. Protein's way over", "ur at 2510. protein's way over"),
    ("first line\nSecond line", "first line\nsecond line"),
    ("A quick one", "a quick one"),
    ("", ""),
])
def test_lowercase_lead(before, after):
    assert lowercase_lead(before) == after
    assert lowercase_lead(after) == after        # idempotent


def test_loop_lowers_the_reply_and_logs(db, anthropic_stub, caplog, monkeypatch):
    import config, logging
    from tests.factories import make_user
    import agent_loop
    monkeypatch.setattr(config, "LOWERCASE_REPLIES_ENABLED", True)
    anthropic_stub.reply_with(lambda kw: "Nice. go film it")
    u = make_user(db)
    with caplog.at_level(logging.INFO, logger="cued.agent_loop"):
        out = agent_loop.run_agent_loop(u, "making a reel", "freeform")
    assert out == "nice. go film it"
    assert any("AGENT_LOOP_LOWERCASED" in r.message for r in caplog.records)
    monkeypatch.setattr(config, "LOWERCASE_REPLIES_ENABLED", False)
    anthropic_stub.reply_with(lambda kw: "Nice. go film it")
    assert agent_loop.run_agent_loop(u, "making a reel", "freeform") == "Nice. go film it"


def test_voice_rules_cover_protein_over_target_and_the_fitbit_offer():
    from agent_loop import _VOICE_PATH
    raw = open(_VOICE_PATH, encoding="utf-8").read()
    assert "Protein OVER the target is never a problem" in raw and '"way over"' in raw
    from connect_offers import OFFER_HEALTH_LINK, OFFER_HEALTH_ASK
    assert "mentioned" not in OFFER_HEALTH_LINK and "mentioned" not in OFFER_HEALTH_ASK
    assert OFFER_HEALTH_LINK.format(device="fitbit").startswith("u put a fitbit on ur signup")
