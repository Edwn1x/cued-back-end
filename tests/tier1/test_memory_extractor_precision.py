"""
Memory extractor precision (founder, 2026-09-11 — "sometimes it's good and sometimes
it trips out"). Live on Haiku for user 27:
  - constraints=["messed up"] — a fragment clipped from "my gym schedule is all messed
    up", stored in the ONE category that renders into every prompt as a rule.
  - "can train Thursday, Sep 12, 2026 ..." — Sep 12, 2026 is a Saturday; "yesterday"
    was Thursday the 10th. Wrong arithmetic, and a habit pinned to a date.
  - "Lol im cs" → nothing stored.
Fix: Sonnet 5 (config.MEMORY_EXTRACTOR_MODEL) + explicit prompt rules + a deterministic
sanitizer (memory.sanitize_facts) under whichever model runs.
"""

from __future__ import annotations

import logging

import pytest

from tests.factories import make_user


def _profile(db, user_id):
    from models import User
    db.expire_all()
    return dict(db.get(User, user_id).user_profile_memory or {})


def _texts(profile, cat):
    return [e["text"] for e in profile.get(cat, [])]


# ── sanitizer (pure) ─────────────────────────────────────────────────────────

def test_sanitizer_rejects_fragments_and_impossible_dates_keeps_the_rest(caplog):
    from memory import sanitize_facts
    facts = [
        {"action": "add", "category": "constraints", "text": "messed up"},
        {"action": "add", "category": "training_preferences",
         "text": "can train Thursday, Sep 12, 2026 in late evening (9-11pm window)"},
        {"action": "add", "category": "schedule", "text": "Has a quiz at 4pm on Friday, Sep 11, 2026"},
        {"action": "add", "category": "identity", "text": "CS major at UC Berkeley"},
        {"action": "add", "category": "training_preferences",
         "text": "can train late evening on stacked class days like Thursday"},
        {"action": "skip", "category": "goals", "text": ""},
    ]
    with caplog.at_level(logging.WARNING):
        kept, rejected = sanitize_facts(facts, user_id=27)
    assert rejected == 2
    assert [f["text"] for f in kept if f["action"] != "skip"] == [
        "Has a quiz at 4pm on Friday, Sep 11, 2026",
        "CS major at UC Berkeley",
        "can train late evening on stacked class days like Thursday",
    ]
    reasons = [r.getMessage() for r in caplog.records if "MEMORY_FACT_REJECTED" in r.getMessage()]
    assert any("reason=fragment" in m and "messed up" in m for m in reasons)
    assert any("reason=date_mismatch" in m and "Sep 12, 2026" in m for m in reasons)


def test_sanitizer_accepts_consistent_weekday_dates_in_any_month_spelling():
    from memory import sanitize_facts
    ok = [{"action": "add", "category": "schedule", "text": "midterm on Tuesday, September 15, 2026 at 7pm"},
          {"action": "add", "category": "schedule", "text": "flies home Wednesday, Dec 23, 2026"}]
    kept, rejected = sanitize_facts(ok)
    assert rejected == 0 and len(kept) == 2
    bad = [{"action": "add", "category": "schedule", "text": "midterm on Monday, September 15, 2026"}]
    assert sanitize_facts(bad)[1] == 1


# ── extractor wiring ─────────────────────────────────────────────────────────

def test_extractor_uses_sonnet_and_the_prompt_carries_the_three_rules(db, anthropic_stub):
    import config
    from app import extract_memory_facts
    user = make_user(db, name="Nau")
    seen = {}
    anthropic_stub.reply_with(lambda kw: seen.update(model=kw["model"], prompt=kw["messages"][0]["content"]) or '{"facts": []}')
    assert extract_memory_facts(user.id, "see why my gym schedule is all messed up", "yeah thursday is brutal") == []
    assert seen["model"] == config.MEMORY_EXTRACTOR_MODEL == "claude-sonnet-5"
    p = seen["prompt"]
    assert "A FACT IS A COMPLETE STATEMENT" in p
    assert "AN ANECDOTE IS NOT A PATTERN" in p
    assert "DIRECT IDENTITY STATEMENTS ALWAYS COUNT" in p
    assert "DATES, carefully" in p and "write BOTH the weekday and the date" in p


def test_extractor_survives_a_thinking_block_and_prose_around_the_json(db, anthropic_stub):
    from app import extract_memory_facts
    from tests._fake_anthropic import MultiText
    user = make_user(db, name="Nau")
    anthropic_stub.push(MultiText(("thinking", ""),
                                  'Sure — here are the facts:\n{"facts": [{"action": "add", "category": "identity", '
                                  '"text": "CS major at UC Berkeley", "replaces_text": null, "safety_critical": false}]} '))
    out = extract_memory_facts(user.id, "lol im cs", "am i close?")
    assert out and out[0]["text"] == "CS major at UC Berkeley"


def test_store_drops_rejected_facts_and_keeps_the_rest(db, anthropic_stub, caplog):
    """End to end: the model emits the live Haiku output verbatim; only the two
    trustworthy facts reach the profile."""
    from app import extract_and_store_memory
    user = make_user(db, name="Nau")
    payload = ('{"facts": ['
               '{"action": "add", "category": "constraints", "text": "messed up", "replaces_text": null, "safety_critical": false},'
               '{"action": "add", "category": "training_preferences", "text": "can train Thursday, Sep 12, 2026 in late evening", "replaces_text": null, "safety_critical": false},'
               '{"action": "add", "category": "identity", "text": "CS major at UC Berkeley", "replaces_text": null, "safety_critical": false},'
               '{"action": "add", "category": "training_preferences", "text": "responds well to accountability partners", "replaces_text": null, "safety_critical": false}'
               ']}')
    anthropic_stub.reply_with(lambda kw: payload)
    with caplog.at_level(logging.INFO):
        extract_and_store_memory(user.id, "see why my gym schedule is all messed up", "thursday is brutal")
    prof = _profile(db, user.id)
    assert _texts(prof, "constraints") == []
    assert _texts(prof, "identity") == ["CS major at UC Berkeley"]
    assert _texts(prof, "training_preferences") == ["responds well to accountability partners"]
    assert any("MEMORY_SANITIZE" in r.getMessage() and "rejected=2" in r.getMessage() for r in caplog.records)


def test_sanitizer_keeps_short_facts_that_state_something_and_never_drops_safety():
    """Founder: "what if the three words is something like 'likes to run'?" Kept — and
    so are two-word stating facts and ANY safety fact, however terse."""
    from memory import sanitize_facts
    keep = [
        {"action": "add", "category": "training_preferences", "text": "likes to run"},
        {"action": "add", "category": "training_preferences", "text": "hates cardio"},
        {"action": "add", "category": "identity", "text": "uses Strava"},
        {"action": "add", "category": "constraints", "text": "bad knee", "safety_critical": True},
    ]
    kept, rejected = sanitize_facts(keep)
    assert rejected == 0 and [f["text"] for f in kept] == ["likes to run", "hates cardio", "uses Strava", "bad knee"]
    drop = [
        {"action": "add", "category": "constraints", "text": "messed up"},
        {"action": "add", "category": "communication_preferences", "text": "so tired"},
        {"action": "add", "category": "goals", "text": "abs"},
    ]
    kept, rejected = sanitize_facts(drop)
    assert kept == [] and rejected == 3
