"""
Tier-2 (live, metered) — Phase 2 voice eval + cost measurement. Runs the real
agent loop (voice.md + unified context, Sonnet 5) on scripted scenarios; asserts
the hard voice guardrails; prints replies for founder review and the measured
per-message cost at BOTH intro and standard rates. Run: pytest --run-tier2 -s.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.tier2

_HYPE = ["great job", "you got this", "let's go", "let's crush", "crushed it",
         "keep it up", "proud of you", "amazing job"]


def _has_emoji(s: str) -> bool:
    return any(ord(c) >= 0x1F000 or 0x2600 <= ord(c) <= 0x27BF for c in s)


def test_voice_and_cost_live(db):
    from tests.factories import make_user
    from memory import apply_facts
    from agent_loop import run_agent_loop
    from models import get_session, TokenUsage

    prof, _ = apply_facts(None, [
        {"action": "add", "category": "goals",
         "text": "cut to 12% body fat by june", "replaces_text": None, "safety_critical": False},
        {"action": "add", "category": "constraints",
         "text": "tweaked left shoulder, avoid overhead pressing", "replaces_text": None,
         "safety_critical": True},
        {"action": "add", "category": "schedule",
         "text": "organic chem exam every friday morning", "replaces_text": None,
         "safety_critical": False},
    ])
    user = make_user(
        db, current_split="ppl", split_pointer_day="push",
        confirmed_training_days="mon,tue,wed,thu,fri",
        user_profile_memory=prof, weight_lbs=175, height_ft=5, height_in=10,
        diet="omnivore", which_gym="rsf", experience="intermediate",
    )

    scenarios = [
        "what should i grab for lunch? at the dining hall rn",
        "thinking of doing some overhead press today, that cool?",   # must respect shoulder
        "ngl i've skipped the gym twice this week",                   # accountability moment
    ]
    replies = [run_agent_loop(user, m, "freeform") for m in scenarios]

    s = get_session()
    try:
        rows = s.query(TokenUsage).filter(
            TokenUsage.user_id == user.id, TokenUsage.site == "agent_loop.run").all()
    finally:
        s.close()
    inp = sum(r.input_tokens or 0 for r in rows)
    out = sum(r.output_tokens or 0 for r in rows)
    cr = sum(r.cache_read_input_tokens or 0 for r in rows)
    cw = sum(r.cache_creation_input_tokens or 0 for r in rows)
    std = sum(r.cost_usd or 0 for r in rows)
    intro = (inp * 2.0 + cw * 2.0 * 1.25 + cr * 2.0 * 0.10 + out * 10.0) / 1e6
    n = len(scenarios)

    print("\n\n=== TIER-2 VOICE + COST (Phase 2) ===")
    for m, r in zip(scenarios, replies):
        print(f"> {m}\n  {r}\n")
    print(f"tokens: in={inp} out={out} cache_read={cr} cache_write={cw}")
    print(f"cost total: standard ${std:.5f} | intro ${intro:.5f}   ({n} messages)")
    print(f"per-message: standard ${std / n:.5f} | intro ${intro / n:.5f}")
    print(f"prompt-cache reads: {cr} tokens ({'HIT' if cr > 0 else 'no hit — voice prefix below min cacheable?'})")

    # Hard voice guardrails (deterministic, from voice.md)
    for r in replies:
        assert r.strip(), "empty reply"
        assert not _has_emoji(r), f"voice.md forbids emojis, got: {r!r}"
    joined = " ".join(replies).lower()
    for b in _HYPE:
        assert b not in joined, f"hype phrase in voice: {b!r}"
