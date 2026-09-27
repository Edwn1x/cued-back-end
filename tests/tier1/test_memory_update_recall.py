"""
memory-update-recall — the `remember` tool recalled stated facts WORSE than legacy
extraction because updates duplicated instead of replacing, then the fresh copy got
evicted. These tests pin the five fixes:

  1. a PARAPHRASED update replaces instead of duplicating (fuzzy update matching)
  2. a contradicting same-topic add supersedes; a distinct-but-related add does NOT
  3. the fresh superseding fact survives an eviction pass while the stale one goes
  4. entry ids are rendered into the block and are usable by invalidate
  5. a truncated routine paste keeps the days that parsed (salvage)

plus the load-bearing guard: safety/allergy entries are NEVER machine-closed by any
of the new matchers.
"""

from __future__ import annotations

import json

import config


# ─── Fix 1: paraphrased update replaces instead of duplicating ───────────────

def test_paraphrased_update_replaces_instead_of_duplicating(db):
    from memory import apply_facts, HISTORY_KEY

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "schedule",
        "text": "goes to sleep around 11pm and wakes at 7am",
        "replaces_text": None, "safety_critical": False,
    }])
    # replaces_text is a PARAPHRASE ("sleep schedule"), not a literal substring of the
    # stored entry — the old byte/substring path would MISS → add → duplicate.
    profile, stats = apply_facts(profile, [{
        "action": "update", "category": "schedule",
        "text": "now sleeps at midnight and wakes at 8am",
        "replaces_text": "sleep schedule",
        "safety_critical": False,
    }])

    live = profile["schedule"]
    assert len(live) == 1, [e["text"] for e in live]
    assert "midnight" in live[0]["text"]
    assert stats["updated"] == 1 and stats["mismatched"] == 0
    assert len(profile[HISTORY_KEY]) == 1 and "11pm" in profile[HISTORY_KEY][0]["text"]


def test_paraphrased_update_is_ambiguity_safe(db):
    """When the paraphrase overlaps two distinct entries equally, don't guess —
    fall back to the mismatch-logged add (no wrong entry silently overwritten)."""
    from memory import apply_facts

    profile, _ = apply_facts(None, [
        {"action": "add", "category": "goals", "text": "wants to run a marathon",
         "replaces_text": None, "safety_critical": False},
        {"action": "add", "category": "goals", "text": "wants to run a 5k",
         "replaces_text": None, "safety_critical": False},
    ])
    profile, stats = apply_facts(profile, [{
        "action": "update", "category": "goals",
        "text": "wants to run faster", "replaces_text": "wants to run",
        "safety_critical": False,
    }])
    # ambiguous ("wants"/"run" overlap both) -> mismatch + add, both originals intact
    assert stats["mismatched"] == 1
    assert sum(1 for e in profile["goals"] if "marathon" in e["text"]) == 1
    assert sum(1 for e in profile["goals"] if "5k" in e["text"]) == 1


# ─── Fix 3: contradicting same-topic add supersedes; distinct facts do not ────

def test_contradicting_same_topic_add_supersedes(db):
    from memory import apply_facts, HISTORY_KEY

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "training_preferences",
        "text": "prefers training at Crossroads gym",
        "replaces_text": None, "safety_critical": False,
    }])
    # A plain add (no replaces_text) that contradicts the same-topic fact — the value
    # word changed, no number to key on, so _find_supersession_target can't catch it.
    profile, stats = apply_facts(profile, [{
        "action": "add", "category": "training_preferences",
        "text": "prefers training at RSF gym",
        "replaces_text": None, "safety_critical": False,
    }])

    live = profile["training_preferences"]
    assert len(live) == 1 and "RSF" in live[0]["text"], [e["text"] for e in live]
    assert len(profile[HISTORY_KEY]) == 1 and "Crossroads" in profile[HISTORY_KEY][0]["text"]


def test_distinct_related_facts_are_not_collapsed(db):
    """Two compatible facts sharing some topic tokens ('likes X for breakfast') must
    NOT be collapsed — the write-time dedup is conservative by design."""
    from memory import apply_facts

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "training_preferences",
        "text": "likes oatmeal for breakfast",
        "replaces_text": None, "safety_critical": False,
    }])
    profile, _ = apply_facts(profile, [{
        "action": "add", "category": "training_preferences",
        "text": "likes eggs for breakfast",
        "replaces_text": None, "safety_critical": False,
    }])
    assert len(profile["training_preferences"]) == 2


# ─── Fix 4: the fresh superseding fact survives eviction, the stale one goes ──

def test_fresh_fact_survives_eviction_while_stale_heavily_used_one_goes(db):
    from memory import apply_facts, _new_entry

    soft = config.USER_PROFILE_MEMORY_CATEGORY_SOFT_CAP
    # A heavily-USED (uses>0), OLD, near-cap-filling stale entry. Under the plain
    # lowest-uses rule a freshly-added uses=0 fact would be evicted ahead of it.
    stale = _new_entry("x" * (soft - 40))
    stale["uses"] = 12
    stale["ts"] = "2020-01-01T00:00:00+00:00"
    profile = {"goals": [stale]}

    profile, _ = apply_facts(profile, [{
        "action": "add", "category": "goals",
        "text": "wants to deadlift 405 by december " + "y" * 30,
        "replaces_text": None, "safety_critical": False,
    }])

    texts = [e["text"] for e in profile["goals"]]
    assert any("deadlift" in t for t in texts), "fresh fact was evicted (the recall drain)"
    assert not any(t.startswith("xxxx") for t in texts), "stale heavily-used entry survived instead"


def test_eviction_protection_is_flag_gated(db, monkeypatch):
    """With the protection flag OFF, the old behavior returns: the fresh uses=0 fact
    is the one evicted. Proves the flag is the live rollback lever."""
    from memory import apply_facts, _new_entry

    monkeypatch.setattr(config, "MEMORY_EVICT_PROTECT_FRESH_ENABLED", False)
    soft = config.USER_PROFILE_MEMORY_CATEGORY_SOFT_CAP
    stale = _new_entry("x" * (soft - 40))
    stale["uses"] = 12
    stale["ts"] = "2020-01-01T00:00:00+00:00"
    profile = {"goals": [stale]}

    profile, _ = apply_facts(profile, [{
        "action": "add", "category": "goals",
        "text": "wants to deadlift 405 by december " + "y" * 30,
        "replaces_text": None, "safety_critical": False,
    }])
    texts = [e["text"] for e in profile["goals"]]
    assert not any("deadlift" in t for t in texts), "flag-off should reproduce the drain"


# ─── Fix 2: ids are rendered into the block and usable by invalidate ──────────

def test_ids_rendered_and_usable_by_invalidate(db):
    import re
    from memory import apply_facts, render_categories, invalidate_entry, CATEGORIES

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "goals", "text": "wants to run a 5k",
        "replaces_text": None, "safety_critical": False,
    }])

    text, _ids = render_categories(profile, CATEGORIES, show_ids=True)
    assert "[id:" in text
    m = re.search(r"\[id:([0-9a-f]+)\]", text)
    assert m, text
    shown_id = m.group(1)
    assert shown_id == profile["goals"][0]["id"]

    # the id shown is exactly what invalidate expects
    ok = invalidate_entry(profile, shown_id, by="remember_tool", trigger="msg-1")
    assert ok and not profile["goals"]

    # default render (no ids) is unchanged
    text_default, _ = render_categories(profile, CATEGORIES)
    assert "[id:" not in text_default


def test_loop_context_shows_ids_when_flag_on(db):
    from memory import apply_facts
    from agent_loop import build_loop_context
    from models import get_session, User

    user_profile, _ = apply_facts(None, [{
        "action": "add", "category": "goals", "text": "training for a spring half marathon",
        "replaces_text": None, "safety_critical": False,
    }])
    from tests.factories import make_user
    user = make_user(db)
    user.user_profile_memory = user_profile
    db.commit()

    s = get_session()
    try:
        ctx = build_loop_context(s.get(User, user.id), s)
    finally:
        s.close()
    assert "half marathon" in ctx
    assert "[id:" in ctx  # the model can now reference a specific fact's id


# ─── Safety guard: nothing new machine-closes a safety/allergy entry ──────────

def test_fuzzy_update_never_targets_safety_entry(db):
    from memory import apply_facts, HISTORY_KEY

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "constraints",
        "text": "severe peanut allergy", "replaces_text": None, "safety_critical": True,
    }])
    # A paraphrased update whose topic overlaps the allergy must NOT close it.
    profile, stats = apply_facts(profile, [{
        "action": "update", "category": "constraints",
        "text": "loves peanut butter now", "replaces_text": "peanut allergy note",
        "safety_critical": False,
    }])
    live = [e["text"] for e in profile["constraints"]]
    assert "severe peanut allergy" in live, "safety allergy was machine-closed"
    assert not profile.get(HISTORY_KEY), "nothing should have been invalidated"
    assert stats["mismatched"] == 1  # fell through to add, safety untouched


def test_topic_supersession_never_closes_safety(db):
    from memory import apply_facts, HISTORY_KEY

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "constraints",
        "text": "bad knee doctor said no squats",
        "replaces_text": None, "safety_critical": True,
    }])
    profile, _ = apply_facts(profile, [{
        "action": "add", "category": "constraints",
        "text": "bad knee doctor said no lunges",
        "replaces_text": None, "safety_critical": False,
    }])
    live = [e["text"] for e in profile["constraints"]]
    assert "bad knee doctor said no squats" in live, "safety constraint was machine-closed"
    assert not profile.get(HISTORY_KEY)


# ─── Fix 5: routine salvage keeps the days that parsed on truncation ──────────

def test_routine_salvage_keeps_parsed_days_on_truncation(anthropic_stub):
    from workouts.routine import parse_routine
    from tests._fake_anthropic import Truncated

    # push + pull arrays close fully; legs is cut off mid-array (the truncation point).
    truncated = (
        '{"days": {'
        '"push": [{"name": "Bench press", "sets": 3, "reps": 8}], '
        '"pull": [{"name": "Barbell row", "sets": 3, "reps": 8}], '
        '"legs": [{"name": "Squat", "sets'
    )
    anthropic_stub.reply_with(lambda kw: Truncated(truncated))

    out = parse_routine("some long routine paste", user_id=1)
    assert set(out) == {"push", "pull"}, out
    assert out["push"] and out["pull"]


def test_routine_salvage_disabled_returns_empty(anthropic_stub, monkeypatch):
    from workouts.routine import parse_routine
    from tests._fake_anthropic import Truncated

    monkeypatch.setattr(config, "ROUTINE_PARSE_SALVAGE_ENABLED", False)
    truncated = '{"days": {"push": [{"name": "Bench press", "sets": 3, "reps": 8}], "pull": [{"na'
    anthropic_stub.reply_with(lambda kw: Truncated(truncated))
    assert parse_routine("paste", user_id=1) == {}


def test_routine_untruncated_full_parse_unaffected(anthropic_stub):
    """A clean (non-truncated) parse still goes through json.loads, not salvage."""
    from workouts.routine import parse_routine

    full = json.dumps({"days": {
        "push": [{"name": "Bench press", "sets": 3, "reps": 8}],
        "pull": [{"name": "Barbell row", "sets": 3, "reps": 8}],
    }})
    anthropic_stub.reply_with(lambda kw: full)
    out = parse_routine("paste", user_id=1)
    assert set(out) == {"push", "pull"}


# ─── Fix 6: semantic update tier (synonym-level paraphrase) ───────────────────

def _seed_bedtime(profile_from):
    from memory import apply_facts
    profile, _ = apply_facts(profile_from, [{
        "action": "add", "category": "schedule", "text": "Bedtime: 23:00",
        "replaces_text": None, "safety_critical": False,
    }])
    return profile


def test_semantic_update_supersedes_synonym_paraphrase(db, anthropic_stub):
    """The lexical matchers miss (bedtime / sleep / midnight share no literal token);
    Haiku returns the matching id → supersede, NO duplicate."""
    from memory import apply_facts, HISTORY_KEY

    profile = _seed_bedtime(None)
    target_id = profile["schedule"][0]["id"]
    anthropic_stub.reply_with(lambda kw: target_id)

    profile, stats = apply_facts(profile, [{
        "action": "update", "category": "schedule",
        "text": "going to sleep around 12am (midnight) due to work",
        "replaces_text": "their usual sleep time",   # 0 literal overlap with "Bedtime: 23:00"
        "safety_critical": False,
    }])

    live = profile["schedule"]
    assert len(live) == 1 and "midnight" in live[0]["text"], [e["text"] for e in live]
    assert stats["updated"] == 1 and stats["mismatched"] == 0
    assert len(profile[HISTORY_KEY]) == 1 and "23:00" in profile[HISTORY_KEY][0]["text"]


def test_semantic_update_none_falls_back_to_add(db, anthropic_stub):
    from memory import apply_facts

    profile = _seed_bedtime(None)
    anthropic_stub.reply_with(lambda kw: "none")

    profile, stats = apply_facts(profile, [{
        "action": "update", "category": "schedule",
        "text": "has a dentist appointment next tuesday",
        "replaces_text": "their usual sleep time",
        "safety_critical": False,
    }])
    assert stats["mismatched"] == 1 and stats["added"] == 1
    assert len(profile["schedule"]) == 2


def test_semantic_update_fails_open_on_model_error(db, anthropic_stub):
    from memory import apply_facts

    profile = _seed_bedtime(None)

    def boom(kw):
        raise RuntimeError("haiku down")
    anthropic_stub.reply_with(boom)

    profile, stats = apply_facts(profile, [{
        "action": "update", "category": "schedule",
        "text": "sleeps at midnight now that work shifted",
        "replaces_text": "their usual sleep time",
        "safety_critical": False,
    }])
    # fail-open: no crash, behaves exactly like today's add
    assert stats["mismatched"] == 1 and stats["added"] == 1
    assert len(profile["schedule"]) == 2


def test_semantic_update_never_targets_safety(db, anthropic_stub):
    """A safety entry is never offered as a candidate (the listing is empty), so even
    if the model names its id nothing is closed."""
    from memory import apply_facts, HISTORY_KEY

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "constraints", "text": "severe peanut allergy",
        "replaces_text": None, "safety_critical": True,
    }])
    allergy_id = profile["constraints"][0]["id"]
    anthropic_stub.reply_with(lambda kw: allergy_id)  # model (wrongly) names the safety id

    profile, stats = apply_facts(profile, [{
        "action": "update", "category": "constraints",
        "text": "enjoys peanut butter these days",
        "replaces_text": "the peanut thing",
        "safety_critical": False,
    }])
    live = [e["text"] for e in profile["constraints"]]
    assert "severe peanut allergy" in live, "safety entry was machine-closed by the semantic pass"
    assert not profile.get(HISTORY_KEY)
    assert stats["mismatched"] == 1
