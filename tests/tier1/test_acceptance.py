"""
Roadmap-failure acceptance cases — the RATCHET.

Genuinely-broken-today cases are @pytest.mark.xfail(strict=True): the suite stays
green, but the day a phase fixes the behavior the test XPASSes and breaks the
build, forcing the marker's removal into that phase's diff. Cases whose fix has
no system to test against yet are skip-until (they can't fail for the right
reason, so xfail would be dishonest).

All assertions are on deterministic state / render / gate output — never on the
mocked model's words — so an XPASS means the behavior actually changed.
"""

from __future__ import annotations

import pytest


# ─── Failure 1: facts lost across the multi-agent split ──────────────────────

def test_cross_domain_fact_reaches_the_seeding_agent(db):
    """Control: a 'schedule' fact renders for training (schedule IS in its map).
    Proves the seed + render work, so the xfail below fails for the right reason."""
    from tests.factories import make_user
    from memory import apply_facts, build_memory_block

    user = make_user(db)
    profile, _ = apply_facts(None, [{
        "action": "add", "category": "schedule",
        "text": "has an organic chem exam every friday morning",
        "replaces_text": None, "safety_critical": False,
    }])
    user.user_profile_memory = profile
    db.commit()
    assert "organic chem exam" in build_memory_block(user, "training")


def test_cross_domain_fact_reaches_the_unified_loop_context(db):
    """A schedule fact is available to the single loop regardless of domain — the
    unified render (all categories) is the Phase-2 fix for the per-agent slice map
    that dropped cross-domain facts. FIXED in Phase 2 — xfail marker removed."""
    from tests.factories import make_user
    from memory import apply_facts
    from agent_loop import build_loop_context
    from models import get_session

    user = make_user(db)
    profile, _ = apply_facts(None, [{
        "action": "add", "category": "schedule",
        "text": "has an organic chem exam every friday morning",
        "replaces_text": None, "safety_critical": False,
    }])
    user.user_profile_memory = profile
    db.commit()

    s = get_session()
    try:
        ctx = build_loop_context(user, s)
    finally:
        s.close()
    assert "organic chem exam" in ctx


# ─── Failure 3: scheduler blind to conversation ("already went") ─────────────

def test_already_at_gym_suppresses_pre_workout_nudge(db, sms_capture):
    """A went_to_gym event today must suppress the pre_workout nudge. FIXED in
    Phase 1 (Event table + scheduler gate) — xfail marker removed."""
    from tests.factories import make_user
    from engagement_tracker import should_send
    from events import record_event
    import scheduler

    user = make_user(
        db,
        confirmed_training_days="mon,tue,wed,thu,fri,sat,sun",  # always a training day
        unanswered_count=0,
    )
    record_event(user.id, "went_to_gym")  # the Phase 1 mechanism for "already went"

    # Guard: the ONLY reason to suppress should be the event, not engagement
    # gating — otherwise a spurious pass. Assert it would otherwise be allowed.
    assert should_send(user, "pre_workout"), "engagement gate would suppress; fix setup"

    scheduler.send_scheduled_message(user.id, "pre_workout")
    assert sms_capture == [], f"nudge fired despite an already-went-to-gym event: {sms_capture}"


# ─── Failure 5a: injuries are immortal (never heal) ──────────────────────────

@pytest.mark.xfail(strict=True, reason="failure 5a — invalidation MECHANISM landed "
                   "in Phase 1 (invalidate_entry + safety-trigger guard); the heal "
                   "DETECTION trigger that calls it lands in Phase 3 (remember-invalidate)")
def test_healed_injury_leaves_active_context(db, driver):
    """A previously-reported injury should leave active context once the user
    says it healed. Today nothing invalidates it, so it renders forever."""
    from tests.factories import make_user
    from memory import apply_facts, build_memory_block
    from models import get_session, User

    user = make_user(db)
    profile, _ = apply_facts(None, [{
        "action": "add", "category": "constraints",
        "text": "tweaked left shoulder", "replaces_text": None,
        "safety_critical": True,
    }])
    user.user_profile_memory = profile
    db.commit()

    driver.send(user, "good news — my shoulder is all healed now, back to normal")

    s = get_session()
    try:
        fresh = s.query(User).get(user.id)
        assert "tweaked left shoulder" not in build_memory_block(fresh, "training")
    finally:
        s.close()


# ─── Failure 5b: contradictory numeric facts coexist ─────────────────────────

def test_changed_numeric_fact_yields_one_current_value(db):
    """'trains 3 days/week' then 'trains 5 days/week' should collapse to one
    current fact. FIXED in Phase 1 (supersession of numeric-divergent near-matches
    + validity windows: the old value moves to history) — xfail marker removed."""
    from memory import apply_facts

    profile, _ = apply_facts(None, [{
        "action": "add", "category": "schedule",
        "text": "trains 3 days per week", "replaces_text": None,
        "safety_critical": False,
    }])
    # A later turn where the user says they now train 5 days — a plain 'add'
    # (the extractor doesn't know the exact prior text to 'replace').
    profile, _ = apply_facts(profile, [{
        "action": "add", "category": "schedule",
        "text": "trains 5 days per week", "replaces_text": None,
        "safety_critical": False,
    }])

    live = [e for e in profile["schedule"] if "days per week" in e["text"]]
    assert len(live) == 1, f"expected one current value, got {[e['text'] for e in live]}"
    assert "5" in live[0]["text"]


# ─── Failure 9 (investigation): no webhook idempotency ───────────────────────

def test_duplicate_message_sid_produces_one_inbound(db, driver):
    """Twilio re-delivers a message (slow synchronous classify_message exceeds
    its ~15s webhook timeout). Replaying the same MessageSid must write state
    once. FIXED in Phase 1 (claim-at-top MessageSid dedup) — marker removed."""
    from tests.factories import make_user
    from models import get_session, Message

    user = make_user(db)
    driver.send(user, "just crushed a chicken burrito bowl", message_sid="SMdup123")
    driver.send(user, "just crushed a chicken burrito bowl", message_sid="SMdup123")

    s = get_session()
    try:
        inbound = s.query(Message).filter(
            Message.user_id == user.id, Message.direction == "in").count()
    finally:
        s.close()
    assert inbound == 1, f"duplicate delivery wrote {inbound} inbound rows"


# ─── Failure 4 & correction-honesty: no system to test against yet ───────────

@pytest.mark.skip(reason="failure 4 — split-day consistency needs the Phase 1 "
                  "split pointer to assert on deterministically; lands Phase 1/2")
def test_split_day_is_consistent_within_a_day():
    ...


@pytest.mark.skip(reason="correction-honesty tier-1 — needs the Phase 2 loop + "
                  "Phase 3 manage_log tool before 'the agent must not claim an "
                  "action it can't take' is expressible")
def test_agent_does_not_confabulate_a_deletion_it_cannot_perform():
    ...
