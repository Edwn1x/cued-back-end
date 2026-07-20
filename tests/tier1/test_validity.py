"""
Phase 1 — validity windows + history + the safety-invalidation audit guard.

Invalidation MOVES an entry into a __history__ bucket (exempt from the injectable
size budget), so live readers see only valid entries automatically. Absence of
`invalidated_at` == valid, so old profiles need no backfill.

The safety guard is the load-bearing part once the model gets write access
(Phase 3 remember tool): a safety:true entry may only be closed WITH a recorded
trigger, and a closure without one is rejected — a hallucinated invalidation of a
real allergy/injury is the worst write this system can make.
"""

from __future__ import annotations


def _seed(db, category, text, *, safety=False):
    from tests.factories import make_user
    from memory import apply_facts
    user = make_user(db)
    profile, _ = apply_facts(None, [{
        "action": "add", "category": category, "text": text,
        "replaces_text": None, "safety_critical": safety,
    }])
    user.user_profile_memory = profile
    db.commit()
    entry = profile[category][0]
    return user, profile, entry


def test_invalidate_moves_entry_to_history_and_out_of_render(db):
    from memory import invalidate_entry, build_memory_block, HISTORY_KEY

    user, profile, entry = _seed(db, "schedule", "has an exam every friday morning")
    assert "exam every friday" in build_memory_block(user, "training")  # precondition

    ok = invalidate_entry(profile, entry["id"], by="user", trigger="msg-123")
    user.user_profile_memory = profile
    db.commit()

    assert ok is True
    assert "exam every friday" not in build_memory_block(user, "training")
    hist = profile.get(HISTORY_KEY, [])
    assert len(hist) == 1
    assert hist[0]["invalidated_at"] and hist[0]["invalidated_by"] == "user"
    assert hist[0]["invalidated_trigger"] == "msg-123"
    assert hist[0]["category"] == "schedule"


def test_safety_invalidation_without_trigger_is_rejected(db):
    from memory import invalidate_entry, build_memory_block, HISTORY_KEY

    user, profile, entry = _seed(db, "constraints", "severe peanut allergy", safety=True)

    ok = invalidate_entry(profile, entry["id"], by="model", trigger=None)
    user.user_profile_memory = profile
    db.commit()

    assert ok is False, "safety entry closed without a recorded trigger"
    # entry stays valid and still renders for every agent
    assert "peanut allergy" in build_memory_block(user, "nutrition")
    assert not profile.get(HISTORY_KEY)


def test_safety_invalidation_with_trigger_succeeds(db):
    from memory import invalidate_entry, build_memory_block, HISTORY_KEY

    user, profile, entry = _seed(db, "constraints", "tweaked left shoulder", safety=True)

    ok = invalidate_entry(profile, entry["id"], by="user_healed", trigger="msg-42")
    user.user_profile_memory = profile
    db.commit()

    assert ok is True
    assert "tweaked left shoulder" not in build_memory_block(user, "training")
    assert profile[HISTORY_KEY][0]["invalidated_trigger"] == "msg-42"


def test_dedup_ignores_invalidated_entries(db):
    """A re-injury is a NEW fact, not a duplicate of the healed one in history."""
    from memory import invalidate_entry, apply_facts, HISTORY_KEY

    user, profile, entry = _seed(db, "constraints", "sore left shoulder", safety=True)
    invalidate_entry(profile, entry["id"], by="user_healed", trigger="msg-1")

    profile, stats = apply_facts(profile, [{
        "action": "add", "category": "constraints", "text": "sore left shoulder",
        "replaces_text": None, "safety_critical": True,
    }])

    live = profile["constraints"]
    assert len(live) == 1 and stats["added"] == 1, "re-injury was wrongly deduped against history"
    assert len(profile[HISTORY_KEY]) == 1


def test_update_via_substring_match_invalidates_old_and_adds_new(db):
    """Distinctive-substring update (was byte-exact): the old value moves to
    history, the new one becomes the single current fact."""
    from memory import apply_facts, HISTORY_KEY

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "goals",
        "text": "wants to bench 225 by summer", "replaces_text": None,
        "safety_critical": False,
    }])
    profile, stats = apply_facts(profile, [{
        "action": "update", "category": "goals",
        "text": "wants to bench 245 by fall", "replaces_text": "bench 225",
        "safety_critical": False,
    }])

    live = profile["goals"]
    assert len(live) == 1 and "245" in live[0]["text"]
    assert stats["updated"] == 1
    assert len(profile[HISTORY_KEY]) == 1 and "225" in profile[HISTORY_KEY][0]["text"]
